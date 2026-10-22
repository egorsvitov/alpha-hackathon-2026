"""Minimal offline consumer: validate JSONL and form analytical cache keys."""
import argparse
import json
from pathlib import Path
from llm.events import validate_bundle


def read_usable(path):
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        bundle = validate_bundle(json.loads(line))
        if bundle.event.availability == 'withdrawn' or bundle.event.quality == 'insufficient':
            continue
        # Keep conflicts alongside claims; they are not resolved by the producer.
        yield (bundle.event.event_id, bundle.event.event_version), bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('jsonl', type=Path)
    args = parser.parse_args()
    for cache_key, bundle in read_usable(args.jsonl):
        print(json.dumps({'cache_key': cache_key, 'quality': bundle.event.quality,
                          'conflict_count': len(bundle.conflicts)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
