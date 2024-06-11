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
    
def install_git(args, keep_open=False):
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

    pull_success, con_success = ssh_manager.execute(f"[ ! -d ~/{III_DRONE_DEPLOYMENT_DIR_NAME} ] && git clone -b {III_DRONE_DEPLOYMENT_BRANCH} {III_DRONE_DEPLOYMENT_URL} ~/{III_DRONE_DEPLOYMENT_DIR_NAME} || (cd ~/{III_DRONE_DEPLOYMENT_DIR_NAME} && git fetch && git pull)")
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not pull_success:
        print('Could not install repo on host: Unknown error')
        exit(1)
    
    print("Deployment repo installed successfully")
    
    if not keep_open:
        exit(0)
    
def install_workspace(args, keep_open=False):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    III_DRONE_WORKSPACE_DIR_NAME = os.getenv('III_DRONE_WORKSPACE_DIR_NAME')
    III_DRONE_WORKSPACE_BRANCH = os.getenv('III_DRONE_WORKSPACE_BRANCH')
    III_DRONE_WORKSPACE_URL = os.getenv('III_DRONE_WORKSPACE_URL')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    if III_DRONE_WORKSPACE_DIR_NAME is None:
        print('III_DRONE_WORKSPACE_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    if III_DRONE_WORKSPACE_BRANCH is None:
        print('III_DRONE_WORKSPACE_BRANCH environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    if III_DRONE_WORKSPACE_URL is None:
        print('III_DRONE_WORKSPACE_URL environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing workspace on host...")
    
    workspace_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_workspace.sh {III_DRONE_WORKSPACE_BRANCH} {III_DRONE_WORKSPACE_URL} {III_DRONE_WORKSPACE_DIR_NAME}",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not workspace_install_success:
        print('Could not install workspace on host: Unknown error')
        exit(1)
        
    print("Workspace installed successfully")
    
    if not keep_open:
        exit(0)
    
def install_docker(args, keep_open=False):
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
        
    print("Docker installed successfully")
    
    if not keep_open:
        exit(0)

def install_dependencies(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing dependencies on host...")
    
    system_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_dependencies.sh",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not system_install_success:
        print('Could not install dependencies on host: Unknown error')
        exit(1)
        
    exit(0)
    
def install_tools(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing tools on host...")
    
    system_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_tools.sh",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not system_install_success:
        print('Could not install tools on host: Unknown error')
        exit(1)
        
    exit(0)
    
def install_cli(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing CLI on host...")
    
    system_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_cli.sh",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not system_install_success:
        print('Could not install CLI on host: Unknown error')
        exit(1)
        
    exit(0)
    
def install_udev_rules(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing udev rules on host...")
    
    system_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_udev_rules.sh",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not system_install_success:
        print('Could not install udev rules on host: Unknown error')
        exit(1)
        
    exit(0)
    
def install_tmuxinator_configuration(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing tmuxinator configuration on host...")
    
    system_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_tmuxinator_configuration.sh",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not system_install_success:
        print('Could not install tmuxinator configuration on host: Unknown error')
        exit(1)
        
    exit(0)
    
def install_configuration(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing configuration on host...")
    
    system_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_configuration.sh",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not system_install_success:
        print('Could not install configuration on host: Unknown error')
        exit(1)
        
    exit(0)
    
def install_environment(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing environment on host...")
    
    system_install_success, con_success = ssh_manager.execute(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_environment.sh",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not system_install_success:
        print('Could not install environment on host: Unknown error')
        exit(1)
        
    exit(0)
    
def install_all(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    III_DRONE_DEPLOYMENT_DIR_NAME = os.getenv('III_DRONE_DEPLOYMENT_DIR_NAME')
    
    if III_DRONE_DEPLOYMENT_DIR_NAME is None:
        print('III_DRONE_DEPLOYMENT_DIR_NAME environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    ssh_manager = SSHManager()
    
    print("Installing all targets on host...")
    
    targets = [
        "dependencies",
        "tools",
        "cli",
        "udev_rules",
        "tmuxinator_configuration",
        "configuration",
        "environment",
    ]
    
    for target in targets:
        print(f"Installing {target.replace('_',' ')}...")
        
        system_install_success, con_success = ssh_manager.execute(
            f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_{target}.sh",
        )
        
        if not con_success:
            print('Could not connect to host. Is the host alive?')
            exit(1)
            
        if not system_install_success:
            print(f'Could not install {target.replace("_"," ")} on host: Unknown error')
            exit(1)
            
    print("All targets installed successfully")
    
    exit(0)

def install(args):
    install_git(args, keep_open=True)
    install_workspace(args, keep_open=True)
    install_docker(args, keep_open=True)
    
    install_all(args)
    
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
    
    print("Pushing container image...")
    
    save_process = subprocess.Popen(
        "docker tag iii_drone_base:latest frnyb/iii_drone_base:latest && docker push frnyb/iii_drone_base:latest",
        shell=True,
        executable='/bin/bash',
    )
    
    save_process.wait()
    
    if save_process.returncode != 0:
        print('Could not push container image')
        exit(1)
        
    print("Pulling container image on host...")
        
    pull_success, con_success = ssh_manager.execute(
        "docker pull frnyb/iii_drone_base:latest",
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not pull_success:
        print('Could not pull container image on host: Unknown error')
        exit(1)
        
    print("Container image deployed successfully. Build the system using 'iii build container' or boot using 'iii system boot' on the host")
    
    exit(0)
    
def synchronize(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
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
    
    syncs = [
        (f"{WORKSPACE_DIR}/cc_ws/src", f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/src",['III-Drone-Core/docs','**/__pycache__']),
        (f"{WORKSPACE_DIR}/cc_ws/install", f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/install",[]),
        (f"{WORKSPACE_DIR}/cc_ws/build", f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/build", []),
        (f"{WORKSPACE_DIR}/cc_ws/log", f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/log", []),
    ]
    
    for src, dest, excludes in syncs:
        print(f"Synchronizing {src} with {dest}...")
        
        sync_success, con_success = ssh_manager.sync(
            src,
            dest,
            exclude_dirs=excludes,
        )
        
        if not con_success:
            print('Could not connect to host. Is the host alive?')
            exit(1)
            
        if not sync_success:
            print(f'Could not synchronize {src} with {dest}: Unknown error')
            exit(1)
    
    print("Workspace synchronized successfully")
    
    exit(0)
    
def ssh(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only deploy in remote configuration')
        exit(1)
        
    ssh_manager = SSHManager()
    
    ssh_manager.open_session()
    
    exit(0)
    
def initialize(parser):
    subparsers = parser.add_subparsers(dest='action')
    
    parser_containers = subparsers.add_parser('container', help='Deploys the container image')
    parser_containers.set_defaults(func=deploy_container)

    parser_install = subparsers.add_parser('install', help='Installs docker on the host')
    parser_install.set_defaults(func=install)
    parser_install_action = parser_install.add_subparsers(dest='install_action')
    
    parser_install_git = parser_install_action.add_parser('git', help='Clones or updates the deployment repo')
    parser_install_git.set_defaults(func=install_git)
    
    parser_install_workspace = parser_install_action.add_parser('workspace', help='Installs the workspace on the host')
    parser_install_workspace.set_defaults(func=install_workspace)

    parser_install_docker = parser_install_action.add_parser('docker', help='Installs docker on the host')
    parser_install_docker.set_defaults(func=install_docker)

    parser_install_dependencies = parser_install_action.add_parser('dependencies', help='Installs dependencies on the host')
    parser_install_dependencies.set_defaults(func=install_dependencies)
    
    parser_install_tools = parser_install_action.add_parser('tools', help='Installs tools on the host')
    parser_install_tools.set_defaults(func=install_tools)
    
    parser_install_cli = parser_install_action.add_parser('cli', help='Installs the CLI on the host')
    parser_install_cli.set_defaults(func=install_cli)
    
    parser_install_udev_rules = parser_install_action.add_parser('udev-rules', help='Installs udev rules on the host')
    parser_install_udev_rules.set_defaults(func=install_udev_rules)
    
    parser_install_tmuxinator_configuration = parser_install_action.add_parser('tmuxinator-configuration', help='Installs tmuxinator configuration on the host')
    parser_install_tmuxinator_configuration.set_defaults(func=install_tmuxinator_configuration)
    
    parser_install_configuration = parser_install_action.add_parser('configuration', help='Installs configuration on the host')
    parser_install_configuration.set_defaults(func=install_configuration)
    
    parser_install_environment = parser_install_action.add_parser('environment', help='Installs environment on the host')
    parser_install_environment.set_defaults(func=install_environment)
    
    parser_install_all = parser_install_action.add_parser('all', help='Installs dependencies, tools, cli, udev_rules, tmuxinator_configuration, configuration and environment on host. Git, workspace and docker must be installed explicitly first.')
    parser_install_all.set_defaults(func=install_all)

    parser_synchronize = subparsers.add_parser('synchronize', help='Synchronizes the workspace with the host')
    parser_synchronize.set_defaults(func=synchronize)

    parser_ssh = subparsers.add_parser('ssh', help='Opens an SSH session to the host')
    parser_ssh.set_defaults(func=ssh)

    
    