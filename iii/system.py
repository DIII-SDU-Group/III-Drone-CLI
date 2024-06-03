import argparse
import os
import subprocess
from threading import Thread, Event

import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient

from iii_drone_interfaces.srv import GetManagedNodes
from iii_drone_interfaces.action import SupervisorShutdown, SupervisorStart, SupervisorStop, SupervisorRestart

class SystemHandler(Node):
    def __init__(self):
        super().__init__(
            node_name='system_handler',
            namespace='iii/cli'
        )
        
        self.cb_group_1 = rclpy.callback_groups.MutuallyExclusiveCallbackGroup()
        
        self.supervisor_start_client = ActionClient(
            self,
            SupervisorStart,
            '/supervision/supervisor/start',
            callback_group=self.cb_group_1
        )

        self.supervisor_stop_client = ActionClient(
            self,
            SupervisorStop,
            '/supervision/supervisor/stop',
            callback_group=self.cb_group_1
        )
        
        self.supervisor_restart_client = ActionClient(
            self,
            SupervisorRestart,
            '/supervision/supervisor/restart',
            callback_group=self.cb_group_1
        )
        
        self.supervisor_shutdown_client = ActionClient(
            self,
            SupervisorShutdown,
            '/supervision/supervisor/shutdown',
            callback_group=self.cb_group_1
        )
        
        self.get_managed_nodes_client = self.create_client(
            GetManagedNodes, 
            '/supervision/supervisor/get_managed_nodes',
            callback_group=self.cb_group_1
        )

        self._start_send_goal_future = None
        self._start_get_result_future = None

        self._tmuxinator_project = os.environ.get('TMUXINATOR_PROJECT', None)

        if self._tmuxinator_project is not None:
            cmd = ['tmux', 'ls']
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()
            self._tmux_session_running = self._tmuxinator_project in stdout.decode('utf-8')
        else:
            self._tmux_session_running = False
        
        self.start_spin_thread()

        self._response_event = Event()
        self._response_ok = False

        self._result_event = Event()
        self._result_ok = False

        self._send_goal_future = None
        self._get_result_future = None

    def _get_managed_nodes(self):
        
        # Wait for service to be available
        if not self.get_managed_nodes_client.wait_for_service(timeout_sec=2.0):
            print('Service not available. Has the supervisor been launched?')
            return []
        
        event = Event()
        
        def response_callback(future):
            event.set()
            
        request = GetManagedNodes.Request()
        future = self.get_managed_nodes_client.call_async(request)
        future.add_done_callback(response_callback)
        
        event.wait()
        
        if future.result() is not None:
            return future.result().managed_nodes
        
        return []

    def _init_synchronization_objects(self):
        self._response_event = Event()
        self._response_ok = False

        self._result_event = Event()
        self._result_ok = False
        
        self._send_goal_future = None
        self._get_result_future = None
        
    def start_spin_thread(self):
        self._multi_threaded_executor = rclpy.executors.MultiThreadedExecutor()
        self._multi_threaded_executor.add_node(self)
        
        self._multi_threaded_executor_thread = Thread(target=self._multi_threaded_executor.spin)
        self._multi_threaded_executor_thread.start()

    def _feedback_callback(self,feedback):
        print(feedback.feedback.message)
        
    def _response_callback(self,future):
        goal_handle = future.result()
        
        if not goal_handle.accepted:
            print('Supervisor rejected action')
            self._response_ok = False
            self._response_event.set()
            return
        
        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self._result_callback)

        self._response_ok = True
        self._response_event.set()

    def _result_callback(self,future):
        result = future.result().result

        print(result.message)
        
        if result.success:
            self._result_ok = True
            self._result_event.set()
            return
        
        self._result_ok = False
        self._result_event.set()

    def start(self, args) -> bool:
        if not self._tmux_session_running:
            print('System not booted. Use "iii system boot" to boot the system.')
            return False
        
        print('Starting system...')

        self._init_synchronization_objects()
        
        if not self.supervisor_start_client.wait_for_server(timeout_sec=args.server_timeout_seconds):
            print(f'Server not available within {args.server_timeout_seconds} seconds. Has the supervisor node been launched?')
            return False
            
        goal = SupervisorStart.Goal()
        goal.action = (SupervisorStart.Goal.START_ACTION_CONFIGURE if args.skip_activate else SupervisorStart.Goal.START_ACTION_ACTIVATE)
        goal.select_nodes = args.select_nodes
        
        self._send_goal_future = self.supervisor_start_client.send_goal_async(goal,feedback_callback=self._feedback_callback)
        self._send_goal_future.add_done_callback(self._response_callback)

        self._response_event.wait()

        if not self._response_ok:
            return False
        
        self._result_event.wait()
        
        return self._result_ok
            
    def stop(self, args) -> bool:
        if not self._tmux_session_running:
            print('System not booted. Use "iii system boot" to boot the system.')
            return False
        
        print('Stopping system...')
        
        self._init_synchronization_objects()
        
        if not self.supervisor_stop_client.wait_for_server(timeout_sec=args.server_timeout_seconds):
            print(f'Server not available within {args.server_timeout_seconds} seconds. Has the supervisor node been launched?')
            return False
            
        goal = SupervisorStop.Goal()
        goal.action = (SupervisorStop.Goal.STOP_ACTION_DEACTIVATE if args.skip_cleanup else SupervisorStop.Goal.STOP_ACTION_CLEANUP)
        goal.select_nodes = args.select_nodes

        self._send_goal_future = self.supervisor_stop_client.send_goal_async(goal,feedback_callback=self._feedback_callback)
        self._send_goal_future.add_done_callback(self._response_callback)

        self._response_event.wait()
        
        if not self._response_ok:
            return False
        
        self._result_event.wait()
        
        return self._result_ok
        
    def restart(self, args) -> bool:
        if not self._tmux_session_running:
            print('System not booted. Use "iii system boot" to boot the system.')
            return False
        
        print('Restarting system...')
        
        self._init_synchronization_objects()
        
        if not self.supervisor_restart_client.wait_for_server(timeout_sec=args.server_timeout_seconds):
            print(f'Server not available within {args.server_timeout_seconds} seconds. Has the supervisor node been launched?')
            return False
            
        goal = SupervisorRestart.Goal()
        goal.restart_type = (SupervisorRestart.Goal.RESTART_TYPE_COLD if args.cold else SupervisorRestart.Goal.RESTART_TYPE_WARM)
        goal.select_nodes = args.select_nodes
        
        self._send_goal_future = self.supervisor_restart_client.send_goal_async(goal,feedback_callback=self._feedback_callback)
        self._send_goal_future.add_done_callback(self._response_callback)
        
        self._response_event.wait()
        
        if not self._response_ok:
            return False
        
        self._result_event.wait()
        
        return self._result_ok
        
    def status(self, args) -> bool:
        print('System status not implemented yet.')
        
        return True

    def shutdown(self, args) -> bool:
        if not self._tmux_session_running:
            print('System not booted. Use "iii system boot" to boot the system.')
            return False
        
        print('Shutting down system...')

        self._init_synchronization_objects()
        
        if not self.supervisor_shutdown_client.wait_for_server(timeout_sec=args.server_timeout_seconds):
            print(f'Server not available within {args.server_timeout_seconds} seconds. Has the system been booted?')
            return False
            
        goal = SupervisorShutdown.Goal()

        self._send_goal_future = self.supervisor_shutdown_client.send_goal_async(goal,feedback_callback=self._feedback_callback)
        self._send_goal_future.add_done_callback(self._response_callback)
        
        self._response_event.wait()
        
        if not self._response_ok:
            return False
        
        self._result_event.wait()
        
        if self._result_ok:
            self._stop_tmux_session()
            print('System shutdown.')
        
        else:
            print('System shutdown failed. Some things may need to be cleaned up manually. Inspect using "iii system status".')
            
        return self._result_ok

    def boot(self, args) -> bool:
        if self.tmuxinator_project is None:
            print('TMUXINATOR_PROJECT environment variable not set. Cannot boot system.')
            return False

        if self.tmux_session_running:
            print ('System already booted. Use "iii system attach" to attach to the tmux session.')
            
            return False
        
        attach = args.attach
        
        cmd = ['tmuxinator', 'start', self.tmuxinator_project, ('--attach' if attach else '--no-attach')]
        
        # Start tmux session, exit this program, but attach to tmux session if requested
        subprocess.run(cmd)

        if not attach:
            print('System booted. Use "iii system start" to start the system.')
            
        return True

    def attach(self, args) -> bool:
        if self.tmuxinator_project is None:
            print('TMUXINATOR_PROJECT environment variable not set. Cannot attach to tmux session.')
            return False
        
        if self.tmux_session_running:
            cmd = ['tmux', 'attach', '-t', self.tmuxinator_project]
            
            subprocess.run(cmd)
            
            return True
            
        else:
            print('Session not running. Use "iii system boot --attach" to start the system and attach to the tmux session.')
            
            return False

    def list_nodes(self, args) -> bool:
        nodes = self._get_managed_nodes()
        
        if len(nodes) == 0:
            return False
        
        for node in nodes:
            print(f'{node}')
        
        return True
        
    def _stop_tmux_session(self):
        if self.tmuxinator_project is None:
            return
        
        process = subprocess.Popen(['tmuxinator', 'stop', self.tmuxinator_project], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.wait()

    @property
    def tmuxinator_project(self):
        return self._tmuxinator_project
    
    @property
    def tmux_session_running(self):
        return self._tmux_session_running
        
def ros_bringup(func):
    def wrapper(*args, **kwargs):
        rclpy.init()
        system = SystemHandler()
        result = func(system, *args, **kwargs)
        rclpy.shutdown()
        
        if result:
            exit(0)
        else:
            exit(1)
            
    return wrapper

@ros_bringup
def start(system, args):
    return system.start(args)
    
@ros_bringup
def stop(system, args):
    return system.stop(args)

@ros_bringup
def restart(system, args):
    return system.restart(args)
    
@ros_bringup
def status(system, args):
    return system.status(args)
    
@ros_bringup
def shutdown(system, args):
    return system.shutdown(args)

@ros_bringup
def boot(system, args):
    return system.boot(args)

@ros_bringup
def attach(system, args):
    return system.attach(args)

@ros_bringup
def list_nodes(system, args):
    return system.list_nodes(args)

def initialize(parser):
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument(
        '--server-timeout-seconds',
        type=int,
        default=5,
        help='Timeout for waiting for server to be available. Default is 5 seconds.'
    )

    subparsers = parser.add_subparsers(
        dest='action',
        title='Actions',
        description='Available actions for system management'
    )

    start_parser = subparsers.add_parser(
        'start', 
        parents=[parent_parser],
        help='Starts the system'
    )
    start_parser.set_defaults(func=start)

    start_parser.add_argument(
        '--skip-activate',
        action='store_true',
        help='Will only configure the system without activating it.'
    )

    start_parser.add_argument(
        '--select-nodes',
        nargs='+',
        default=[],
        help='Start the specified nodes and all their dependencies.'
    )

    stop_parser = subparsers.add_parser('stop', parents=[parent_parser], help='Stops the system')
    stop_parser.set_defaults(func=stop)

    stop_parser.add_argument(
        "--skip-cleanup",
        action='store_true',
        help='Will only deactivate the system without cleaning it up.'
    )

    stop_parser.add_argument(
        '--select-nodes',
        nargs='+',
        default=[],
        help='Stop the specified nodes and all their dependencies.'
    )

    restart_parser = subparsers.add_parser('restart', parents=[parent_parser], help='Restarts the system')
    restart_parser.set_defaults(func=restart)
    
    restart_parser.add_argument(
        '--cold',
        action='store_true',
        help='Will cleanup the system before starting it again, otherwise will only deactivate before activating.'
    )
    
    restart_parser.add_argument(
        '--select-nodes',
        nargs='+',
        default=[],
        help='Restart the specified nodes and all their dependencies.'
    )

    status_parser = subparsers.add_parser('status', parents=[parent_parser], help='Displays the status of the system')
    status_parser.set_defaults(func=status)
    
    shutdown_parser = subparsers.add_parser('shutdown', parents=[parent_parser], help='Shuts down the system')
    shutdown_parser.set_defaults(func=shutdown)

    boot_parser = subparsers.add_parser('boot', parents=[parent_parser], help='Boots the system')
    boot_parser.set_defaults(func=boot)

    boot_parser.add_argument(
        '--attach',
        action='store_true',
        help='Will attach to the tmux session after starting the system.'
    )
    
    attach_parser = subparsers.add_parser('attach', parents=[parent_parser], help='Attaches to the system tmux session')
    attach_parser.set_defaults(func=attach)

    list_nodes_parser = subparsers.add_parser('list-nodes', parents=[parent_parser], help='Lists all managed nodes in the system')
    list_nodes_parser.set_defaults(func=list_nodes)