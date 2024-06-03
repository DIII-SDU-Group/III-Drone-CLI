import argparse
import argcomplete
from . import system, config

def main():
    parser = argparse.ArgumentParser(prog='iii')
    subparsers = parser.add_subparsers(dest='subcommand')

    parser_system = subparsers.add_parser('system', help='Commands for system management')
    system.initialize(parser_system)

    parser_config = subparsers.add_parser('config', help='Launches configuration manager')
    parser_config.set_defaults(func=config.run, action='run')

    argcomplete.autocomplete(parser)

    args = parser.parse_args()
    if args.subcommand is None:
        parser.print_help()
    elif args.subcommand == 'config':
        args.func()
    elif args.action is None:
        parser_system.print_help()
    else:
        args.func(args)

if __name__ == '__main__':
    main()