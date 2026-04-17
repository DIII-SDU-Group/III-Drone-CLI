"""Top-level `iii` CLI entry point and subcommand dispatcher."""

import argparse
import argcomplete
from importlib import import_module


def _run_config():
    import_module("iii.config").run()

def main():
    parser = argparse.ArgumentParser(prog='iii')
    subparsers = parser.add_subparsers(dest='subcommand')

    system = import_module("iii.system")
    parser_system = subparsers.add_parser('system', help='Commands for system management')
    system.initialize(parser_system)

    parser_config = subparsers.add_parser('config', help='Launches configuration manager')
    parser_config.set_defaults(func=lambda: _run_config(), action='run')
    
    build = import_module("iii.build")
    parser_build = subparsers.add_parser('build', help='Commands for building parts of the system')
    build.initialize(parser_build)
    
    deploy = import_module("iii.deploy")
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
