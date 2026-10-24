"""Общие помощники тестов аналитика (без сети, на FakeLLM)."""

from __future__ import annotations

import copy
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.analyst.input_adapter import bank_fragments_from_evidence, build_analyst_input, company_context
from app.analyst.llm_gateway import BudgetLedger, ChatRequest, FakeLLM
from app.analyst.service import AnalystService
from app.analyst.settings import AnalystSettings
from app.services.seed_data import COMPANIES_DATA, EVENTS_DATA, SHARED_EVIDENCE

AS_OF = "2026-10-22T12:00:00+03:00"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EVAL_FIXTURES = Path(__file__).resolve().parents[2] / "app" / "analyst" / "eval" / "fixtures"


def seed_company(company_id: str = "SYNTHETIC_U_1042") -> dict[str, Any]:
    return copy.deepcopy(next(c for c in COMPANIES_DATA if c["company_id"] == company_id))


def seed_event(event_id: str) -> dict[str, Any]:
    return copy.deepcopy(next(e for e in EVENTS_DATA if e["event_id"] == event_id))


def bundle_event(bundle: dict[str, Any], **extra: Any) -> dict[str, Any]:
    ev = bundle["event"]
    out = {
        "event_id": ev["event_id"], "title": ev.get("title") or "Событие", "date": ev.get("first_published_at") or "2026-10-20",
        "topic": "other", "topic_label": "Тест", "sources_count": len(bundle.get("sources") or []) or 1, "is_synthetic": True,
        "bundle": bundle,
    }
    out.update(extra)
    return out


def load_fixture_bundle(name: str) -> dict[str, Any]:
    return json.loads((EVAL_FIXTURES / name).read_text(encoding="utf-8"))


def make_bundle(event_id: str, title: str, claims: list[dict[str, Any]], *, published: str = "2026-10-20T10:00:00+03:00",
                conflicts: list[dict[str, Any]] | None = None, cluster_id: str | None = None) -> dict[str, Any]:
    """Мини-EventBundle: каждое утверждение — {text, modality?, polarity?}."""
    out_claims, evidence = [], []
    for i, c in enumerate(claims, start=1):
        ev_id = f"{event_id}:ev{i}"
        out_claims.append({
            "claim_id": f"{event_id}:c{i}", "text": c["text"], "modality": c.get("modality", "asserted"),
            "polarity": c.get("polarity", "affirmed"), "stage": c.get("stage"), "scenario": c.get("scenario"),
            "time": None, "measurements": c.get("measurements", []), "evidence_ids": [ev_id],
        })
        evidence.append({"evidence_id": ev_id, "source_id": f"{event_id}:src", "quote": c["text"], "field": "text"})
    bundle = {
        "schema_version": "0.1",
        "event": {"event_id": event_id, "event_version": 1, "event_type": "other", "title": title, "first_published_at": published,
                  "quality": "complete"},
        "claims": out_claims,
        "sources": [{"source_id": f"{event_id}:src", "title": title, "publisher": "Тестовое издание", "published_at": published[:10],
                     "is_synthetic": True}],
        "evidence": evidence,
        "conflicts": conflicts or [],
        "relations": [],
        "limitations": [],
    }
    if cluster_id:
        bundle["cluster_id"] = cluster_id
    return bundle


def feature_alias(company: dict[str, Any], feature_id: str) -> str:
    ctx = company_context(company)
    for i, f in enumerate(ctx.verified_facts, start=1):
        if f.feature_id == feature_id:
            return f"F{i}"
    raise KeyError(feature_id)


def draft(**overrides: Any) -> dict[str, Any]:
    """Корректный ответ модели по умолчанию (include, ссылки на C1 и F1)."""
    base = {
        "decision": "include",
        "exclude_reason": None,
        "event_status": "occurred",
        "impact_direction": "negative",
        "impact_channels": ["costs"],
        "materiality": "medium",
        "urgency": "medium",
        "headline": "Событие может повлиять на компанию",
        "what_happened": [{"text": "Источник сообщает о событии.", "kind": "fact", "claim_ids": ["C1"]}],
        "company_relevance": {"text": "Событие касается отрасли компании.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]},
        "impact_mechanism": {"text": "Влияние может пройти через издержки.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]},
        "risks": [{"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]}],
        "opportunities": [],
        "next_steps": [{"text": "Проверить зависимость от события.", "roles": ["cfo"], "feature_ids": ["F1"]}],
        "recommended_checks": [],
        "unknowns": [],
        "impact_estimate": None,
        "bank_assessment": {"status": "not_found", "bank_chunk_ids": []},
        "ai_conclusion": {"text": "Стоит проверить влияние на издержки.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]},
        "inference_status": "supported",
    }
    base.update(overrides)
    return base


def exclude_draft(reason: str = "Нет связи с бизнесом компании") -> dict[str, Any]:
    return {
        "decision": "exclude", "exclude_reason": reason, "event_status": "occurred", "headline": "Событие не касается компании",
        "what_happened": [{"text": "Источник сообщает о событии.", "kind": "fact", "claim_ids": ["C1"]}],
    }


def responder_by_content(rules: list[tuple[str, dict[str, Any]]], default: dict[str, Any] | None = None) -> Callable[[ChatRequest], str]:
    """Отдаёт черновик по первому совпадению подстроки в сообщении пользователя."""

    def respond(req: ChatRequest) -> str:
        content = req.messages[-1]["content"] if req.messages else ""
        for needle, d in rules:
            if needle in content:
                return json.dumps(d, ensure_ascii=False)
        return json.dumps(default or draft(), ensure_ascii=False)

    return respond


def make_service(mode: str = "llm", responder: Any = None, *, tmp: Path | None = None, bundled: Path | None = None,
                 extra_env: dict[str, str] | None = None, with_llm: bool = True) -> tuple[AnalystService, FakeLLM | None, Path]:
    tmp = tmp or Path(tempfile.mkdtemp(prefix="analyst_test_"))
    env = {
        "ANALYST_MODE": mode,
        "ANALYST_CACHE_DIR": str(tmp / "runtime"),
        "ANALYST_BUNDLED_CACHE_DIR": str(bundled or (tmp / "bundled")),
        "ANALYST_AS_OF": AS_OF,
        "ANALYST_MAX_PARALLEL": "1",
        "ANALYST_CHECK_KEY_INFO": "false",
    }
    env.update(extra_env or {})
    settings = AnalystSettings.from_env(env)
    fake = None
    if with_llm:
        fake = FakeLLM(responder if responder is not None else (lambda req: json.dumps(draft(), ensure_ascii=False)),
                       ledger=BudgetLedger(settings.daily_budget_rub))
    return AnalystService(settings, llm=fake), fake, tmp


def analyst_input(company: dict[str, Any], role: str, events: list[dict[str, Any]], relevance: dict[str, Any] | None = None):
    return build_analyst_input(
        company, role, events, as_of=datetime.fromisoformat(AS_OF), relevance_by_event=relevance or {},
        bank_corpus=bank_fragments_from_evidence(SHARED_EVIDENCE),
    )
