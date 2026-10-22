"""Exact normalized dot-product retrieval; chunks compete within each event."""
import numpy as np
from ranking.contracts import parse_timestamp
from ..representation_contracts import DenseHit, EncoderMismatch, CorruptVector
from ..embedding_cache import vector_bytes, vector_sha256


def search_dense(index, queries, *, top_k=50):
    if top_k<1: raise ValueError('positive top_k required')
    result=[]
    for view in queries.queries:
        if view.encoder_fingerprint != index.fingerprint:raise EncoderMismatch('query/index fingerprint mismatch')
        vector_bytes(view.vector)
        if vector_sha256(view.vector)!=view.vector_sha256:raise CorruptVector('query vector checksum mismatch')
        best={}
        for matrix,refs in index.iter_blocks(1024):
            matrix=np.asarray(matrix)
            if matrix.shape!=(len(refs),384) or matrix.dtype!=np.float32 or not np.isfinite(matrix).all():
                raise CorruptVector('invalid event block')
            if not np.allclose(np.linalg.norm(matrix,axis=1),1,atol=1e-5,rtol=0):raise CorruptVector('unnormalized event block')
            scores=matrix @ view.vector
            for score,ref in zip(scores,refs):
                key=ref.snapshot.event_id
                old=best.get(key)
                if old and old[1].snapshot != ref.snapshot:raise CorruptVector('multiple snapshots of event in index')
                if old is None or score>old[0] or (score==old[0] and ref.chunk_id<old[1].chunk_id):best[key]=(float(score),ref)
        for rank,(uid,(score,ref)) in enumerate(sorted(best.items(),key=lambda p:(-p[1][0],p[0]))[:top_k],1):
            result.append(DenseHit(ref.snapshot,view.kind,view.to_dict(),ref,rank,score,view.feature_refs,
                view.metadata_ref if view.kind.startswith('graph_') else None))
    return result
