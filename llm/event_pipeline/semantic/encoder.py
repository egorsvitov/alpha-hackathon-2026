"""Local dense embeddings; downloading weights is explicit, never a chat call."""

import importlib.metadata
import re


class SentenceEncoder:
    def __init__(
        self, model="BAAI/bge-m3", *, revision, device="cpu", allow_download=False
    ):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("model_revision_must_be_commit_sha")
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(
            model,
            revision=revision,
            device=device,
            local_files_only=not allow_download,
            trust_remote_code=False,
            prompts={"query": "", "document": ""},
        )
        self.profile = {
            "adapter": "sentence-transformers.v1",
            "model": model,
            "revision": revision,
            "device": device,
            "prompt": "",
            "normalized": True,
            "max_tokens": self.model.max_seq_length,
            "packages": {
                p: importlib.metadata.version(p)
                for p in ("sentence-transformers", "torch", "transformers")
            },
        }

    def encode(self, texts):
        counts = [
            len(
                self.model.tokenizer(text, truncation=False, add_special_tokens=True)[
                    "input_ids"
                ]
            )
            for text in texts
        ]
        if any(n > self.model.max_seq_length for n in counts):
            raise ValueError("embedding_input_too_long_replan_with_smaller_fragments")
        vectors = self.model.encode(
            texts,
            prompt="",
            batch_size=len(texts),
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return vectors.tolist(), counts
