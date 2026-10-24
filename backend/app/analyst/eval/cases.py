"""Представительные кейсы для выбора модели и метрик качества (синтетические данные).

Кейсы: ставка ЦБ + кредитный профиль; поставщик угля + металлургия; квоты ЕС;
рубль/ВЭД; нерелевантная новость; противоречащие источники о ставке.
Ожидания — минимальные проверяемые свойства, а не «эталонный текст».
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.seed_data import COMPANIES_DATA, EVENTS_DATA

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    title: str
    company_id: str
    role: str
    event: dict[str, Any]
    expected_decisions: tuple[str, ...]
    expect_bank: bool | None = None
    expect_event_status: tuple[str, ...] = ()
    forbidden_issue_codes: tuple[str, ...] = field(
        default=(
            "number_not_in_data",
            "unsupported_strong_claim",
            "damage_as_fact",
            "identification",
            "bank_invented",
            "revenue_period",
            "estimate_number_not_in_data",
        )
    )


def company(company_id: str) -> dict[str, Any]:
    return next(c for c in COMPANIES_DATA if c["company_id"] == company_id)


def seed_event(event_id: str) -> dict[str, Any]:
    return dict(next(e for e in EVENTS_DATA if e["event_id"] == event_id))


def bundle_event(fixture: str) -> dict[str, Any]:
    bundle = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    ev = bundle["event"]
    return {
        "event_id": ev["event_id"], "title": ev["title"], "date": ev["first_published_at"], "topic": "rates",
        "topic_label": "Ставки и кредит", "sources_count": len(bundle.get("sources") or []) or 1, "is_synthetic": True,
        "bundle": bundle,
    }


def eval_cases() -> list[EvalCase]:
    return [
        EvalCase("rate_credit", "Ставка ЦБ + кредитный профиль", "SYNTHETIC_U_1042", "cfo", seed_event("e1_key_rate"),
                 ("include", "watch"), expect_bank=True, expect_event_status=("occurred",)),
        EvalCase("coal_supplier", "Поставщик угля + металлургия", "SYNTHETIC_U_1042", "ceo", seed_event("e5_coke_supplier"),
                 ("include", "watch"), expect_bank=True, expect_event_status=("occurred",)),
        EvalCase("eu_quota", "Квоты ЕС на стальной прокат", "SYNTHETIC_U_1042", "ceo", seed_event("e2_eu_steel_quota"),
                 ("include", "watch"), expect_bank=False),
        EvalCase("rub_fx", "Ослабление рубля + ВЭД", "SYNTHETIC_U_2218", "treasurer", seed_event("e3_rub_weak"),
                 ("include", "watch"), expect_bank=False, expect_event_status=("occurred", "mixed")),
        EvalCase("irrelevant", "Нерелевантная новость (шахматы)", "SYNTHETIC_U_3305", "cfo", seed_event("e7_noise_sports"),
                 ("exclude",), expect_bank=False),
        EvalCase("conflict", "Противоречащие источники о ставке", "SYNTHETIC_U_1042", "cfo", bundle_event("rate_conflict.json"),
                 ("include", "watch"), expect_bank=None, expect_event_status=("mixed", "unclear", "occurred")),
    ]
