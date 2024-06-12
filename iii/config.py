import os
import subprocess

CLI_CONFIGURATION = os.getenv('CLI_CONFIGURATION')

if CLI_CONFIGURATION is None:
    print("CLI_CONFIGURATION environment variable is not set. Have you sourced the setup scripts?")
    exit(1)
    
if CLI_CONFIGURATION not in ['host', 'container', 'remote']:
    print('Invalid configuration. Please set CLI_CONFIGURATION to "host", "container" or "remote"')
    exit(1)

if CLI_CONFIGURATION == 'container':
    from iii_drone_configuration import configuration_client_node
    
elif CLI_CONFIGURATION == 'host':
    from .container_manager import ContainerManager
    
else:
    from .ssh_manager import SSHManager

def _run_container():
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
    container_manager = ContainerManager()
    
    if container_manager.execute_cli('/home/iii/.local/bin/iii config'):
        exit(0)
        
    exit(1)
    
def _run_remote():
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
    if CLI_CONFIGURATION == 'container':
        _run_container()
    elif CLI_CONFIGURATION == 'host':
        _run_host()
    elif CLI_CONFIGURATION == 'remote':
        _run_remote()
    else:
        print('Invalid configuration. Please set CLI_CONFIGURATION to "host" or "container"')
        exit(1)