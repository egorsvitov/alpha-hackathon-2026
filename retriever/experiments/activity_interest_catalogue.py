"""Assemble and validate agent-authored interests; performs no model/API calls."""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1] / 'catalogues/activity-interests-v1'
WORK = Path('data/processed/retriever/activity-interests-v1/work')
ACTIVITY_FIELDS = {'okved_code', 'activity', 'interests'}
TOPIC_FIELDS = {'slug', 'title', 'description', 'applicable_when'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(rows, expected):
    """Structural guarantees, plus diagnostics requiring editorial judgment."""
    errors, lengths, seen_ids = [], [], set()
    descriptions = defaultdict(list)
    actual = [(r.get('okved_code'), r.get('activity')) for r in rows]
    wanted = [(r['okved_code'], r['activity']) for r in expected]
    if actual != wanted:
        errors.append('Activity codes/names/order differ from the frozen input')
    for row in rows:
        code = row.get('okved_code')
        if set(row) != ACTIVITY_FIELDS:
            errors.append(f'{code}: unexpected activity fields')
        topics = row.get('interests', [])
        if len(topics) != 5:
            errors.append(f'{code}: expected five interests, got {len(topics)}')
        titles, local_descriptions = set(), set()
        for t in topics:
            slug = t.get('slug', '')
            identity = f'{code}/{slug}'
            if set(t) != TOPIC_FIELDS:
                errors.append(f'{identity}: unexpected topic fields')
            if not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', slug):
                errors.append(f'{identity}: invalid slug')
            if identity in seen_ids:
                errors.append(f'{identity}: duplicate topic ID')
            seen_ids.add(identity)
            for field in ('title', 'description'):
                value = t.get(field)
                if not isinstance(value, str) or not value.strip() or value != value.strip():
                    errors.append(f'{identity}: missing or untrimmed {field}')
            condition = t.get('applicable_when')
            if condition is not None and (not isinstance(condition, str) or not condition.strip()):
                errors.append(f'{identity}: invalid applicability condition')
            title = t.get('title', '').casefold()
            desc = ' '.join(t.get('description', '').casefold().split())
            if title in titles or desc in local_descriptions:
                errors.append(f'{identity}: duplicate title or description within activity')
            titles.add(title)
            local_descriptions.add(desc)
            descriptions[desc].append(identity)
            lengths.append((identity, len(t.get('description', '').split())))
    if errors:
        raise ValueError('\n'.join(errors))
    return {
        'activity_count': len(rows), 'interest_count': len(seen_ids),
        'conditional_interest_count': sum(t['applicable_when'] is not None for r in rows for t in r['interests']),
        'description_word_count_min': min((n for _, n in lengths), default=0),
        'description_word_count_max': max((n for _, n in lengths), default=0),
        'outside_target_word_count': [{'interest_id': i, 'words': n} for i, n in lengths if not 15 <= n <= 35],
        'exact_cross_activity_duplicates': [ids for ids in descriptions.values() if len(ids) > 1],
    }


def assemble(work_root, output):
    expected = read(output / 'activities.json')
    rows, reviews, sources = [], [], []
    for number in (1, 2, 3):
        directory = work_root / f'reviewer-{number}'
        files = sorted(directory.glob('batch-*.json'))
        if not files:
            raise ValueError(f'No reviewed batches in {directory}')
        batch = [row for path in files for row in read(path)]
        validate(batch, read(work_root / f'input-{number}.json'))
        review = read(directory / 'review.json')
        records = review['activities']
        if [r['okved_code'] for r in records] != [r['okved_code'] for r in batch]:
            raise ValueError(f'Review coverage/order differs in {directory}')
        draft_files = sorted((work_root / f'generator-{number}').glob('batch-*.json'))
        draft_rows = [r for p in draft_files for r in read(p)]
        validate(draft_rows, read(work_root / f'input-{number}.json'))
        changed = []
        for original, revised, record in zip(draft_rows, batch, records):
            if record['status'] not in {'approved', 'revised', 'needs_human_review'}:
                raise ValueError(f'Unknown review status: {record}')
            if not isinstance(record['changes'], list) or not isinstance(record['remaining_notes'], list):
                raise ValueError('Review changes and remaining_notes must be lists')
            if original != revised:
                changed.append(record['okved_code'])
                if record['status'] == 'approved' or not record['changes']:
                    raise ValueError(f'Unlogged editorial changes: {record["okved_code"]}')
            elif record['status'] == 'revised':
                raise ValueError(f'Revision claimed without changes: {record["okved_code"]}')
            if record['status'] == 'needs_human_review' and not record['remaining_notes']:
                raise ValueError('Ambiguous activities need an explicit remaining note')
        review['changed_activity_codes_verified'] = changed
        rows.extend(batch)
        reviews.append(review)
        sources.extend({'file': f'generator-{number}/{p.name}', 'sha256': sha(p)} for p in draft_files)
        sources.extend({'file': f'reviewer-{number}/{p.name}', 'sha256': sha(p)} for p in files)
        sources.append({'file': f'reviewer-{number}/review.json', 'sha256': sha(directory / 'review.json')})
    diagnostics = validate(rows, expected)
    save(output / 'catalogue.json', rows)
    save(output / 'reviews.json', reviews)
    with (output / 'catalogue.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=['okved_code', 'activity', 'interest_id', 'title', 'description', 'applicable_when', 'review_status'])
        writer.writeheader()
        statuses = {r['okved_code']: r['status'] for review in reviews for r in review['activities']}
        for row in rows:
            for t in row['interests']:
                writer.writerow(dict(okved_code=row['okved_code'], activity=row['activity'],
                    interest_id=row['okved_code'] + '/' + t['slug'], title=t['title'],
                    description=t['description'], applicable_when=t['applicable_when'] or '',
                    review_status=statuses[row['okved_code']]))
    status_counts = dict(Counter(r['status'] for review in reviews for r in review['activities']))
    report = [
        '# Итог подготовки каталога интересов', '',
        f"Деятельностей: **{len(rows)}**. Отдельных интересов: **{diagnostics['interest_count']}**.", '',
        'Генерация: три gpt-6-luna medium. Независимая редактура: три gpt-6.1-sol medium.',
        'Все темы — гипотезы интересов деятельности; человеческого утверждения нет.', '',
        '## Проверки', '',
        '- Точное совпадение кодов, названий и порядка с зафиксированным списком.',
        '- Ровно пять интересов на деятельность, уникальные идентификаторы и тексты внутри деятельности.',
        '- Полный журнал независимой проверки; заявленные исправления сверены с исходными пакетами.',
        f"- Между деятельностями найдено групп дословных повторов: {len(diagnostics['exact_cross_activity_duplicates'])}.",
        f"- Тем с условием применимости: {diagnostics['conditional_interest_count']}.",
        '- Длина текста проверяется диагностически, без дописывания общих слов ради объёма.', '',
        '## Статусы редакторской проверки', '',
    ]
    report.extend(f'- `{status}`: {count} деятельностей.' for status, count in sorted(status_counts.items()))
    report.extend(['', 'Статусы относятся к деятельности целиком, а не к числу изменённых интересов.', '',
                   '## Оставшиеся замечания', ''])
    notes = [(r['okved_code'], note) for review in reviews for r in review['activities'] for note in r['remaining_notes']]
    report.extend(f'- `{code}`: {note}' for code, note in notes)
    if not notes:
        report.append('Проверяющие не оставили отдельных замечаний; это не заменяет проверку релевантности на новостях.')
    report.extend(['', '## Ограничения этапа', '',
                   'Широкий код деятельности не доказывает все частные операции компании. '
                   'Условные темы требуют проверки применимости. Качество поиска ещё не измерялось.', '',
                   'Эмбеддинги и графовые интересы не вычислены, выдача ретривера не изменена. '
                   'Исходные новости, профили компаний, платежи, ключи и рабочие базы не включены.', ''])
    (output / 'REPORT.md').write_text('\n'.join(report), encoding='utf-8')
    files = ['activities.json', 'catalogue.json', 'catalogue.csv', 'reviews.json', 'EDITORIAL_GUIDE.md', 'README.md', 'REPORT.md']
    manifest = {
        'schema_version': 'activity-interests.v1', 'created_on': '2026-10-23',
        'status': 'agent_reviewed_hypotheses', 'human_approved': False,
        'generator_model': 'gpt-6-luna', 'generator_reasoning_effort': 'medium',
        'reviewer_model': 'gpt-6.1-sol', 'reviewer_reasoning_effort': 'medium',
        'review_status_counts': status_counts,
        'structural_validation': diagnostics,
        'source_catalogue': read(work_root / 'source.json'),
        'reviewed_batch_hashes': sources,
        'assembly_tool': {'path': 'retriever/experiments/activity_interest_catalogue.py',
                          'sha256': sha(Path(__file__))},
        'files': {name: sha(output / name) for name in files},
        'limitations': ['Not verified company facts', 'No independent human approval',
                       'Search interests are hypotheses; conditional topics need applicability checks',
                       'No embeddings or graph interests computed; retrieval runtime unchanged'],
    }
    save(output / 'manifest.json', manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', type=Path, default=WORK)
    parser.add_argument('--output', type=Path, default=ROOT)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    if args.validate_only:
        result = validate(read(args.output / 'catalogue.json'), read(args.output / 'activities.json'))
        manifest = read(args.output / 'manifest.json')
        for name, digest in manifest['files'].items():
            if sha(args.output / name) != digest:
                raise ValueError(f'Artifact hash mismatch: {name}')
    else:
        result = assemble(args.work_root, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
