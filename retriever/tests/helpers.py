from pathlib import Path
import hashlib
import numpy as np
from ranking.profiles import CompanyRepository
from retriever.representation_contracts import InputTooLong

FIXTURE=Path('ranking/tests/fixtures/profiles/companies.csv')
class FakeEncoder:
    fingerprint='synthetic-hash-encoder.v1'
    def __init__(self):self.calls=0
    def token_lengths(self,texts,*,input_kind):return [(len(t)+3)//4+3 for t in texts]
    def _encode(self,texts,prefix):
        output=[]
        for text in texts:
            if (len(text)+3)//4+3>512:raise InputTooLong(text)
            raw=hashlib.shake_256((prefix+text).encode()).digest(384)
            v=np.frombuffer(raw,dtype=np.uint8).astype(np.float32)-128
            output.append(v/np.linalg.norm(v));self.calls+=1
        return np.array(output,dtype=np.float32).reshape(-1,384)
    def encode_queries(self,texts):return self._encode(texts,'query: ')
    def encode_documents(self,texts):return self._encode(texts,'passage: ')
