"""Independent activity-hypothesis vectors with auditable company/code mapping."""
from pathlib import Path
import json
import os
import tempfile

import numpy as np

from retriever.representation_contracts import EncoderMismatch, CorruptVector, content_hash
from .activity_catalogue import query_text, sha256, save_json

KINDS = ('activity_topics', 'activity_finance')


def check_vectors(matrix, count):
    if matrix.shape != (count, 384) or matrix.dtype != np.float32 or not np.isfinite(matrix).all():
        raise CorruptVector('invalid activity vector matrix')
    if count and not np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-5, rtol=0):
        raise CorruptVector('activity vectors must have unit norm')


def build_activity_index(catalogue_path, output, *, encoder):
    source, output = Path(catalogue_path), Path(output)
    if output.exists():
        raise FileExistsError('immutable index already exists')
    manifest = json.loads((source / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if sha256(source / name) != expected:
            raise CorruptVector('catalogue checksum mismatch')
    rows = json.loads((source / 'catalogue.json').read_text())
    entries = [dict(okved_code=row['okved_code'], kind=kind,
                    text=query_text(row, kind), status='hypothesis')
               for row in rows for kind in KINDS]
    vectors = encoder.encode_queries([r['text'] for r in entries])
    check_vectors(vectors, len(entries))
    metadata = dict(schema_version='activity-index.v1', encoder_fingerprint=encoder.fingerprint,
                    encoder=encoder.fingerprint_metadata, entries=entries,
                    catalogue=rows, catalogue_manifest=manifest,
                    company_map=json.loads((source / 'company-activity-map.json').read_text()))
    metadata['snapshot_id'] = content_hash(metadata)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        tmp = Path(tmp)
        np.save(tmp / 'vectors.npy', vectors, allow_pickle=False)
        save_json(tmp / 'metadata.json', metadata)
        save_json(tmp / 'manifest.json', {p.name: sha256(p) for p in tmp.iterdir()})
        ActivityIndex.open(tmp)
        os.rename(tmp, output)
    return ActivityIndex.open(output)


class ActivityIndex:
    @classmethod
    def open(cls, path):
        root = Path(path)
        manifest = json.loads((root / 'manifest.json').read_text())
        if set(manifest) != {'vectors.npy', 'metadata.json'}:
            raise CorruptVector('invalid activity manifest')
        for name, expected in manifest.items():
            if sha256(root / name) != expected:
                raise CorruptVector('activity checksum mismatch')
        obj = cls()
        obj.metadata = json.loads((root / 'metadata.json').read_text())
        identity = dict(obj.metadata)
        if identity.pop('snapshot_id') != content_hash(identity):
            raise CorruptVector('activity snapshot hash mismatch')
        obj.matrix = np.load(root / 'vectors.npy', allow_pickle=False, mmap_mode='r')
        check_vectors(obj.matrix, len(obj.metadata['entries']))
        obj.fingerprint = obj.metadata['encoder_fingerprint']
        obj.positions = {(r['okved_code'], r['kind']): i for i, r in enumerate(obj.metadata['entries'])}
        if len(obj.positions) != len(obj.matrix):
            raise CorruptVector('duplicate activity entries')
        obj.catalogue = {r['okved_code']: r for r in obj.metadata['catalogue']}
        return obj

    def queries(self, company_id, *, kinds=KINDS):
        code = self.metadata['company_map'][company_id]
        if not kinds or not set(kinds) <= set(KINDS):
            raise ValueError('invalid activity views')
        return [(self.metadata['entries'][self.positions[code, kind]],
                 self.matrix[self.positions[code, kind]]) for kind in kinds]

    def search_company(self, company_id, event_index, *, top_k=20, kinds=KINDS):
        """Max cosine across hypothesis views/windows; scores are not probabilities."""
        if self.fingerprint != event_index.fingerprint:
            raise EncoderMismatch('activity and event vectors must use the same encoder')
        if top_k < 1:
            raise ValueError('positive top_k required')
        scores, matches = {}, {}
        for entry, vector in self.queries(company_id, kinds=kinds):
            for chunk, score in zip(event_index.chunks, event_index.matrix @ vector):
                eid = chunk.snapshot.event_id
                if eid not in scores or float(score) > scores[eid]:
                    scores[eid] = float(score)
                    matches[eid] = dict(view_kind=entry['kind'], okved_code=entry['okved_code'],
                        text=entry['text'], status='hypothesis', chunk_ref=chunk.to_dict())
        ordered = sorted(scores, key=lambda eid: (-scores[eid], eid))[:top_k]
        return [dict(event_id=eid, score=scores[eid], match=matches[eid],
                     title=event_index.snapshots[eid].bundle['event']['title']) for eid in ordered]
