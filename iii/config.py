import os
import subprocess

from iii_drone_configuration import configuration_client_node

def run():
    # process = subprocess.Popen(
    #     # command,
    #     "ros2 node list | grep -q configuration_server && exit 0 || exit 1",
    #     shell=True,
    #     start_new_session=True,
    #     executable='/bin/bash',
    # )
    
    # process.wait()
    
    # if process.returncode != 0:
    #     print('Configuration server is not running. Start using iii system start')
        
    #     exit(1)
        
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