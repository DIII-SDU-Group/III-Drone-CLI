"""Configuration-client entry points for the III CLI.

Depending on the CLI mode, this module either launches the local configuration
client directly or forwards the request into the container or remote host.
"""

import os
import subprocess

CLI_CONFIGURATION = os.getenv('CLI_CONFIGURATION')

def _run_container():
    from iii_drone_configuration import configuration_client_node

    process = subprocess.Popen(
        "timeout 5s ros2 service call /configuration/configuration_server/configuration_server/get_state lifecycle_msgs/srv/GetState \"{}\" | grep -q 'id=3' && exit 0 || exit 1",
        shell=True,
        start_new_session=True,
        executable='/bin/bash',
    )
    
    process.wait()
    
    if process.returncode != 0:
        print('Configuration server is not running. Start using iii system start')
        
        exit(1)
    
    configuration_client_node.main()
    
def _run_host():
    from .container_manager import ContainerManager

    container_manager = ContainerManager()
    
    if container_manager.execute_cli('/home/iii/.local/bin/iii config'):
        exit(0)
        
    exit(1)
    
def _run_remote():
    from .ssh_manager import SSHManager

    ssh_manager = SSHManager()
    
    command_success, connection_success = ssh_manager.execute('iii config')
        
    if not connection_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not command_success:
        print('Could not run the command on the host. Has the system been deployed?')
        exit(1)
        
    exit(0)

def run():
    if CLI_CONFIGURATION == 'container' or CLI_CONFIGURATION == 'dev':
        _run_container()
    elif CLI_CONFIGURATION == 'host':
        _run_host()
    elif CLI_CONFIGURATION == 'remote':
        _run_remote()
    else:
        print('Invalid configuration. Please set CLI_CONFIGURATION to "host" or "container"')
        exit(1)
