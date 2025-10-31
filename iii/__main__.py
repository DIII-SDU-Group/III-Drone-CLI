import argparse
import argcomplete
from . import system, config, build, deploy
import sys

def main():
    parser = argparse.ArgumentParser(prog='iii')
    subparsers = parser.add_subparsers(dest='subcommand')

    parser_system = subparsers.add_parser('system', help='Commands for system management')
    system.initialize(parser_system)

    parser_config = subparsers.add_parser('config', help='Launches configuration manager')
    parser_config.set_defaults(func=config.run, action='run')
    
    parser_build = subparsers.add_parser('build', help='Commands for building parts of the system')
    build.initialize(parser_build)
    
    parser_deploy = subparsers.add_parser('deploy', help='Commands for deploying parts of the system')
    deploy.initialize(parser_deploy)

    argcomplete.autocomplete(parser)

    args = parser.parse_args()
    if args.subcommand is None:
        parser.print_help()
    elif args.subcommand == 'config':
        args.func()
    elif args.action is None and args.subcommand == 'system':
        parser_system.print_help()
    elif args.action is None and args.subcommand == 'build':
        parser_build.print_help()
    elif args.action is None and args.subcommand == 'deploy':
        parser_deploy.print_help()
    else:
        args.func(args)

if __name__ == '__main__':
    main()