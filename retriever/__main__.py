"""Shared CLI dispatcher; implementations register their commands."""
import argparse

def main():
    parser = argparse.ArgumentParser(prog='python -m retriever')
    subparsers = parser.add_subparsers(dest='command', required=True)
    try:
        from .company_build_cli import register_parser
    except ModuleNotFoundError as exc:
        if exc.name != 'retriever.company_build_cli':
            raise
    else:
        register_parser(subparsers)
    from .runtime_cli import register_parser as register_runtime
    register_runtime(subparsers)
    args = parser.parse_args()
    result = args.func(args)
    return result if isinstance(result, int) else 0

if __name__ == '__main__':
    raise SystemExit(main())
