"""Validate model-authored activity hypotheses, without promoting them to facts."""
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path

VERSION = 'activity-hypotheses.v1'
FIELDS = {'okved_code', 'activity', 'search_description',
          'financial_search_description', 'conditional_topics', 'status', 'generator_model'}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def source_inventory(path):
    """Keep raw codes as strings. Ambiguous company rows are explicitly excluded."""
    counts = Counter()
    companies = defaultdict(set)
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream, delimiter=';'):
            code, name = row['MAINOKVED_CODE'].strip(), row['MAINOKVED_NAME'].strip()
            if not code or not name or not row['SYNTHETIC_U']:
                raise ValueError('missing source company/activity identity')
            counts[code, name] += 1
            companies[row['SYNTHETIC_U']].add(tuple(sorted(row.items())))
    names = defaultdict(set)
    for code, name in counts:
        names[code].add(name)
    if any(len(values) != 1 for values in names.values()):
        raise ValueError('one code has multiple activity names; resolve explicitly')
    activities = [dict(okved_code=code, activity=name, company_rows=count)
                  for (code, name), count in sorted(counts.items())]
    mapping, excluded = {}, []
    for uid, records in sorted(companies.items()):
        if len(records) != 1:
            excluded.append(uid)
        else:
            mapping[uid] = dict(next(iter(records)))['MAINOKVED_CODE'].strip()
    return activities, mapping, excluded


def validate_catalogue(records, activities):
    expected = {r['okved_code']: r['activity'] for r in activities}
    found = {}
    for row in records:
        if set(row) != FIELDS:
            raise ValueError('unexpected/missing catalogue fields')
        code = row['okved_code']
        if code in found or code not in expected or row['activity'] != expected[code]:
            raise ValueError('duplicate, unknown or mislabelled code: ' + code)
        if row['status'] != 'hypothesis' or row['generator_model'] != 'gpt-6-luna':
            raise ValueError('incorrect generation provenance/status: ' + code)
        for key in ('search_description', 'financial_search_description'):
            text = row[key]
            if not isinstance(text, str) or not 30 <= len(text) <= 1500:
                raise ValueError('invalid description length: ' + code)
            if text.strip()[-1] not in '.!?':
                raise ValueError('unfinished sentence: ' + code)
        topics = row['conditional_topics']
        if not isinstance(topics, list) or not 1 <= len(topics) <= 3:
            raise ValueError('conditional topics required: ' + code)
        for topic in topics:
            if set(topic) != {'topic', 'condition'} or any(
                    not isinstance(v, str) or not v.strip() for v in topic.values()):
                raise ValueError('condition missing: ' + code)
        found[code] = row
    if found.keys() != expected.keys():
        raise ValueError('missing codes: ' + ', '.join(sorted(expected.keys() - found.keys())))
    return [found[code] for code in sorted(found)]


def query_text(row, kind):
    fields = {'activity_topics': 'search_description',
              'activity_finance': 'financial_search_description'}
    if kind not in fields:
        raise ValueError('unknown activity view')
    # Conditional topics stay in metadata; their preconditions are NOT assumed true.
    return ' '.join((f"Деятельность: {row['activity']}. Возможные темы новостей: "
                     + row[fields[kind]]).split())


def compile_catalogue(source, shards, output):
    output = Path(output)
    if output.exists():
        raise FileExistsError('immutable catalogue output already exists')
    activities, mapping, excluded = source_inventory(source)
    raw = [r for path in shards for r in json.loads(Path(path).read_text())]
    rows = validate_catalogue(raw, activities)
    duplicate_texts = {}
    for field in ('search_description', 'financial_search_description'):
        groups = defaultdict(list)
        for row in rows:
            groups[row[field]].append(row['okved_code'])
        duplicate_texts[field] = [codes for codes in groups.values() if len(codes) > 1]
    output.mkdir(parents=True)
    save_json(output / 'catalogue.json', rows)
    save_json(output / 'company-activity-map.json', mapping)
    manifest = dict(schema_version=VERSION, source_sha256=sha256(source),
                    input_shards={Path(p).name: sha256(p) for p in shards},
                    activities=len(rows), company_mappings=len(mapping),
                    ambiguous_companies_excluded=excluded,
                    duplicate_descriptions=duplicate_texts,
                    status='model_authored_hypotheses_not_verified_company_facts',
                    human_review='pending', paid_api_calls=0,
                    files={p.name: sha256(p) for p in output.iterdir()})
    save_json(output / 'manifest.json', manifest)
    return manifest
