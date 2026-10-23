"""Generate deterministic Draft 2020-12 schemas for public v1 contracts."""
import argparse
import json
from pathlib import Path

from .models import (
    ApplyResult, ArticleRevision, Candidate, ClusterSnapshot, CompanyInterestSet,
    EncodedItem, IndexSnapshot, Interest, RetrievalCandidateBatch, RetrievalRequest,
    RetrievalRun, SearchHit, SearchItem, VectorRecord,
)

SCHEMA_VERSION = 'v1'
PUBLIC_MODELS = (
    ApplyResult, ArticleRevision, Candidate, ClusterSnapshot, CompanyInterestSet,
    EncodedItem, IndexSnapshot, Interest, RetrievalCandidateBatch, RetrievalRequest,
    RetrievalRun, SearchHit, SearchItem, VectorRecord,
)


def schema_document(model):
    """Pydantic is authoritative; schema identities carry the contract version."""
    return dict(model.model_json_schema(mode='validation'),
                **{'$schema': 'https://json-schema.org/draft/2020-12/schema',
                   '$id': f'urn:retriever:{model.__name__}:{SCHEMA_VERSION}'})


def generate(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for model in PUBLIC_MODELS:
        path = output / f'{model.__name__}.{SCHEMA_VERSION}.schema.json'
        path.write_text(json.dumps(schema_document(model), ensure_ascii=False, sort_keys=True, indent=2)+'\n', encoding='utf-8')
        paths.append(path)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='Output directory for versioned JSON schemas')
    args = parser.parse_args(argv)
    try:
        paths = generate(args.output)
    except OSError as exc:
        parser.exit(2, f'error: cannot write schemas: {exc.strerror}\n')
    print(json.dumps({'schema_version': SCHEMA_VERSION, 'schemas': len(paths), 'output': args.output}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
