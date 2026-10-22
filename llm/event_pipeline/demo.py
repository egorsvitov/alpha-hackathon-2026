"""Standalone synthetic example, never contacts a provider."""

import json
import hashlib
from pathlib import Path
from .pipeline import plan_run, extract_mentions, cluster_mentions
from .runtime import OfflineProvider, ProviderResponse
from .output import export_run


def response(data):
    return ProviderResponse(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(data, ensure_ascii=False)},
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30},
        }
    )


class DemoProvider(OfflineProvider):
    def __init__(self):
        super().__init__([])

    def complete(self, request):
        self.requests.append(request)
        data = json.loads(request["messages"][1]["content"])
        if "primary" in data:
            return response({"mentions": [{"block_ids": [data["primary"][0]]}]})
        m = data["new_mention"]
        return response(
            {
                "decisions": [
                    {
                        "cluster_id": c["cluster_id"],
                        "identity": "uncertain",
                        "claim_compatibility": "unknown",
                        "mention_ids": [m["mention_id"], c["mention_ids"][0]],
                        "block_ids": m["block_ids"]
                        + next(
                            v["block_ids"]
                            for v in data["mentions"]
                            if v["mention_id"] == c["mention_ids"][0]
                        ),
                        "reason": "Дата заседания не установлена.",
                    }
                    for c in data["clusters"]
                ]
            }
        )


def synthetic_corpus(root):
    from llm.events.contracts.models import ArticleInput
    from llm.events.contracts.validation import article_sha256

    root = Path(root)
    root.mkdir(parents=True)
    docs = []
    for i, text in enumerate(
        ["ЦБ сохранил ставку 14%.", "Регулятор сохранил ставку 15%."]
    ):
        a = ArticleInput(
            article_id=f"synthetic:{i}",
            source="synthetic",
            url=None,
            title="Решение",
            published_at=None,
            language="ru",
            text=text,
            content_kind="original",
            is_full_original_text=True,
            is_synthetic=True,
        )
        docs.append(
            {
                "document_id": f"d{i}",
                "article": a.model_dump(mode="json"),
                "article_sha256": article_sha256(a),
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "provenance": [
                    {"title_origin": "source", "publication": {"precision": "unknown"}}
                ],
            }
        )
    raw = "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in docs).encode()
    (root / "documents.jsonl").write_bytes(raw)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "prepared-news.v1",
                "corpus_id": hashlib.sha256(raw).hexdigest(),
                "outputs": {"documents.jsonl": hashlib.sha256(raw).hexdigest()},
            }
        )
    )
    return docs


def demo(output):
    root = Path(output)
    if root.exists():
        raise ValueError("demo_exists")
    synthetic_corpus(root / "corpus")
    plan_run(root / "corpus", root / "run")
    p = DemoProvider()
    extract_mentions(root / "run", p, 10)
    cluster_mentions(root / "run", p, 10)
    result = export_run(root / "run", root / "export")
    result["offline_requests"] = len(p.requests)
    return result
