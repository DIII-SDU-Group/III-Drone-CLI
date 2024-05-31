import argparse

def start(args):
    print('Starting system...')
    # Add code here to start system

def stop(args):
    print('Stopping system...')
    # Add code here to stop system

def restart(args):
    print('Restarting system...')
    # Add code here to restart system

def status(args):
    print('Checking system status...')
    # Add code here to check system status

def add_arguments(parser):
    subparsers = parser.add_subparsers(dest='action')

    subparsers.add_parser('start').set_defaults(func=start)
    subparsers.add_parser('stop').set_defaults(func=stop)
    subparsers.add_parser('restart').set_defaults(func=restart)
    subparsers.add_parser('status').set_defaults(func=status)