import argparse
import argcomplete
from . import system

def main():
    parser = argparse.ArgumentParser(prog='iii')
    subparsers = parser.add_subparsers(dest='subcommand')

    parser_system = subparsers.add_parser('system')
    system.add_arguments(parser_system)

    argcomplete.autocomplete(parser)

    args = parser.parse_args()
    if args.subcommand is None:
        parser.print_help()
    elif args.action is None:
        parser_system.print_help()
    else:
        args.func(args)

if __name__ == '__main__':
    main()