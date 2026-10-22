"""One CLI for the event pipeline. Each stage still exposes a Python API."""
import argparse
from importlib import import_module
import sys

COMMANDS = {
    'storage': ('storage.cli', 'Батчи, версии статей, задания и миграции'),
    'extract': ('extraction.cli', 'Обнаружение → предметные пакеты → варианты A/B'),
    'llm': ('extraction.admin_cli', 'Простой B3, профили, usage и бюджет'),
    'normalize': ('normalization.cli', 'B4: цитаты, числа, даты и диагностика'),
    'catalog': ('catalog.cli', 'B5: сопоставление и история событий'),
    'publish': ('publication.cli', 'B6: публикация, JSONL и происхождение'),
    'contracts': ('contracts.check', 'Проверка разметки и контрактных примеров'),
    'import-news': ('storage.news_adapter', 'Импорт JSONL сборщика в B2'),
    'demo': ('examples.v2_offline', 'Полный A/B-пример без API'),
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=[*COMMANDS, 'check'])
    parser.epilog = '\n'.join(f'{name}: {description}' for name, (_, description) in COMMANDS.items())
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    if not argv or argv[0] in ('-h', '--help'):
        parser.print_help()
        return 0
    if argv[0] == 'check':
        from .checks import main as check
        return check(argv[1:])
    if argv[0] not in COMMANDS:
        parser.error('unknown command: ' + argv[0])
    module = import_module('llm.events.' + COMMANDS[argv[0]][0])
    # Older B1 tools read argparse directly; preserve the familiar -m behavior.
    previous = sys.argv
    try:
        sys.argv = [previous[0], *argv[1:]]
        if argv[0] == 'contracts':
            return module.main()
        return module.main(argv[1:])
    finally:
        sys.argv = previous


if __name__ == '__main__':
    raise SystemExit(main())
