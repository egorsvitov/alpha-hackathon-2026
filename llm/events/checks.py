"""Offline schema and fixture checks for every active stage."""
import argparse
import json
import subprocess
import sys
from .paths import ROOT

CHECKS = (
    ('contracts.schemas', ['--check']),
    ('contracts.check', []),
    ('extraction.admin_cli', ['schema', '--check']),
    ('extraction.cli', ['schema', '--check']),
    ('normalization.cli', ['schema', '--check']),
    ('normalization.cli', ['check']),
    ('catalog.cli', ['schema', '--check']),
    ('catalog.cli', ['check']),
    ('publication.cli', ['schema', '--check']),
)


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    results = []
    for module, args in CHECKS:
        run = subprocess.run([sys.executable, '-m', 'llm.events.' + module, *args],
                             cwd=ROOT, text=True, capture_output=True)
        results.append({'check':module, 'ok':run.returncode == 0})
        if run.returncode:
            results[-1]['diagnostic'] = run.stdout + run.stderr
    ok = all(r['ok'] for r in results)
    print(json.dumps({'status':'ok' if ok else 'failed', 'checks':results}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
