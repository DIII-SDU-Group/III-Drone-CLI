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
    
def _build_container_remote(push=False):
    WORKSPACE_DIR = os.getenv('WORKSPACE_DIR')
    
    if WORKSPACE_DIR is None:
        print('WORKSPACE_DIR environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)

    print('Building base container image...')
        
    process = subprocess.Popen(
        f"docker buildx build --platform linux/arm64 -f {WORKSPACE_DIR}/Dockerfile -t iii_drone_base:latest {WORKSPACE_DIR}",
        shell=True,
        executable='/bin/bash',
        cwd=WORKSPACE_DIR,
    )
    
    process.wait()
    
    if process.returncode != 0:
        print('Could not build container image')
        exit(1)
        
    if push:
        print('Pushing container image...')
        
        process = subprocess.Popen(
            "docker tag iii_drone_base:latest frnyb/iii_drone_base:latest && docker push frnyb/iii_drone_base:latest",
            shell=True,
            executable='/bin/bash',
        )
        
        process.wait()
        
        if process.returncode != 0:
            print('Could not push container image')
            exit(1)

    print("Building cross-compilation container image...")
    
    process = subprocess.Popen(
        f"docker build -t iii_cc:latest -f {WORKSPACE_DIR}/Dockerfile.cc {WORKSPACE_DIR}",
        shell=True,
        executable='/bin/bash',
        cwd=WORKSPACE_DIR,
    )
    
    process.wait()

    if process.returncode != 0:
        print('Could not build cross-compilation container image')
        exit(1)
        
    print("Container images built successfully. Deploy to target using 'iii deploy container'")
    
    exit(0)

def build_container(args):
    if CLI_CONFIGURATION == 'container':
        print('Cannot build container in container configuration')
        exit(1)
        
    if CLI_CONFIGURATION == 'host':
        _build_container_host()
        
    else:
        _build_container_remote(push=args.push)

def cross_compile(args):
    if CLI_CONFIGURATION != 'remote':
        print('Cross compilation can only be done in remote configuration')
        exit(1)
        
    WORKSPACE_DIR = os.getenv('WORKSPACE_DIR')
    
    if WORKSPACE_DIR is None:
        print('WORKSPACE_DIR environment variable is not set. Has the setup_remote.bash script been sourced?')
        exit(1)

    # Preparing workspace for cross-compilation
    os.system(f"cp -rf {WORKSPACE_DIR}/src {WORKSPACE_DIR}/cc_ws/")
    os.system(f"cp -rf {WORKSPACE_DIR}/setup_real.bash {WORKSPACE_DIR}/cc_ws/")
        
    if args.micro_ros_agent:
        # Running emulated build of micro_ros_agent
        process = subprocess.Popen(
            "docker run -it --rm --init --privileged --platform linux/arm64 -v ./cc_ws:/home/iii/ws:cached iii_drone_base:latest colcon build --packages-up-to micro_ros_agent --cmake-force-configure --cmake-clean-cache",
            shell=True,
            executable='/bin/bash',
            cwd=WORKSPACE_DIR,
        )
        
        process.wait()
        
        if process.returncode != 0:
            print('Could not cross-compile micro_ros_agent')
            exit(1)
            
    # Cross compile workspace
    process = subprocess.Popen(
        f"docker run --rm -it --privileged --init -v {WORKSPACE_DIR}/cc_ws:/home/iii/ws:cached iii_cc:latest ./colcon_cc.bash {' '.join(args.colcon_args) if args.colcon_args is not None else ''}",
        shell=True,
        executable='/bin/bash',
    )
    
    process.wait()
    
    if process.returncode != 0:
        print('Could not cross-compile workspace')
        exit(1)
        
    exit(0)
    
def build_system(args):
    container_manager = ContainerManager()
    container_manager.colcon_build(colcon_build_args=args.colcon_args)

def initialize(parser):
    subparsers = parser.add_subparsers(dest='action')
    
    parser_containers = subparsers.add_parser('containers', help='Builds the container images')
    parser_containers.set_defaults(func=build_container)

    parser_containers.add_argument(
        "--push",
        action="store_true",
        help="Push the built container images to the registry",
    )
    
    parser_system = subparsers.add_parser('system', help='Builds the ROS2 system')
    parser_system.set_defaults(func=build_system)
    
    parser_system.add_argument(
        '--colcon-args',
        type=str,
        nargs=argparse.REMAINDER,
        help='Arguments to pass to colcon build',
    )
    
    parser_cross_compile = subparsers.add_parser('cross-compile', help='Cross compiles the ROS2 system')
    parser_cross_compile.set_defaults(func=cross_compile)

    parser_cross_compile.add_argument(
        "--micro-ros-agent",
        action="store_true",
        help="Also compile micro_ros_agent. This is excluded by default since it requires an emulated build environment.",
    )
    
    parser_cross_compile.add_argument(
        "--colcon-args",
        type=str,
        nargs=argparse.REMAINDER,
        help="Arguments to pass to colcon build",
    )
    
    
    