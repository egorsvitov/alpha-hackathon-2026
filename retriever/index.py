"""Immutable exact event index; company vectors remain separate query artifacts."""
from pathlib import Path
from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import json
import os
import tempfile
import numpy as np
from ranking.contracts import StoredEvent, parse_timestamp
from .representation_contracts import SnapshotKey, ChunkRef, content_hash, EncoderMismatch, CorruptVector
from .eligibility import eligibility_reason
from .event_views import build_event_views, POLICY_VERSION
from .embedding_cache import vector_key


def _dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')


def build_event_index(store, *, encoder, output_path, as_of, window_start, window_end, cache=None):
    with cache.writer_lock() if cache else nullcontext():
        return _build_event_index(store,encoder=encoder,output_path=output_path,as_of=as_of,window_start=window_start,window_end=window_end,cache=cache)


def _build_event_index(store, *, encoder, output_path, as_of, window_start, window_end, cache=None):
    stamps = [parse_timestamp(t).isoformat() for t in (as_of, window_start, window_end)]
    as_of, window_start, window_end = stamps
    if parse_timestamp(window_start) > parse_timestamp(window_end) or parse_timestamp(window_end) > parse_timestamp(as_of):
        raise ValueError('invalid index window')
    snapshots, documents, refs, vectors, excluded = {}, {}, [], [], {}
    # EventStore selects the latest visible version BEFORE availability filtering.
    for stored in store.list_latest(as_of):
        reason = eligibility_reason(stored, as_of=as_of, window_start=window_start, window_end=window_end)
        if reason:
            excluded[stored.event_id] = reason
            continue
        lexical, texts, chunk_refs = build_event_views(stored, encoder=encoder)
        snapshots[stored.event_id] = stored.to_dict()
        documents[stored.event_id] = lexical
        for text in texts:
            key = vector_key(encoder.fingerprint, text, input_kind='document')
            vector = cache.get(key) if cache else None
            if vector is None:
                vector = encoder.encode_documents([text])[0]
                if cache:
                    from .representation_contracts import text_hash
                    cache.put(key, vector, metadata={'encoder_fingerprint':encoder.fingerprint,'input_kind':'document','normalized_text_hash':text_hash(text)})
            vectors.append(vector)
        refs.extend(r.to_dict() for r in chunk_refs)
    matrix = np.asarray(vectors, dtype=np.float32).reshape((-1,384))
    metadata = dict(as_of=as_of, window_start=window_start, window_end=window_end,
        fingerprint=encoder.fingerprint, policy_version=POLICY_VERSION, snapshots=snapshots,
        documents=documents, chunks=refs, excluded=excluded)
    metadata['index_snapshot_id'] = content_hash(metadata)
    output = Path(output_path)
    if output.exists():
        existing = EventIndex.open(output)
        if existing.index_snapshot_id != metadata['index_snapshot_id']:
            raise ValueError('immutable event index already exists with different content')
        return existing
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.event-index-') as temp:
        tmp=Path(temp)
        np.save(tmp/'vectors.npy', matrix, allow_pickle=False)
        _dump(tmp/'metadata.json',metadata)
        manifest = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp.iterdir()}
        _dump(tmp/'manifest.json',manifest)
        for p in tmp.iterdir():
            with p.open('rb') as f: os.fsync(f.fileno())
        # Validate before publish; readers never see a partially built directory.
        EventIndex.open(tmp)
        os.rename(tmp, output)
    return EventIndex.open(output)


class EventIndex:
    @classmethod
    def open(cls, path):
        obj=cls();obj.path=Path(path)
        manifest=json.loads((obj.path/'manifest.json').read_text())
        if set(manifest) != {'vectors.npy','metadata.json'}:
            raise CorruptVector('invalid event manifest files')
        for name, sha in manifest.items():
            if hashlib.sha256((obj.path/name).read_bytes()).hexdigest()!=sha:
                raise CorruptVector('event index checksum mismatch')
        obj.metadata=json.loads((obj.path/'metadata.json').read_text())
        identity=dict(obj.metadata); identity.pop('index_snapshot_id')
        if content_hash(identity)!=obj.metadata['index_snapshot_id']:
            raise CorruptVector('event index identity mismatch')
        for name in ('fingerprint','index_snapshot_id','as_of','window_start','window_end'):
            setattr(obj,name,obj.metadata[name])
        obj.matrix=np.load(obj.path/'vectors.npy',mmap_mode='r',allow_pickle=False)
        obj.chunks=[ChunkRef(SnapshotKey(**r['snapshot']),r['chunk_id'],r['claim_refs'],r['offsets']) for r in obj.metadata['chunks']]
        if obj.matrix.dtype!=np.float32 or obj.matrix.shape!=(len(obj.chunks),384):
            raise CorruptVector('invalid event matrix shape or dtype')
        if not np.isfinite(obj.matrix).all() or (len(obj.matrix) and not np.allclose(np.linalg.norm(obj.matrix,axis=1),1,atol=1e-5,rtol=0)):
            raise CorruptVector('invalid normalized event vectors')
        obj.snapshots={}
        for uid,raw in obj.metadata['snapshots'].items():
            obj.snapshots[uid]=StoredEvent(raw['event_id'],raw['event_version'],raw['payload_sha256'],parse_timestamp(raw['received_at']),raw['source_mode'],raw['ingest_id'],raw['bundle'])
            if eligibility_reason(obj.snapshots[uid],as_of=obj.as_of,window_start=obj.window_start,window_end=obj.window_end):
                raise CorruptVector('ineligible snapshot in published index')
        for r in obj.chunks:
            stored=obj.resolve_snapshot(r.snapshot)
            from ranking.contracts import resolve_pointer
            for ref in r.claim_refs:
                if (ref['event_id'],ref['event_version'])!=(stored.event_id,stored.event_version) or ref['claim_id'] not in {c['claim_id'] for c in stored.bundle['claims']}:
                    raise CorruptVector('chunk claim reference mismatch')
            for span in r.offsets:
                text=resolve_pointer(stored.bundle,span['path'])
                if not isinstance(text,str) or not (0<=span['start']<span['end']<=len(text)):
                    raise CorruptVector('invalid chunk source offsets')
        if set(obj.metadata['documents'])!=set(obj.snapshots):raise CorruptVector('lexical/snapshot universe mismatch')
        obj.documents=obj.metadata['documents']
        return obj

    def iter_blocks(self,max_rows=1024):
        if max_rows<1: raise ValueError('positive block size required')
        for start in range(0,len(self.chunks),max_rows):
            yield self.matrix[start:start+max_rows],self.chunks[start:start+max_rows]

    def resolve_snapshot(self,ref):
        if isinstance(ref,dict): ref=SnapshotKey(**ref)
        stored=self.snapshots[ref.event_id]
        if (stored.event_version,stored.payload_sha256)!=(ref.event_version,ref.payload_sha256):
            raise CorruptVector('event snapshot hash/version mismatch')
        return stored
