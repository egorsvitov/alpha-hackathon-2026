"""The clean branch can compute/replay with an injected encoder, no old NLP imports."""

import json
from contextlib import nullcontext
from typing import ClassVar

import numpy as np
import pytest

from retriever.experiments import embed_activity_interests as module
from retriever.models import digest


class Cache:
    def writer_lock(self):
        return nullcontext()

    def close(self):
        pass


class Encoder:
    fingerprint_metadata: ClassVar[dict] = {"profile": "fake-test-only"}
    fingerprint = digest(fingerprint_metadata)
    profile: ClassVar[dict] = {"max_tokens": 8192}
    cache = Cache()
    metrics: ClassVar[dict] = {"inferred": 1, "inference_seconds": 1.0}

    def token_lengths(self, texts, *, input_kind):
        return [3 for t in texts]

    def encode_queries(self, texts):
        values = np.zeros((len(texts), 1024), dtype=np.float32)
        values[:, 0] = 1.0
        return values

    def vector_key(self, text, *, input_kind):
        return digest([self.fingerprint, text, input_kind])


def encoder_factory(**kwargs):
    return Encoder()


def setup_source(tmp_path, monkeypatch):
    topic = {
        "row": 0,
        "interest_id": "41.20/materials",
        "description": "Цены материалов",
    }
    news = {
        "fingerprint": Encoder.fingerprint,
        "profile": Encoder.fingerprint_metadata,
        "query_texts": ["query"],
    }
    identity = {"encoder_fingerprint": Encoder.fingerprint}
    monkeypatch.setattr(module, "source", lambda *args: ([topic], identity, news))
    index = tmp_path / "news.json"
    index.write_text(json.dumps(news))
    np.save(tmp_path / "bge-queries.npy", Encoder().encode_queries(["query"]))
    return index


def test_build_and_replay_without_old_pipeline(tmp_path, monkeypatch):
    index = setup_source(tmp_path, monkeypatch)
    output = tmp_path / "encoded"
    report = module.build(
        output, tmp_path, index, encoder_factory=__name__ + ":encoder_factory"
    )
    assert report["status"] == "verified"
    assert report["interest_count"] == 1
    replay = module.build(output, tmp_path, index)
    assert replay["replay_no_inference"]
    assert replay["encoder_fingerprint"] == Encoder.fingerprint


def test_fresh_build_requires_factory_not_missing_legacy_import(tmp_path, monkeypatch):
    index = setup_source(tmp_path, monkeypatch)
    with pytest.raises(
        ValueError, match="fresh_encoding_requires_explicit_encoder_factory"
    ):
        module.build(tmp_path / "fresh", tmp_path, index)
