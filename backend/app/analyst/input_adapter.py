"""Независимый адаптер входа: наши данные → ``AnalystInput.v1-draft``.

Поддерживаются два формата профиля:

* UI-профиль из ``seed_data.COMPANIES_DATA`` (поля ``products``, ``fts``, ``counterparties``…);
* нормализованный профиль ранкера (``ranking/``, ``profiles.json``) со словарём
  ``features[feature_id] = {path, value, quality, origin}``.

События:

* демо-события seed (``NewsEvent``) + их структурные фрагменты из ``data/seed_bundles.json``;
* EventBundle v0.1/v0.2 (например, из ``ml_bridge.load_live_event_bundles``);
* новость, загруженная пользователем (``POST /news``): утверждения строятся из заголовка
  и текста, источник помечается как непроверенный.

Промпты и бизнес-логика не зависят от этого модуля: при смене схемы входа правится
только адаптер. Неизвестное значение — ``None``/``unknowns``, а не ``False``/``0``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from app.analyst.schemas import (
    AnalysisProjection,
    AnalystInput,
    AnalystRequest,
    BankFragment,
    Candidate,
    CompanyContext,
    EconomicExposure,
    EventBundleRef,
    GraphContext,
    MatchContext,
    MatchSignal,
    Origin,
    ProjectionBundle,
    RelatedEntity,
    VerifiedFact,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
SEED_ORIGIN = Origin(type="seed_profile", ref="backend/app/services/seed_data.py#COMPANIES_DATA")

PRODUCT_LABELS = {
    "rko": ("CL_PRODUCT_RKO_MARK", "Расчётно-кассовое обслуживание"),
    "guarantees": ("CL_PRODUCT_GAR_MARK", "Банковские гарантии"),
    "loans": ("CL_PRODUCT_LOA_MARK", "Кредитные продукты"),
    "leasing": ("CL_PRODUCT_LEA_MARK", "Лизинг"),
    "deposits": ("CL_PRODUCT_DEP_MARK", "Депозиты"),
    "payroll": ("CL_PRODUCT_ZPP_MARK", "Зарплатный проект"),
    "factoring": ("FACTORING_MARK", "Факторинг"),
}

PRODUCT_EXPOSURES = {
    "loans": ("Стоимость и доступность заёмного финансирования", "medium"),
    "leasing": ("Стоимость лизинга и обновления основных средств", "low"),
    "deposits": ("Доходность размещения свободных средств", "medium"),
    "factoring": ("Оборотный капитал и дебиторская задолженность", "low"),
    "guarantees": ("Обеспечение контрактов банковскими гарантиями", "low"),
}

RANKING_PATH_LABELS = {
    "/okved/code": "Основной ОКВЭД (код)",
    "/okved/name": "Основной ОКВЭД (вид деятельности)",
    "/industry": "Отрасль (по ОКВЭД)",
    "/region": "Регион обслуживания",
    "/city": "Город обслуживания",
    "/age_band": "Возраст компании",
    "/employees_band": "Численность сотрудников",
    "/development_stage": "Стадия развития",
    "/group_id": "Группа компаний",
    "/client_type": "Тип клиента",
    "/fts_present": "Есть таможенные декларации (ВЭД)",
    "/countries": "Страны ВЭД (без направления экспорт/импорт)",
    "/revenue/raw": "Выручка за прошлый отчётный год (как в данных)",
    "/products/rko": "Расчётно-кассовое обслуживание",
    "/products/guarantee": "Банковские гарантии",
    "/products/loan": "Кредитные продукты",
    "/products/leasing": "Лизинг",
    "/products/deposit": "Депозиты",
    "/products/payroll": "Зарплатный проект",
    "/products/factoring": "Факторинг",
}
RANKING_SKIP_PREFIXES = ("/company_id", "/source_snapshot", "/display_name", "/is_synthetic", "/revenue/unit", "/revenue/period")

STANDARD_UNKNOWNS = [
    "Условия действующих кредитных договоров (тип ставки, сроки, объём долга) в профиле неизвестны.",
    "Структура закупок и продаж, договоры с контрагентами неизвестны; платежи подтверждают только факт расчётов, не тип отношений.",
    "Выручка известна только за прошлый отчётный год; квартальные и месячные показатели неизвестны.",
]


# =========================================================================== утилиты


def stable_hash(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_dt(value: str | None) -> datetime | None:
    """ISO-дата или дата-время; без пояса — считаем московским временем."""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        if len(v) == 10:
            dt = datetime.fromisoformat(v + "T00:00:00")
        else:
            dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
    return dt


def _yes_no(value: Any) -> str:
    return {True: "есть", False: "нет"}.get(value, "неизвестно")


# =========================================================================== профиль компании


def company_context_from_seed(company: dict[str, Any]) -> CompanyContext:
    facts: list[VerifiedFact] = []
    unknowns: list[str] = list(STANDARD_UNKNOWNS)
    exposures: list[EconomicExposure] = []
    related: list[RelatedEntity] = []

    def add(fid: str, field: str, value: Any, description: str, quality: str = "observed") -> None:
        facts.append(
            VerifiedFact(feature_id=fid, field=field, value=value, description=description, origin=SEED_ORIGIN, quality=quality)  # type: ignore[arg-type]
        )

    if company.get("okved_code") or company.get("okved_name"):
        add("company.okved", "MAINOKVED_CODE/MAINOKVED_NAME", f"{company.get('okved_code', '')} {company.get('okved_name', '')}".strip(), "Основной ОКВЭД")
    if company.get("industry"):
        add("company.industry", "industry", company["industry"], "Отрасль (по ОКВЭД)", "derived")
    revenue = company.get("revenue_bn_rub")
    if isinstance(revenue, (int, float)) and revenue > 0:
        add("company.revenue", "REVENUE_COMPANY", f"{revenue} млрд ₽", "Выручка за прошлый отчётный год (2025), не за текущий период")
    else:
        unknowns.append("Выручка компании неизвестна (в данных заглушка или пусто).")
    region = ", ".join(x for x in (company.get("region"), company.get("city")) if x)
    if region:
        add("company.region", "RC_NAME/CITY_NAME", region, "Регион и город обслуживания")
    if company.get("workers_label"):
        add("company.staff", "WORKERS", company["workers_label"], "Численность сотрудников (диапазон)")
    if company.get("age_label"):
        add("company.age", "AGE", company["age_label"], "Возраст компании (диапазон)")
    if company.get("stage"):
        add("company.stage", "STAGE", company["stage"], "Стадия развития")
    if company.get("group_id"):
        add("company.group", "GROUP_ID", company["group_id"], "Входит в группу компаний (по владению или ЕИО)")

    products = company.get("products") or {}
    for key, (field, label) in PRODUCT_LABELS.items():
        value = products.get(key)
        if value is None:
            unknowns.append(f"Наличие продукта «{label}» в профиле неизвестно.")
            continue
        add(f"product.{key}", field, _yes_no(value), f"{label} в Альфа-Банке")
        if value is True and key in PRODUCT_EXPOSURES:
            factor, conf = PRODUCT_EXPOSURES[key]
            exposures.append(
                EconomicExposure(
                    exposure_id=f"exp.{key}", factor=factor, confidence=conf,  # type: ignore[arg-type]
                    basis_feature_ids=[f"product.{key}"],
                    description=f"Выведено из наличия продукта «{label}»; условия договоров неизвестны.",
                )
            )

    fts = company.get("fts") or {}
    if fts.get("flag") is None:
        unknowns.append("Наличие внешнеэкономической деятельности неизвестно.")
    else:
        add("fts.flag", "FTS_FLAG", _yes_no(fts.get("flag")), "Есть таможенные декларации (ВЭД)")
        if fts.get("flag"):
            basis = ["fts.flag"]
            if fts.get("country"):
                add("fts.country", "FTS_COUNTRY_NAME", fts["country"], "Страна ВЭД по декларациям (направление экспорт/импорт неизвестно)")
                basis.append("fts.country")
            exposures.append(
                EconomicExposure(
                    exposure_id="exp.fx_trade", factor="Валютный курс и условия внешней торговли", confidence="medium",
                    basis_feature_ids=basis,
                    description="Выведено из наличия таможенных деклараций; направление и валюта контрактов неизвестны.",
                )
            )
            unknowns.append("Направление ВЭД (экспорт или импорт), валюта контрактов и товарная структура неизвестны.")

    for i, cp in enumerate(company.get("counterparties") or [], start=1):
        fid = f"counterparty.{i}"
        direction = "исходящие платежи компании" if cp.get("direction") == "out" else "входящие платежи компании"
        share = cp.get("share_pct")
        value = f"{cp.get('name')}: {direction}" + (f", {share}% от объёма" if share is not None else "")
        note = cp.get("note")
        description = "Платёжная связь (тип отношений по платежам не подтверждён)"
        add(fid, "PAYMENTS", value, description, "demo_label" if note else "observed")
        if note:
            note = f"пометка демо-профиля, договором не подтверждена: {note}"
        related.append(
            RelatedEntity(
                entity_id=fid, name=str(cp.get("name")),
                relation="payment_out" if cp.get("direction") == "out" else "payment_in",
                share_pct=float(share) if isinstance(share, (int, float)) else None,
                basis_feature_ids=[fid], note=note,
            )
        )

    return CompanyContext(
        profile_version=company.get("profile_version") or "unknown",
        profile_snapshot_ref=f"seed:{company.get('company_id')}@{company.get('profile_version')}",
        verified_facts=facts,
        economic_exposures=exposures,
        related_entities=related,
        graph_context=GraphContext(
            included=bool(related),
            reason="Доли платежей из демо-профиля; self-переводы исключены на этапе подготовки данных" if related else "Платёжный контекст не передан",
        ),
        unknowns=_dedup(unknowns),
        synthetic_demo_profile=True,
    )


def company_context_from_ranking_profile(profile: dict[str, Any]) -> CompanyContext:
    """Профиль ранкера (``features`` с происхождением) → ``CompanyContext``."""
    facts: list[VerifiedFact] = []
    unknowns: list[str] = list(STANDARD_UNKNOWNS)
    exposures: list[EconomicExposure] = []
    features = profile.get("features") or {}
    for fid, f in features.items():
        path = f.get("path") or ""
        if path.startswith(RANKING_SKIP_PREFIXES):
            continue
        label = RANKING_PATH_LABELS.get(path, path.strip("/").replace("/", " → "))
        value = f.get("value")
        quality = f.get("quality") or "observed"
        if value is None or value == [] or quality == "unknown":
            unknowns.append(f"{label}: неизвестно.")
            continue
        if isinstance(value, bool):
            value = _yes_no(value)
        if path == "/revenue/raw":
            label += "; единица и период не подтверждены — не считать рублями за период"
        origin = f.get("origin") or {}
        facts.append(
            VerifiedFact(
                feature_id=fid,
                field=path,
                value=value,
                description=label,
                origin=Origin(type=str(origin.get("kind") or "profile"), ref=origin.get("source_path")),
                quality="derived" if quality == "derived" else "observed",
            )
        )
        product_key = {"/products/loan": "loans", "/products/leasing": "leasing", "/products/deposit": "deposits",
                       "/products/factoring": "factoring", "/products/guarantee": "guarantees"}.get(path)
        if product_key and f.get("value") is True:
            factor, conf = PRODUCT_EXPOSURES[product_key]
            exposures.append(EconomicExposure(exposure_id=f"exp.{product_key}", factor=factor, confidence=conf,  # type: ignore[arg-type]
                                              basis_feature_ids=[fid]))
        if path == "/fts_present" and f.get("value") is True:
            exposures.append(EconomicExposure(exposure_id="exp.fx_trade", factor="Валютный курс и условия внешней торговли",
                                              confidence="medium", basis_feature_ids=[fid]))
    for item in profile.get("missing_fields") or []:
        unknowns.append(f"Нет данных: {item}.")
    for item in profile.get("limitations") or []:
        if isinstance(item, str):
            unknowns.append(item)
    return CompanyContext(
        profile_version=str(profile.get("profile_version") or "unknown"),
        profile_snapshot_ref=f"ranking-profile:{profile.get('company_id')}",
        verified_facts=facts,
        economic_exposures=exposures,
        related_entities=[],
        graph_context=GraphContext(included=False, reason="Граф платежей в профиль ранкера не передан"),
        unknowns=_dedup(unknowns),
        synthetic_demo_profile=bool(profile.get("is_synthetic", True)),
    )


def company_context(profile: dict[str, Any]) -> CompanyContext:
    if isinstance(profile.get("features"), dict):
        return company_context_from_ranking_profile(profile)
    return company_context_from_seed(profile)


def _dedup(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# =========================================================================== события


@lru_cache(maxsize=1)
def _seed_bundles() -> dict[str, dict[str, Any]]:
    path = DATA_DIR / "seed_bundles.json"
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {b["event"]["event_id"]: b for b in raw.get("bundles", [])}


def seed_bundle(event_id: str) -> dict[str, Any] | None:
    b = _seed_bundles().get(event_id)
    return json.loads(json.dumps(b)) if b else None


_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


def bundle_from_news_event(event: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    """Строит фрагмент EventBundle для события без структурной разметки (загрузка пользователя).

    Каждое утверждение — дословный фрагмент заголовка/текста; модальность не размечена
    (``unknown``), источник — «Загружено пользователем», непроверенный.
    """
    event_id = str(event.get("event_id"))
    title = (event.get("title") or "").strip()
    body = (text if text is not None else event.get("text") or "").strip()
    parts = [title] + [s.strip() for s in _SENTENCE_RE.split(body) if len(s.strip()) > 15][:4]
    claims, evidence = [], []
    for i, part in enumerate(_dedup(parts), start=1):
        ev_id = f"{event_id}:ev{i}"
        claims.append({
            "claim_id": f"{event_id}:c{i}", "text": part, "modality": "unknown", "polarity": "affirmed",
            "stage": None, "scenario": None, "time": None, "measurements": [], "participants": [],
            "geographies": [], "attribution": {"name": "Пользователь (источник не указан)"}, "evidence_ids": [ev_id],
        })
        evidence.append({"evidence_id": ev_id, "source_id": f"{event_id}:src", "field": "title" if i == 1 else "text", "quote": part})
    return {
        "schema_version": "user-upload",
        "event": {
            "event_id": event_id, "event_version": 1, "event_type": event.get("topic") or "other", "title": title,
            "first_published_at": event.get("date"), "quality": "partial",
        },
        "claims": claims,
        "sources": [{"source_id": f"{event_id}:src", "title": title, "publisher": "Загружено пользователем",
                     "published_at": event.get("date"), "is_synthetic": True}],
        "evidence": evidence,
        "conflicts": [],
        "relations": [],
        "limitations": ["Новость загружена пользователем; источник и достоверность не проверены."],
    }


def bundle_for_event(event: dict[str, Any]) -> dict[str, Any]:
    """Структурные данные события: готовый EventBundle, seed-фрагмент или сборка из текста."""
    if isinstance(event.get("bundle"), dict):
        return event["bundle"]
    if isinstance(event.get("claims"), list) and isinstance(event.get("event"), dict):
        return event
    seeded = seed_bundle(str(event.get("event_id")))
    if seeded:
        return seeded
    return bundle_from_news_event(event)


def projection_from_bundle(bundle: dict[str, Any]) -> ProjectionBundle:
    """Неизменённые структурные части EventBundle (без суммаризации)."""
    ev = dict(bundle.get("event") or {})
    ev.setdefault("event_version", 1)
    return ProjectionBundle.model_validate(
        {
            "bundle_schema_version": str(bundle.get("schema_version")) if bundle.get("schema_version") else None,
            "event": ev,
            "claims": bundle.get("claims") or [],
            "conflicts": bundle.get("conflicts") or [],
            "relations": bundle.get("relations") or [],
            "sources": bundle.get("sources") or [],
            "evidence": bundle.get("evidence") or [],
            "limitations": [x for x in (bundle.get("limitations") or []) if isinstance(x, str)],
        }
    )


# =========================================================================== Bank RAG (минимальный)

_WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.I)
_STOP = {"на", "по", "из", "за", "до", "и", "в", "с", "к", "от", "что", "как", "для", "без", "не", "года", "году", "уровне"}


def _stems(text: str) -> set[str]:
    return {w.lower()[:5] for w in _WORD_RE.findall(text or "") if len(w) > 3 and w.lower() not in _STOP}


def bank_fragments_from_evidence(evidence: Iterable[Any]) -> list[BankFragment]:
    """Фрагменты аналитики банка из общего списка доказательств (``type == bank_research``)."""
    out = []
    for ev in evidence:
        d = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
        if d.get("type") != "bank_research":
            continue
        out.append(
            BankFragment(
                bank_chunk_id=d["evidence_id"], document_id=d.get("doc"), title=d.get("title") or "",
                publisher=d.get("publisher") or "Центр макроэкономических исследований Альфа-Банка",
                published_at=d.get("date"), page=d.get("page"), quote=d.get("quote") or "",
                is_synthetic=bool(d.get("is_synthetic")),
            )
        )
    return out


def search_bank_fragments(
    bundle: dict[str, Any], fragments: list[BankFragment], as_of: datetime, *, min_overlap: int = 2, top_k: int = 2
) -> list[BankFragment]:
    """Поиск по пересечению основ слов (BM25 на корпусе банка — задача модуля RAG).

    Материалы, опубликованные после ``as_of``, не используются. Применимость: ``current``,
    если материал не старше события более чем на 45 дней, иначе ``outdated``.
    """
    ev = bundle.get("event") or {}
    query = " ".join([ev.get("title") or ""] + [c.get("text") or "" for c in bundle.get("claims") or []])
    q = _stems(query)
    event_dt = parse_dt(ev.get("first_published_at"))
    scored = []
    for fr in fragments:
        published = parse_dt(fr.published_at)
        if published and published > as_of:
            continue
        overlap = len(q & _stems(fr.title + " " + fr.quote))
        if overlap >= min_overlap:
            applicability = "unknown"
            if published and event_dt:
                applicability = "current" if (event_dt - published).days <= 45 else "outdated"
            scored.append((overlap, fr.model_copy(update={"applicability": applicability})))
    scored.sort(key=lambda x: -x[0])
    return [fr for _, fr in scored[:top_k]]


# =========================================================================== контекст отбора


def match_context_from_relevance(relevance: Any, ctx: CompanyContext, bundle: dict[str, Any]) -> MatchContext:
    """Сигналы отборщика (шаблонного правила или ранкера) → ``match_context``.

    ``feature_key`` факторов сопоставляется с ID признаков профиля; это только
    подсказка «возможно интересно», не доказательство влияния.
    """
    if relevance is None:
        return MatchContext()
    rel = relevance.model_dump() if hasattr(relevance, "model_dump") else dict(relevance)
    known = {f.feature_id for f in ctx.verified_facts}
    signals, matched = [], []
    for factor in rel.get("factors") or []:
        key = str(factor.get("feature_key") or "")
        ids = [fid for fid in factor.get("feature_ids") or [] if fid in known]
        ids += _map_template_feature_key(key, factor.get("label") or "", ctx)
        matched.extend(ids)
        signals.append(MatchSignal(channel="rules", profile_facet=key or None, reason=f"{factor.get('label')}: {factor.get('detail')}"))
    hypotheses = [r for r in (rel.get("reasons") or []) if isinstance(r, str)]
    confirmed = None
    names = [e.name for e in ctx.related_entities]
    text = json.dumps(bundle.get("claims") or [], ensure_ascii=False)
    for name in names:
        core = re.sub(r"^(ООО|АО|ПАО|ОАО|ЗАО)\s*", "", name).strip("«»\" ")
        if core and core in text:
            confirmed = True
    return MatchContext(
        signals=signals,
        matched_company_feature_ids=_dedup(matched),
        matched_event_claim_ids=[c.get("claim_id") for c in bundle.get("claims") or [] if c.get("claim_id")],
        confirmed_entity_match=confirmed,
        impact_hypotheses=hypotheses,
        assumptions_not_verified=["Связь события с компанией предложена правилом отбора и требует проверки аналитиком."] if signals else [],
        prior_score=rel.get("score"),
    )


def _map_template_feature_key(key: str, label: str, ctx: CompanyContext) -> list[str]:
    known = {f.feature_id for f in ctx.verified_facts}
    if key.startswith("product."):
        return [key] if key in known else []
    if key == "fts":
        return [x for x in ("fts.flag", "fts.country") if x in known]
    if key.startswith("industry"):
        return ["company.industry"] if "company.industry" in known else []
    if key.startswith("region"):
        return ["company.region"] if "company.region" in known else []
    if key.startswith("counterparty"):
        out = []
        for e in ctx.related_entities:
            core = re.sub(r"^(ООО|АО|ПАО|ОАО|ЗАО)\s*", "", e.name).strip("«»\" ")
            if core and core in label:
                out.append(e.entity_id)
        return out
    return []


# =========================================================================== сборка входа


def build_candidate(
    event: dict[str, Any],
    ctx: CompanyContext,
    as_of: datetime,
    *,
    relevance: Any = None,
    bank_corpus: list[BankFragment] | None = None,
) -> Candidate:
    bundle = bundle_for_event(event)
    projection = projection_from_bundle(bundle)
    ev = projection.event
    sha = stable_hash(bundle)
    return Candidate(
        candidate_id=f"cand:{ev.event_id}:v{ev.event_version}",
        cluster_id=str(event.get("cluster_id") or bundle.get("cluster_id") or f"cluster:{ev.event_id}"),
        event_bundle_refs=[
            EventBundleRef(event_id=ev.event_id, event_version=int(ev.event_version or 1), snapshot_sha256=sha,
                           snapshot_ref=f"bundle:{ev.event_id}@v{ev.event_version}")
        ],
        analysis_projection=AnalysisProjection(event_bundles=[projection]),
        match_context=match_context_from_relevance(relevance, ctx, bundle),
        bank_fragments=search_bank_fragments(bundle, bank_corpus or [], as_of),
    )


def build_analyst_input(
    company: dict[str, Any],
    role: str,
    events: list[dict[str, Any]],
    *,
    as_of: datetime,
    relevance_by_event: dict[str, Any] | None = None,
    bank_corpus: list[BankFragment] | None = None,
    news_window_hours: int = 24 * 60,
    max_insights: int = 10,
) -> AnalystInput:
    ctx = company_context(company)
    relevance_by_event = relevance_by_event or {}
    candidates = [
        build_candidate(ev, ctx, as_of, relevance=relevance_by_event.get(str(ev.get("event_id") or (ev.get("event") or {}).get("event_id"))), bank_corpus=bank_corpus)
        for ev in events
    ]
    company_id = str(company.get("company_id"))
    request_id = "req:" + stable_hash([company_id, role, as_of.isoformat(), [c.candidate_id for c in candidates]])[:16]
    return AnalystInput(
        request=AnalystRequest(
            request_id=request_id, company_id=company_id, role=role,  # type: ignore[arg-type]
            as_of=as_of.isoformat(), news_window_hours=news_window_hours, max_insights=max_insights,
        ),
        company_context=ctx,
        candidates=candidates,
    )
