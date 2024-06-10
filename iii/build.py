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
    from .container_manager import ContainerManager
    
else:
    from .ssh_manager import SSHManager
    
def _build_container_host():
    container_manager = ContainerManager()

    if container_manager.build():
        exit(0)
        
    exit(1)
    
def _build_container_remote():
    WORKSPACE_DIR = os.getenv('WORKSPACE_DIR')
    
    if WORKSPACE_DIR is None:
        print('WORKSPACE_DIR environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)
        
    process = subprocess.Popen(
        f"docker buildx build --platform linux/arm64 -f {WORKSPACE_DIR}/Dockerfile -t iii_drone_base:latest {WORKSPACE_DIR}",
        shell=True,
        executable='/bin/bash',
    )
    
    process.wait()
    
    if process.returncode != 0:
        print('Could not build container image')
        exit(1)
        
    print("Container image built successfully. Deploy to target using 'iii deploy container'")
    
    exit(0)

def build_container(args):
    if CLI_CONFIGURATION == 'container':
        print('Cannot build container in container configuration')
        exit(1)
        
    if CLI_CONFIGURATION == 'host':
        _build_container_host()
        
    else:
        _build_container_remote()
    
def build_system(args):
    container_manager = ContainerManager()
    container_manager.colcon_build(colcon_build_args=args.colcon_args)

def initialize(parser):
    subparsers = parser.add_subparsers(dest='action')
    
    parser_containers = subparsers.add_parser('container', help='Builds the container image')
    parser_containers.set_defaults(func=build_container)
    
    parser_system = subparsers.add_parser('system', help='Builds the ROS2 system')
    parser_system.set_defaults(func=build_system)
    
    parser_system.add_argument(
        '--colcon-args',
        type=str,
        nargs=argparse.REMAINDER,
        help='Arguments to pass to colcon build',
    )
    
    
    