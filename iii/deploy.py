import argparse
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
    pass
    
elif CLI_CONFIGURATION == 'host':
    pass
    
else:
    from .ssh_manager import SSHManager
    
def deploy_git(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy git in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_BRANCH = os.getenv('III_DRONE_DEPLOYMENT_BRANCH')
    III_DRONE_DEPLOYMENT_URL = os.getenv('III_DRONE_DEPLOYMENT_URL')
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_BRANCH is None:
        print('III_DRONE_DEPLOYMENT_BRANCH environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    if III_DRONE_DEPLOYMENT_URL is None:
        print('III_DRONE_DEPLOYMENT_URL environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Pulling repository on host...")

    commands = [
        f"[ ! -d ~/{III_DRONE_DEPLOYMENT_DIR_NAME} ] && git clone -b {III_DRONE_DEPLOYMENT_BRANCH} --recursive {III_DRONE_DEPLOYMENT_URL} ~/{III_DRONE_DEPLOYMENT_DIR_NAME} || echo 'Repository already exists'",
        f"cd ~/{III_DRONE_DEPLOYMENT_DIR_NAME} && git fetch origin && git checkout {III_DRONE_DEPLOYMENT_BRANCH} && git pull && git submodule update --init --recursive"
    ]

    for command in commands:
        pull_success, con_success = ssh_manager.execute(command)
        
        if not con_success:
            print('Could not connect to host. Is the host alive?')
            exit(1)
            
        if not pull_success:
            print('Could not pull repository on host: Unknown error')
            exit(1)
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not pull_success:
        print('Could not pull repository on host: Unknown error')
        exit(1)

    print("Repository deployed successfully")
    
    exit(0)
        
def install_docker(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing docker on host...")
    
    docker_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_docker.sh",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not docker_install_success:
        print('Could not install docker on host: Unknown error')
        exit(1)
        
    exit(0)
    
def install_system(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing system on host...")
    
    system_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install.sh",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not system_install_success:
        print('Could not install system on host: Unknown error')
        exit(1)
        
    exit(0)
    
def deploy_container(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy container in remote configuration')
        exit(1)
        
    check_image_process = subprocess.Popen(
        "docker image inspect iii_drone_base:latest | grep -q '\"Architecture\": \"arm64\"'",
        shell=True,
        executable='/bin/bash',
    )
    
    check_image_process.wait()
    
    if check_image_process.returncode != 0:
        print('Container image iii_drone_base:latest is not built for arm64 architecture. Build using \'iii build container\'')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Saving container image...")
    
    save_process = subprocess.Popen(
        "docker save iii_drone_base:latest -o /tmp/iii_drone_base.tar.gz",
        shell=True,
        executable='/bin/bash',
    )
    
    save_process.wait()
    
    if save_process.returncode != 0:
        print('Could not save container image')
        exit(1)
        
    print("Transfering container image to host...")
        
    tf_success, con_success = ssh_manager.transfer_to_host(
        '/tmp/iii_drone_base.tar.gz',
        '/tmp/iii_drone_base.tar.gz',
        force=True
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not tf_success:
        print('Could not transfer container image to host: Unknown error')
        exit(1)
        
    print("Loading container image on host...")
    
    load_success, con_success = ssh_manager.execute(
        "docker load -i /tmp/iii_drone_base.tar.gz",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not load_success:
        print('Could not load container image on host: Unknown error')
        exit(1)
        
    print("Container image deployed successfully. Boot the system using 'iii system boot'")
    
    exit(0)
    
def synchronize(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy container in remote configuration')
        exit(1)

    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)

    III_DRONE_WORKSPACE_DIR_NAME = os.getenv('III_DRONE_WORKSPACE_DIR_NAME')
    
    if III_DRONE_WORKSPACE_DIR_NAME is None:
        print('III_DRONE_WORKSPACE_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)

    WORKSPACE_DIR = os.getenv('WORKSPACE_DIR')
    
    if WORKSPACE_DIR is None:
        print('WORKSPACE_DIR environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    sync_success, con_success = ssh_manager.sync(
        f"{WORKSPACE_DIR}",
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}",
        exclude_dirs=['build', 'install', 'log', 'src/III-Drone-Core/docs','**/__pycache__'],
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not sync_success:
        print('Could not synchronize workspace with host: Unknown error')
        exit(1)
        
    print("Workspace synchronized successfully")
    
    exit(0)
    
def initialize(parser):
    subparsers = parser.add_subparsers(dest='action')
    
    parser_git = subparsers.add_parser('git', help='Clones or updates the deployment repository')
    parser_git.set_defaults(func=deploy_git)
    
    parser_containers = subparsers.add_parser('container', help='Deploys the container image')
    parser_containers.set_defaults(func=deploy_container)

    parser_install = subparsers.add_parser('install', help='Installs docker on the host')
    parser_install_action = parser_install.add_subparsers(dest='install_action')
    
    parser_install_docker = parser_install_action.add_parser('docker', help='Installs docker on the host')
    parser_install_docker.set_defaults(func=install_docker)

    parser_install_system = parser_install_action.add_parser('system', help='Installs the system on the host')
    parser_install_system.set_defaults(func=install_system)

    parser_synchronize = subparsers.add_parser('synchronize', help='Synchronizes the workspace with the host')
    parser_synchronize.set_defaults(func=synchronize)
    
    