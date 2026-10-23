"""Prepare shards, compile reviewed agent output, or build GPU vectors (opt-in)."""
import argparse
import json
from pathlib import Path

from retriever.experiments.activity_catalogue import source_inventory, save_json, compile_catalogue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--source', type=Path, required=True)
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--shards', type=int, default=3)
    compile_ = sub.add_parser('compile')
    compile_.add_argument('--source', type=Path, required=True)
    compile_.add_argument('--shards', type=Path, nargs='+', required=True)
    compile_.add_argument('--output', type=Path, required=True)
    encode = sub.add_parser('encode')
    encode.add_argument('--catalogue', type=Path, required=True)
    encode.add_argument('--output', type=Path, required=True)
    encode.add_argument('--model-path', type=Path, required=True)
    encode.add_argument('--batch-size', type=int, default=16)
    args = parser.parse_args()
    if args.command == 'prepare':
        activities, _, _ = source_inventory(args.source)
        if args.shards < 1:
            parser.error('shards must be positive')
        args.output.mkdir(parents=True, exist_ok=False)
        save_json(args.output / 'activities.json', activities)
        for i in range(args.shards):
            save_json(args.output / f'shard-{i+1}.json',
                      activities[i*len(activities)//args.shards:(i+1)*len(activities)//args.shards])
    elif args.command == 'compile':
        print(json.dumps(compile_catalogue(args.source, args.shards, args.output), ensure_ascii=False))
    else:
        from retriever.experiments.gpu_encoder import GPUEncoder
        from retriever.experiments.activity_index import build_activity_index
        encoder = GPUEncoder(args.model_path, args.batch_size)
        index = build_activity_index(args.catalogue, args.output, encoder=encoder)
        report = dict(vectors=len(index.matrix), companies=len(index.metadata['company_map']),
                      encoding_seconds=encoder.elapsed_seconds,
                      peak_gpu_bytes=encoder.torch.cuda.max_memory_allocated(),
                      encoder_fingerprint=encoder.fingerprint)
        save_json(args.output.parent / (args.output.name + '-build-report.json'), report)
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
