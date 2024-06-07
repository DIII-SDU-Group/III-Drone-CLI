import os
import subprocess

CLI_CONFIGURATION = os.getenv('CLI_CONFIGURATION')

if CLI_CONFIGURATION is None:
    print("CLI_CONFIGURATION environment variable is not set. Have you sourced the setup scripts?")
    exit(1)
    
if CLI_CONFIGURATION not in ['host', 'container']:
    print('Invalid configuration. Please set CLI_CONFIGURATION to "host" or "container"')
    exit(1)

if CLI_CONFIGURATION == 'container':
    from iii_drone_configuration import configuration_client_node
    
else:
    from .container_manager import ContainerManager

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
    
    if container_manager.execute_cli('iii config'):
        exit(0)
        
    exit(1)

def run():
    if CLI_CONFIGURATION == 'container':
        _run_container()
    elif CLI_CONFIGURATION == 'host':
        _run_host()
    else:
        print('Invalid configuration. Please set CLI_CONFIGURATION to "host" or "container"')
        exit(1)