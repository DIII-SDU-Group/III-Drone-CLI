import argparse
import os
import subprocess

from .container_manager import ContainerManager

def build_containers(args):
    container_manager = ContainerManager()

    if container_manager.build():
        exit(0)
        
    exit(1)
    
def build_system(args):
    container_manager = ContainerManager()
    container_manager.colcon_build(colcon_build_args=args.colcon_args)

def initialize(parser):
    subparsers = parser.add_subparsers(dest='action')
    
    parser_containers = subparsers.add_parser('containers', help='Builds the containers')
    parser_containers.set_defaults(func=build_containers)
    
    parser_system = subparsers.add_parser('system', help='Builds the ROS2 system')
    parser_system.set_defaults(func=build_system)
    
    parser_system.add_argument(
        '--colcon-args',
        type=str,
        nargs=argparse.REMAINDER,
        help='Arguments to pass to colcon build',
    )
    
    
    