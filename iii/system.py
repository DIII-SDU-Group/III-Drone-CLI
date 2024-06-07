import argparse
import os
import subprocess
from threading import Thread, Event
from time import sleep

CLI_CONFIGURATION = os.getenv('CLI_CONFIGURATION')

if CLI_CONFIGURATION is None:
    print("CLI_CONFIGURATION environment variable is not set. Have you sourced the setup scripts?")
    exit(1)
    
if CLI_CONFIGURATION not in ['host', 'container']:
    print('Invalid configuration. Please set CLI_CONFIGURATION to "host" or "container"')
    exit(1)
    
if CLI_CONFIGURATION == 'container':
    from .system_handler import SystemHandler

    import rclpy
    
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

    @ros_bringup
    def kill_session(system, args):
        return system.kill_session(args)

else:
    from .container_manager import ContainerManager
    from .tmux_handler import TmuxHandler

    def host_bringup(func):
        def wrapper(*args, **kwargs):
            container_manager = ContainerManager()
            result = func(container_manager, *args, **kwargs)
            
            if result:
                exit(0)
            else:
                exit(1)
                
        return wrapper
    
    @host_bringup
    def start(container_manager, args):
        return container_manager.execute_cli(
            'iii system start',
            [
                '--server-timeout-seconds', str(args.server_timeout_seconds),
                '--skip-activate' if args.skip_activate else '',
                '--select-nodes' if len(args.select_nodes) > 0 else '',
                *args.select_nodes
            ]
        )
        
    @host_bringup
    def stop(container_manager, args):
        return container_manager.execute_cli(
            'iii system stop',
            [
                '--server-timeout-seconds', str(args.server_timeout_seconds),
                '--skip-cleanup' if args.skip_cleanup else '',
                '--select-nodes' if len(args.select_nodes) > 0 else '',
                *args.select_nodes
            ]
        )
        
    @host_bringup
    def restart(container_manager, args):
        return container_manager.execute_cli(
            'iii system restart',
            [
                '--server-timeout-seconds', str(args.server_timeout_seconds),
                '--cold' if args.cold else '',
                '--select-nodes' if len(args.select_nodes) > 0 else '',
                *args.select_nodes
            ]
        )
        
    @host_bringup
    def status(container_manager, args):
        return container_manager.execute_cli(
            'iii system status',
            [
                '--server-timeout-seconds', str(args.server_timeout_seconds)
            ]
        )
        
    @host_bringup
    def shutdown(container_manager, args):
        return container_manager.execute_cli(
            'iii system shutdown',
            [
                '--server-timeout-seconds', str(args.server_timeout_seconds),
                '--keep-session' if args.keep_session else ''
            ]
        )
        
    @host_bringup
    def boot(container_manager, args):
        tmux_handler = TmuxHandler()
        
        return tmux_handler.start(attach=args.attach)
        
    @host_bringup
    def attach(container_manager, args):
        tmux_handler = TmuxHandler()
        
        return tmux_handler.attach()
        
    @host_bringup
    def list_nodes(container_manager, args):
        return container_manager.execute_cli(
            'iii system list-nodes',
            [
                '--server-timeout-seconds', str(args.server_timeout_seconds)
            ]
        )
        
    @host_bringup
    def kill_session(container_manager, args):
        return container_manager.execute_cli(
            'iii system kill-session',
            [
                '--server-timeout-seconds', str(args.server_timeout_seconds),
                '--force' if args.force else ''
            ]
        )

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

    shutdown_parser.add_argument(
        '--keep-session',
        action='store_true',
        help='Will not stop the tmux session after shutting down the system.'
    )

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

    kill_session_parser = subparsers.add_parser('kill-session', parents=[parent_parser], help='Kills the system tmux session')
    kill_session_parser.set_defaults(func=kill_session)
    
    kill_session_parser.add_argument(
        '--force',
        action='store_true',
        help='Will not prompt for user confirmation.'
    )