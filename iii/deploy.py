"""Remote deployment subcommands for the III CLI.

This module coordinates repository installation, workspace setup, dependency
installation, and related deployment tasks over SSH-driven host access.
"""

import argparse
import os
import subprocess
import time

CLI_CONFIGURATION = os.getenv('CLI_CONFIGURATION')

if CLI_CONFIGURATION is None:
    print("CLI_CONFIGURATION environment variable is not set. Have you sourced the setup scripts?")
    exit(1)
    
if CLI_CONFIGURATION not in ['host', 'container', 'remote', 'dev']:
    print('Invalid configuration. Please set CLI_CONFIGURATION to "host", "container", "remote" or "dev"')
    exit(1)

if CLI_CONFIGURATION == 'remote':
    from .ssh_manager import SSHManager
    
def install_git(args, keep_open=False):
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy git in remote or dev configuration')
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
    
    if args.force:
        print("Warning: Forcing update will reset the deployment repository to the latest commit. All local changes will be lost.")
        time.sleep(1)
    
    print("Pulling repository on host...")
    
    command = f"[ ! -d ~/{III_DRONE_DEPLOYMENT_DIR_NAME} ] && git clone -b {III_DRONE_DEPLOYMENT_BRANCH} {III_DRONE_DEPLOYMENT_URL} ~/{III_DRONE_DEPLOYMENT_DIR_NAME} || (cd ~/{III_DRONE_DEPLOYMENT_DIR_NAME} && git fetch"
    
    if args.force:
        command += " && git reset --hard && git clean -fdx"

    command += " && git pull)"

    pull_success, con_success = ssh_manager.execute(command)
    
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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

    if args.force:
        print("Warning: Forcing update will reset the workspace repository to the latest commit. All local changes will be lost.")
        time.sleep(1)
        
    ssh_manager = SSHManager()
    
    print("Installing workspace on host...")

    command = f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/scripts/install_workspace.sh {III_DRONE_WORKSPACE_BRANCH} {III_DRONE_WORKSPACE_URL} {III_DRONE_WORKSPACE_DIR_NAME}"
    
    if args.force:
        command += " --force"
    
    workspace_install_success, con_success = ssh_manager.execute(command)
    
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
        exit(1)

    has_target = False
        
    if args.git or args.all:
        install_git(args, keep_open=True)
        has_target = True
        
    if args.workspace or args.all:
        install_workspace(args, keep_open=True)
        has_target = True
        
    if args.docker or args.all:
        install_docker(args, keep_open=True)
        has_target = True
        
    if args.dependencies or args.all:
        install_dependencies(args)
        has_target = True
        
    if args.tools or args.all:
        install_tools(args)
        has_target = True
        
    if args.cli or args.all:
        install_cli(args)
        has_target = True
        
    if args.udev_rules or args.all:
        install_udev_rules(args)
        has_target = True
        
    if args.tmuxinator_configuration or args.all:
        install_tmuxinator_configuration(args)
        has_target = True
        
    if args.configuration or args.all:
        install_configuration(args)
        has_target = True
        
    if args.environment or args.all:
        install_environment(args)
        has_target = True

    if not has_target:
        print('No target specified. Specify a target using --<target> or use --all to install all targets.')
        exit(1)
        
    exit(0)
    
def deploy_container(args):
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy container in remote or dev configuration')
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
        
    print("Container image deployed successfully. Launch the container using 'iii system up'")
    
    exit(0)
    
def synchronize(args):
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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
    
    syncs = []
    
    if args.src or args.all:
        syncs.append(
            (f"{WORKSPACE_DIR}/src", f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/src",['III-Drone-Core/docs','**/__pycache__']),
        )
        
    if args.install or args.all:
        syncs.append(
            (f"{WORKSPACE_DIR}/cc_ws/install", f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/install",[]),
        )
        
    if args.build or args.all:
        syncs.append(
            (f"{WORKSPACE_DIR}/cc_ws/build", f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/build", []),
        )
        
    if args.log or args.all:
        syncs.append(
            (f"{WORKSPACE_DIR}/cc_ws/log", f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/log", []),
        )
    
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

def pull_rosbags(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only pull rosbags in remote configuration')
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
    
    from_dir = f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/rosbags"
    to_dir = f"{WORKSPACE_DIR}/rosbags"
    
    sync_success, con_success = ssh_manager.reverse_sync(
        from_dir,
        to_dir
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not sync_success:
        print('Could not pull rosbags: Unknown error')
        exit(1)

def pull_src(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cannot only pull rosbags in remote configuration')
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
    
    sync_success, con_success = ssh_manager.reverse_sync(
        f"~/{III_DRONE_DEPLOYMENT_DIR_NAME}/{III_DRONE_WORKSPACE_DIR_NAME}/src",
        f"{WORKSPACE_DIR}/src"
    )
    
    if not con_success:
        print('Could not connect to host. Is the host alive?')
        exit(1)
        
    if not sync_success:
        print('Could not pull rosbags: Unknown error')
        exit(1)
    
    
def ssh(args):
    if CLI_CONFIGURATION not in ['remote', 'dev']:
        print('Cannot only deploy in remote or dev configuration')
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

    parser_install.add_argument('--git', action='store_true', help='Clones or updates the deployment repo')
    parser_install.add_argument('--workspace', action='store_true', help='Installs the workspace on the host')
    parser_install.add_argument('--docker', action='store_true', help='Installs docker on the host')
    parser_install.add_argument('--dependencies', action='store_true', help='Installs dependencies on the host')
    parser_install.add_argument('--tools', action='store_true', help='Installs tools on the host')
    parser_install.add_argument('--cli', action='store_true', help='Installs the CLI on the host')
    parser_install.add_argument('--udev-rules', action='store_true', help='Installs udev rules on the host')
    parser_install.add_argument('--tmuxinator-configuration', action='store_true', help='Installs tmuxinator configuration on the host')
    parser_install.add_argument('--configuration', action='store_true', help='Installs configuration on the host')
    parser_install.add_argument('--environment', action='store_true', help='Installs environment on the host')
    parser_install.add_argument('--all', action='store_true', help='Installs all targets on host.')
    parser_install.add_argument('--force', action='store_true', help='Force update the deployment repo or workspace')

    parser_synchronize = subparsers.add_parser('synchronize', help='Synchronizes the workspace with the host')
    parser_synchronize.set_defaults(func=synchronize)
    
    parser_synchronize.add_argument('--src', action='store_true', help='Synchronize the src directory')
    parser_synchronize.add_argument('--install', action='store_true', help='Synchronize the install directory')
    parser_synchronize.add_argument('--build', action='store_true', help='Synchronize the build directory')
    parser_synchronize.add_argument('--log', action='store_true', help='Synchronize the log directory')
    parser_synchronize.add_argument('--all', action='store_true', help='Synchronize all directories')

    parser_ssh = subparsers.add_parser('ssh', help='Opens an SSH session to the host')
    parser_ssh.set_defaults(func=ssh)

    parser_pull_rosbags = subparsers.add_parser('pull_rosbags', help='Pulls rosbags from the host')
    parser_pull_rosbags.set_defaults(func=pull_rosbags)
    
    parser_pull_src = subparsers.add_parser('pull_src', help='Pulls src from the host')
    parser_pull_src.set_defaults(func=pull_src)
    
