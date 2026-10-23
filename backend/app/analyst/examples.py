"""Библиотека учебных примеров (few-shot) для LLM-аналитика.

Данные — ``app/analyst/examples/*.json`` (по одному примеру на файл) и манифест открытых
источников ``examples/sources.json``. Компании в примерах синтетические, события — по открытым
публикациям (Банк России, Минфин, Еврокомиссия, Всемирный банк) на дату примера.

* ``select_examples`` — детерминированный выбор не более ``max_n`` примеров в бюджете токенов;
  антипримеры только с ``include_anti=True``; при слабой связи события и ``max_n >= 2``
  обязательно попадает пример ``exclude``.
* ``format_examples_block`` / ``examples_block`` — компактный текст для вставки в промпт.
* ``python -m app.analyst.examples --rebuild`` — пересчитать ``expected_output`` (AnalystOutput)
  по черновику через текущий QA и сборку аналитика (без LLM).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"

TOPIC_ALIASES = {
    "rate": "rates", "key_rate": "rates", "policy_rate": "rates", "credit": "rates",
    "fx_rate": "fx", "currency": "fx",
    "sanction": "sanctions", "sanction_or_trade_restriction": "sanctions", "restriction": "sanctions", "quota": "sanctions",
    "tax": "taxes", "tax_change": "taxes", "regulation": "taxes",
    "commodity": "commodities", "price_change": "commodities", "raw_materials": "commodities",
    "market_indicator": "inflation",
}
KNOWN_TOPICS = {"rates", "fx", "sanctions", "trade", "taxes", "commodities", "inflation", "industry", "counterparty", "other"}
DECISION_ORDER = {"include": 0, "watch": 1, "exclude": 2}

BLOCK_HEADER = (
    "ПРИМЕРЫ РАЗБОРА (учебные). Компании в примерах синтетические, события взяты из открытых источников "
    "на дату примера и могут быть устаревшими. Это пример, не факты о текущей компании: переноси только приём "
    "рассуждения, ID F/C/B примера относятся только к примеру."
)


@dataclass(frozen=True)
class Example:
    example_id: str
    slug: str
    title: str
    kind: str  # reference | anti
    topics: tuple[str, ...]
    role: str
    decision: str
    teaches: tuple[str, ...]
    defect: dict[str, Any] | None
    source_ids: tuple[str, ...]
    input: dict[str, Any]
    draft: dict[str, Any]
    expected_output: dict[str, Any] | None
    path: Path

    @property
    def is_anti(self) -> bool:
        return self.kind == "anti"


@lru_cache(maxsize=1)
def load_examples() -> tuple[Example, ...]:
    out = []
    for p in sorted(EXAMPLES_DIR.glob("ex*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out.append(
            Example(
                example_id=d["example_id"], slug=d["slug"], title=d["title"], kind=d["kind"], topics=tuple(d["topics"]),
                role=d["role"], decision=d["decision"], teaches=tuple(d.get("teaches") or ()), defect=d.get("defect"),
                source_ids=tuple(d.get("source_ids") or ()), input=d["input"], draft=d["draft"],
                expected_output=d.get("expected_output"), path=p,
            )
        )
    return tuple(out)


def load_sources() -> dict[str, Any]:
    return json.loads((EXAMPLES_DIR / "sources.json").read_text(encoding="utf-8"))


def normalize_topic(topic: str | None) -> str:
    t = (topic or "other").strip().lower()
    return TOPIC_ALIASES.get(t, t)


def estimate_tokens(text: str) -> int:
    """Грубая оценка: байты UTF-8 / 4 (с запасом для кириллицы)."""
    return math.ceil(len(text.encode("utf-8")) / 4)


# =========================================================================== форматирование


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: _compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [x for x in (_compact(v) for v in value) if x not in (None, "", [], {})]
    return value


def _referenced(draft: dict[str, Any], prefix: str) -> set[str]:
    refs: set[str] = set()

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("claim_ids", "feature_ids", "bank_chunk_ids") and isinstance(v, list):
                    refs.update(str(x) for x in v if str(x).startswith(prefix))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(draft)
    return refs


def format_example(ex: Example) -> str:
    inp, draft = ex.input, ex.draft
    cand = inp["candidates"][0]
    bundle = cand["analysis_projection"]["event_bundles"][0]
    feats = _referenced(draft, "F") | {"F1"}
    facts = [
        f"F{i} {f['description']}: {f['value']}"
        for i, f in enumerate(inp["company_context"]["verified_facts"], start=1)
        if f"F{i}" in feats
    ]
    used_claims = _referenced(_shorten(draft), "C") | {"C1"}
    claims = []
    for i, c in enumerate(bundle["claims"], start=1):
        if f"C{i}" not in used_claims:
            continue
        tag = "" if (c.get("modality") or "asserted") == "asserted" else f"[{c['modality']}] "
        claims.append(f"C{i} {tag}{c['text']}")
    lines = [
        f"--- {'Антипример (так НЕ отвечать)' if ex.is_anti else 'Пример'} {ex.example_id} · {ex.title} · роль {ex.role}",
        "Профиль: " + "; ".join(facts),
        f"Событие {(bundle['event'].get('first_published_at') or '')[:10]}: " + " | ".join(claims),
    ]
    reasons = [s["reason"] for s in cand["match_context"].get("signals") or []]
    if reasons:
        lines.append("Сигнал отбора: " + "; ".join(reasons))
    for i, b in enumerate(cand.get("bank_fragments") or [], start=1):
        lines.append(f"B{i} ({b.get('published_at')}, {b.get('applicability')}, синтетический учебный): «{b['quote']}»")
    if ex.is_anti and ex.defect:
        bad = {k: draft[k] for k in ex.defect.get("bad_fields") or [] if k in draft}
        lines.append("Плохой ответ (фрагмент): " + _j(_compact(bad)))
        lines.append("Что не так: " + "; ".join(ex.defect.get("what_is_wrong") or []))
        lines.append("Как правильно: " + str(ex.defect.get("fix") or ""))
    else:
        lines.append("Ответ: " + _j(_compact(_shorten(draft))))
    return "\n".join(lines)


def _shorten(draft: dict[str, Any]) -> dict[str, Any]:
    """Для промпта: по одному пункту в списках, без recommended_checks и unknowns (экономия токенов)."""
    out = {}
    for k, v in draft.items():
        if k in ("recommended_checks", "unknowns"):
            continue
        if isinstance(v, list) and k != "impact_channels":
            v = v[:1]
        out[k] = v
    return out


def format_examples_block(examples: list[Example]) -> str:
    if not examples:
        return ""
    return BLOCK_HEADER + "\n" + "\n".join(format_example(e) for e in examples)


# =========================================================================== выбор


def _score(ex: Example, topic: str, role: str | None, decision: str | None) -> int:
    s = 0
    if ex.topics and ex.topics[0] == topic:
        s += 4
    elif topic in ex.topics:
        s += 2
    if role and ex.role == role:
        s += 1
    if decision and ex.decision == decision:
        s += 2
    return s


def select_examples(
    topic: str | None,
    role: str | None = None,
    decision: str | None = None,
    max_n: int = 2,
    max_tokens: int = 1200,
    *,
    weak_match: bool | None = None,
    include_anti: bool = False,
) -> list[Example]:
    """Детерминированно выбирает не более ``max_n`` примеров, чей блок укладывается в ``max_tokens``.

    ``weak_match`` — событие слабо связано с компанией (None: выводится из ``decision`` in
    watch/exclude или неизвестной темы); тогда при ``max_n >= 2`` обязательно берётся пример exclude.
    Антипримеры попадают в выбор только при ``include_anti=True``.
    """
    if max_n <= 0:
        return []
    t = normalize_topic(topic)
    weak = weak_match if weak_match is not None else (decision in ("watch", "exclude") or t not in KNOWN_TOPICS or t == "other")
    pool = [e for e in load_examples() if include_anti or not e.is_anti]

    def key(e: Example) -> tuple:
        return (-_score(e, t, role, decision), e.is_anti, DECISION_ORDER.get(e.decision, 9), e.example_id)

    ranked = sorted(pool, key=key)
    chosen: list[Example] = []

    def try_add(e: Example) -> bool:
        if len(chosen) >= max_n or e in chosen:
            return False
        if estimate_tokens(format_examples_block(chosen + [e])) > max_tokens:
            return False
        chosen.append(e)
        return True

    if weak and max_n >= 2:
        # Лучшая пара «содержательный пример + exclude», которая влезает в бюджет; иначе — один exclude.
        excludes = [e for e in ranked if e.decision == "exclude" and not e.is_anti]
        others = [e for e in ranked if e.decision != "exclude"]
        pair = next(
            ((o, x) for o in others for x in excludes if estimate_tokens(format_examples_block([o, x])) <= max_tokens),
            None,
        )
        if pair:
            chosen.extend(pair)
        else:
            for x in excludes:
                if try_add(x):
                    break
    for e in ranked:
        if len(chosen) >= max_n:
            break
        try_add(e)
    return sorted(chosen, key=lambda e: (e.is_anti, DECISION_ORDER.get(e.decision, 9), key(e)))


def examples_block(topic: str | None, role: str | None = None, decision: str | None = None, max_n: int = 2,
                   max_tokens: int = 1200, **kw: Any) -> str:
    return format_examples_block(select_examples(topic, role, decision, max_n, max_tokens, **kw))


# =========================================================================== эталонный выход


def analyst_input(ex: Example):
    from app.analyst.schemas import AnalystInput

    return AnalystInput.model_validate(ex.input)


def run_qa(ex: Example):
    """QA (``qa.validate_draft``) черновика примера на его входе."""
    from app.analyst.qa import validate_draft
    from app.analyst.schemas import LLMDraft
    from app.analyst.view import build_prompt_view

    inp = analyst_input(ex)
    view = build_prompt_view(inp, inp.candidates[0])
    draft = LLMDraft.model_validate(view.realias_draft(ex.draft))
    return validate_draft(draft, view, inp.request.role, as_of=inp.request.as_of), draft


def rebuild_expected_output(ex: Example) -> dict[str, Any]:
    """AnalystOutput по черновику примера: QA + сборка аналитика без вызова LLM."""
    from app.analyst.analyst import BusinessAnalyst

    inp = analyst_input(ex)
    cand = inp.candidates[0]
    qa, draft = run_qa(ex)
    analyst = BusinessAnalyst(None, model="few-shot-reference")
    outcome = analyst._build(inp, cand, draft, qa, [])
    outcome.run.generated_by = "cached"
    out = analyst.assemble(inp, {cand.candidate_id: outcome})
    for run in out.run_metadata.candidates:
        run.trace = []
    return out.model_dump(mode="json")


def _rebuild_all() -> None:
    for ex in load_examples():
        d = json.loads(ex.path.read_text(encoding="utf-8"))
        d["expected_output"] = None if ex.is_anti else rebuild_expected_output(ex)
        ex.path.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(ex.path.name)


if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--rebuild" in sys.argv:
        _rebuild_all()
    else:
        topic = sys.argv[1] if len(sys.argv) > 1 else "rates"
        block = examples_block(topic)
        print(block)
        print(f"\n~{estimate_tokens(block)} токенов")
