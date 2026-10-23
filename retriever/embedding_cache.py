"""Immutable SQLite vector objects, shared independently of company provenance."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import hashlib
import json
import sqlite3
import numpy as np
from .representation_contracts import BuildLocked, CorruptVector, VectorCollision, content_hash, text_hash


def vector_key(fingerprint, text, *, input_kind='query'):
    if input_kind not in {'query', 'document'}:
        raise ValueError('unsupported input kind')
    return content_hash(dict(encoder_fingerprint=fingerprint, input_kind=input_kind, normalized_text_hash=text_hash(text)))


def exact_vector_key(fingerprint, text, *, input_kind='query'):
    """Raw-news policy: exact UTF-8 text, independently of the B6 NFC policy."""
    if input_kind not in {'query', 'document'}:
        raise ValueError('unsupported input kind')
    return content_hash(dict(encoder_fingerprint=fingerprint, input_kind=input_kind,
                            exact_utf8_sha256=hashlib.sha256(text.encode()).hexdigest()))


def vector_bytes(vector, dimension=384):
    arr = np.asarray(vector)
    if arr.dtype != np.float32 or arr.shape != (dimension,) or not np.isfinite(arr).all() or abs(float(np.linalg.norm(arr)) - 1) > 1e-5:
        raise CorruptVector(f'expected finite normalized float32[{dimension}]')
    return arr.astype('<f4', copy=False).tobytes()


def vector_sha256(vector, dimension=384):
    return hashlib.sha256(vector_bytes(vector, dimension)).hexdigest()


class VectorCache:
    def __init__(self, path, *, dimension=384):
        if type(dimension) is not int or dimension < 1:
            raise ValueError('positive dimension required')
        self.dimension = dimension
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=0)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('CREATE TABLE IF NOT EXISTS objects (key TEXT PRIMARY KEY, data BLOB NOT NULL, sha256 TEXT NOT NULL, metadata TEXT NOT NULL)')
        self.connection.commit()

    @contextmanager
    def writer_lock(self):
        with self.path.with_suffix(self.path.suffix + '.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BuildLocked('another cache/build writer holds the lock') from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def get(self, vector_key):
        row = self.connection.execute('SELECT data,sha256 FROM objects WHERE key=?', (vector_key,)).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row[0]).hexdigest() != row[1]:
            raise CorruptVector('object checksum mismatch', context={'key': vector_key})
        vector = np.frombuffer(row[0], dtype='<f4').copy()
        vector_bytes(vector, self.dimension)
        return vector

    def metadata(self, key):
        row = self.connection.execute('SELECT metadata FROM objects WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, vector_key, vector, *, metadata):
        # Cache records must never contain source/company provenance.
        if any(k in metadata for k in ('company_id', 'profile_version', 'feature_refs', 'source_provenance')):
            raise ValueError('company provenance belongs in mapping')
        data = vector_bytes(vector, self.dimension)
        old = self.get(vector_key)
        if old is not None:
            if vector_bytes(old, self.dimension) != data or self.metadata(vector_key) != metadata:
                raise VectorCollision('immutable vector object collision')
            return
        try:
            with self.connection:
                self.connection.execute('INSERT INTO objects VALUES (?,?,?,?)', (vector_key, data, hashlib.sha256(data).hexdigest(), json.dumps(metadata, sort_keys=True)))
        except sqlite3.OperationalError as exc:
            raise BuildLocked(str(exc)) from exc

    def close(self):
        self.connection.close()


class CachedEncoder:
    """Restartable exact-input wrapper; shared engine, no company-specific vectors."""
    def __init__(self, encoder, path, *, dimension, batch_size=8):
        self.encoder=encoder
        self.cache=VectorCache(path,dimension=dimension)
        self.fingerprint=encoder.fingerprint
        self.fingerprint_metadata=encoder.fingerprint_metadata
        self.batch_size=batch_size
        self.metrics=dict(hits=0,inferred=0)

    def token_lengths(self,texts,*,input_kind):
        return self.encoder.token_lengths(texts,input_kind=input_kind)

    def _encode(self,texts,kind):
        values={};pending=[]
        for text in dict.fromkeys(texts):
            v=self.cache.get(exact_vector_key(self.fingerprint,text,input_kind=kind))
            if v is None:pending.append(text)
            else:values[text]=v;self.metrics['hits']+=1
        method=self.encoder.encode_documents if kind=='document' else self.encoder.encode_queries
        for off in range(0,len(pending),self.batch_size):
            batch=pending[off:off+self.batch_size];vectors=method(batch)
            for text,v in zip(batch,vectors):
                self.cache.put(exact_vector_key(self.fingerprint,text,input_kind=kind),v,
                    metadata=dict(encoder_fingerprint=self.fingerprint,input_kind=kind,exact_text_sha256=hashlib.sha256(text.encode()).hexdigest()))
                values[text]=v;self.metrics['inferred']+=1
        return np.stack([values[t] for t in texts]) if texts else np.empty((0,self.cache.dimension),np.float32)

    def encode_queries(self,texts):return self._encode(texts,'query')
    def encode_documents(self,texts):return self._encode(texts,'document')
