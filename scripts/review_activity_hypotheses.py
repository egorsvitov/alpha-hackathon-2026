"""Same-encoder comparison on a fixed existing showcase, with no quality claims."""
import argparse
import json
from pathlib import Path

import numpy as np

from retriever.index import EventIndex, build_event_index
from retriever.experiments.gpu_encoder import GPUEncoder
from retriever.experiments.activity_index import ActivityIndex
from retriever.experiments.activity_catalogue import save_json, sha256


def dense_ranking(vectors, index):
    scores = np.max(vectors @ index.matrix.T, axis=0)
    event_scores = {}
    for chunk, score in zip(index.chunks, scores):
        uid = chunk.snapshot.event_id
        event_scores[uid] = max(event_scores.get(uid, -1), float(score))
    return [dict(event_id=uid, score=event_scores[uid], title=index.snapshots[uid].bundle['event']['title'])
            for uid in sorted(event_scores, key=lambda uid: (-event_scores[uid], uid))]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--activity-index', type=Path, required=True)
    p.add_argument('--source-event-index', type=Path, required=True)
    p.add_argument('--showcase', type=Path, required=True)
    p.add_argument('--model-path', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    encoder = GPUEncoder(args.model_path)
    activity = ActivityIndex.open(args.activity_index)
    if encoder.fingerprint != activity.fingerprint:
        raise ValueError('different GPU encoder from activity build')
    original = EventIndex.open(args.source_event_index)

    class FrozenStore:
        def list_latest(self, as_of):
            if as_of != original.as_of:
                raise ValueError('comparison cutoff changed')
            return list(original.snapshots.values())

    events = build_event_index(FrozenStore(), encoder=encoder, output_path=args.output / 'events',
        as_of=original.as_of, window_start=original.window_start, window_end=original.window_end)
    if set(events.snapshots) != set(original.snapshots):
        raise ValueError('comparison event corpus changed')
    rows = []
    for file in sorted((args.showcase / 'results').glob('*.json')):
        previous = json.loads(file.read_text())
        profile = json.loads((args.showcase / 'profiles' / file.name).read_text())
        uid = profile['company_id']
        base = previous['trace']['company_queries']['queries']
        base_vectors = encoder.encode_queries([q['metadata_ref']['text'] for q in base])
        new_queries = activity.queries(uid)
        new_vectors = np.stack([v for _, v in new_queries])
        variants = {'base3_gpu': dense_ranking(base_vectors, events),
                    'activity_topics': activity.search_company(uid, events, top_k=100, kinds=('activity_topics',)),
                    'activity_finance': activity.search_company(uid, events, top_k=100, kinds=('activity_finance',)),
                    'activity_both': activity.search_company(uid, events, top_k=100),
                    'base3_plus_activity': dense_ranking(np.concatenate([base_vectors, new_vectors]), events)}
        row = dict(company_id=uid, activity=profile['okved']['name'], region=profile['region'],
                   okved_code=profile['okved']['code'], query_entries=[q for q, _ in new_queries],
                   variants=variants)
        save_json(args.output / file.name, row)
        rows.append(row)
    manifest = dict(status='exploratory_not_Recall_evaluation', company_count=len(rows),
        events=len(events.snapshots), chunks=len(events.chunks), as_of=events.as_of,
        encoder_fingerprint=encoder.fingerprint, source_event_index=original.index_snapshot_id,
        activity_snapshot=activity.metadata['snapshot_id'],
        variant_policy='max cosine over views and chunks, stable event-id tie-break; no threshold or token budget',
        encoded_texts=encoder.encoded_texts, encoding_seconds=encoder.elapsed_seconds,
        peak_gpu_bytes=encoder.torch.cuda.max_memory_allocated(),
        source_showcase_manifest_sha256=sha256(args.showcase / 'manifest.json'), paid_api_calls=0)
    save_json(args.output / 'manifest.json', manifest)
    md = ['# Проверка поисковых гипотез на прежних восьми компаниях', '',
          'Это поисковые кандидаты, не выводы о влиянии на компанию. Разметки релевантности нет; Recall не измерен.', '',
          'Все варианты ниже используют один GPU-энкодер FP32, одинаковые 100 событий и прежние окна/чанки. '
          'Ранжирование — максимум cosine по представлениям и чанкам. Это не прежний H3 с BM25, '
          'а отдельная проверка текстов запросов. Бюджет входа аналитика не применяется.', '']
    for row in rows:
        md += [f"## {row['company_id']} · {row['region']}", '', row['activity'], '']
        for entry in row['query_entries']:
            md += [f"**{entry['kind']}:** {entry['text']}", '']
        for variant in ('base3_gpu', 'activity_both', 'base3_plus_activity'):
            md += [f'### {variant}: первые 10', '']
            for i, hit in enumerate(row['variants'][variant][:10], 1):
                md.append(f"{i}. {hit['title']} — cosine {hit['score']:.4f}")
            md.append('')
    (args.output / 'README.md').write_text('\n'.join(md) + '\n')
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == '__main__':
    main()
