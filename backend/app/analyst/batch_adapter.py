"""Адаптер входа ретривера ``ImpactCandidateBatch v1`` → ``AnalystInput.v1-draft``.

Отдельный от промптов слой (ТЗ v1.1, разд. 3: «независимый адаптер входа»): при
смене контракта ретривера правится только этот модуль. Что делает:

1. **Валидация.** Всегда — Pydantic-модели пакета (повторяют
   ``impact-candidate-batch.v1.schema.json``) и смысловые проверки: ID кандидатов
   уникальны, ``snapshot`` совпадает с событием бандла, ``evidence_ids`` утверждений
   и ``source_id`` цитат существуют, ``claim_refs`` указывают на существующие
   утверждения, признаки принадлежат той же компании. Если доступен каталог схем
   (``schema_dir`` или ``ANALYST_BATCH_SCHEMA_DIR``) — дополнительно проверка по JSON
   Schema (``jsonschema_lite``: пакет и каждый бандл B6 v0.2). Ошибка уровня пакета —
   ``BatchValidationError``; ошибка в одном кандидате — кандидат отклоняется с
   причиной, остальные анализируются.
2. **Статус.** ``empty`` → вход без кандидатов и причина (``empty_reason``);
   ``degraded`` → анализ с пометкой в неизвестном; ``ready`` без кандидатов — тоже пустой вход.
3. **Профиль.** ``verified_facts`` → проверенные признаки (флаги продуктов — «есть/нет
   в Альфа-Банке», выручка — без единицы и периода); ``unknowns``,
   ``payment_context.status=not_loaded`` и ``group_context`` → неизвестное
   («не загружено», а не «нет контрагентов»).
4. **Контроль времени.** Источники, опубликованные после ``as_of``, и цитаты/утверждения,
   опирающиеся только на них, отбрасываются; событие после ``as_of`` исключит ``precheck``.
5. **Качество текста.** Утверждения с повреждённой кодировкой (символы U+FFFD)
   помечаются; если читаемых утверждений нет — флаг ``unreadable_claims`` (кандидат
   исключается без вызова LLM).
6. **Сигналы отбора.** Каналы и совпавшие представления → ``match_context`` как
   «возможно интересно»; признаки, которых нет среди проверенных фактов
   (например ``/industry``), не передаются как основание.
7. **Бюджет промпта.** Оценка полного промпта (системный + пользовательский +
   примеры, байты UTF-8 / 4) на кандидата; при превышении целиком отбрасываются
   менее значимые утверждения (не из ``claim_refs`` и не из противоречий) вместе с
   цитатами и источниками, на которые больше никто не ссылается. Цитаты не
   обрезаются, ID оставшихся утверждений и цитат не меняются.

Роль задаётся параметром (любая из четырёх); по умолчанию — ``request.role`` пакета.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.analyst.input_adapter import PRODUCT_EXPOSURES, RANKING_PATH_LABELS, parse_dt, projection_from_bundle
from app.analyst.jsonschema_lite import SchemaValidator, load_validator
from app.analyst.schemas import (
    ROLES,
    AnalysisProjection,
    AnalystInput,
    AnalystRequest,
    Candidate,
    CompanyContext,
    EconomicExposure,
    EventBundleRef,
    GraphContext,
    MatchContext,
    MatchSignal,
    Origin,
    ProjectionBundle,
    VerifiedFact,
)

BATCH_SCHEMA_VERSION = "impact_candidate_batch.v1"
BATCH_SCHEMA_FILE = "impact-candidate-batch.v1.schema.json"
BUNDLE_SCHEMA_FILE = "event-bundle.v0.2.schema.json"
DEFAULT_TOKEN_BUDGET = 8000

SKIP_PATHS = ("/company_id", "/source_snapshot")
UNKNOWN_LABELS = {
    "/countries": "Страны ВЭД неизвестны.",
    "/industry": "Укрупнённая отрасль в профиле не указана (есть только ОКВЭД).",
    "/revenue/period": "Период, за который указана выручка, неизвестен.",
    "/revenue/unit": "Единица измерения выручки неизвестна — сумму выручки не использовать.",
    "/source_snapshot/data_as_of": "Дата актуальности профиля неизвестна.",
}
CHANNEL_RU = {"dense": "семантическое сходство", "lexical": "совпадение слов", "structured": "структурное правило"}
VIEW_RU = {"business": "вид деятельности", "finance": "финансовые продукты", "trade_geo": "ВЭД и география"}
LIMITATION_RU = {
    "candidate_requires_business_analysis": "Кандидат требует бизнес-анализа: сходство при отборе не доказывает влияния.",
    "retrieval_similarity_is_not_business_significance": "Сходство при отборе не является бизнес-значимостью.",
}
BATCH_UNKNOWNS = [
    "Условия кредитных договоров компании (тип ставки, сроки, объём долга) в профиле неизвестны.",
    "Площадки, маршруты, поставщики и покупатели компании в профиле неизвестны.",
]


class BatchValidationError(ValueError):
    """Пакет не соответствует контракту ``impact_candidate_batch.v1``."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]) + (f" … (+{len(errors) - 5})" if len(errors) > 5 else ""))
        self.errors = errors


# =========================================================================== модели пакета


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


def _check_dt(v: str) -> str:
    if parse_dt(v) is None or "T" not in v:
        raise ValueError("ожидалась дата-время ISO 8601")
    return v


class BatchRequest(_Strict):
    request_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    role: Literal["ceo", "cfo", "treasurer", "chief_accountant"]
    as_of: str
    window_start: str
    window_end: str
    requested_limit: int = Field(ge=1, le=20)
    max_input_tokens: int = Field(ge=1)

    @field_validator("as_of", "window_start", "window_end")
    @classmethod
    def _dt(cls, v: str) -> str:
        return _check_dt(v)


class BatchFact(_Loose):
    feature_id: str = Field(min_length=1)
    path: str
    value: Any = None
    quality: str = "observed"
    origin: dict[str, Any] = Field(default_factory=dict)


class BatchCompanyContext(_Loose):
    profile_version: str
    profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot: dict[str, Any]
    verified_facts: list[BatchFact]
    derived_exposures: list[Any]
    group_context: dict[str, Any]
    payment_context: dict[str, Any]
    unknowns: list[str]


class BatchSnapshot(_Loose):
    event_id: str = Field(min_length=1)
    event_version: int = Field(ge=1)
    payload_sha256: str
    received_at: str
    source_mode: str
    ingest_id: str | None = None


class BatchMember(_Strict):
    snapshot: BatchSnapshot
    bundle: dict[str, Any]


class BatchMatchContext(_Strict):
    channels: list[Literal["dense", "lexical", "structured"]]
    matched_views: list[str]
    feature_ids: list[str]
    neighbor_feature_refs: list[dict[str, Any]]
    claim_refs: list[dict[str, Any]]
    event_paths: list[str]
    hypotheses: list[Any] = Field(max_length=0)
    unknowns: list[Any]


class BatchCandidate(_Strict):
    candidate_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    # По схеме v1 кластеров ещё нет (null); строку принимаем для совместимости вперёд.
    event_cluster_id: str | None
    cluster_version: str | int | None
    members: list[BatchMember] = Field(min_length=1, max_length=1)
    match_context: BatchMatchContext
    limitations: list[str]


class ImpactCandidateBatch(_Strict):
    schema_version: Literal["impact_candidate_batch.v1"]
    request: BatchRequest
    status: Literal["ready", "empty", "degraded"]
    empty_reason: Literal["no_events", "no_matches", "budget_exhausted"] | None
    company_context: BatchCompanyContext
    candidates: list[dict[str, Any]] = Field(max_length=20)
    retrieval_context: dict[str, Any]
    limitations: list[str]

    @field_validator("retrieval_context")
    @classmethod
    def _retrieval(cls, v: dict[str, Any]) -> dict[str, Any]:
        need = ["trace_id", "index_snapshot_id", "encoder_fingerprint", "representation_version", "policy_version",
                "data_watermark", "failed_channels"]
        missing = [k for k in need if k not in v]
        if missing:
            raise ValueError(f"нет полей retrieval_context: {', '.join(missing)}")
        return v


# =========================================================================== результат адаптера


@dataclass
class AdapterResult:
    analyst_input: AnalystInput
    status: str
    empty_reason: str | None = None
    notes: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    truncation: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_estimates: dict[str, int] = field(default_factory=dict)
    schema_checked: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.analyst_input.candidates


TokenEstimator = Callable[[AnalystInput, Candidate], int]


def _err_text(exc: ValidationError, prefix: str = "") -> list[str]:
    out = []
    for e in exc.errors():
        loc = "/".join(str(x) for x in e.get("loc", ()))
        out.append(f"{prefix}/{loc}: {e.get('msg')}")
    return out


def default_schema_dir() -> Path | None:
    env = os.environ.get("ANALYST_BATCH_SCHEMA_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    return None


# =========================================================================== адаптер


class ImpactBatchAdapter:
    """``ImpactCandidateBatch v1`` → ``AnalystInput``.

    ``token_budget`` — потолок оценки полного промпта на кандидата (байты UTF-8 / 4);
    ``reserve_tokens`` — запас под блок памяти фактов; ``estimator`` — функция оценки
    промпта (по умолчанию ``prompt_budget.estimate_prompt_tokens`` текущей версии промпта).
    """

    def __init__(
        self,
        *,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        reserve_tokens: int = 0,
        schema_dir: str | Path | None = None,
        estimator: TokenEstimator | None = None,
        prompt_version: str | None = None,
    ) -> None:
        self.token_budget = token_budget
        self.reserve_tokens = reserve_tokens
        self.schema_dir = Path(schema_dir) if schema_dir else default_schema_dir()
        self._estimator = estimator
        self.prompt_version = prompt_version
        self._batch_validator: SchemaValidator | None = None
        self._bundle_validator: SchemaValidator | None = None
        if self.schema_dir is not None:
            bp, ep = self.schema_dir / BATCH_SCHEMA_FILE, self.schema_dir / BUNDLE_SCHEMA_FILE
            self._batch_validator = load_validator(bp) if bp.is_file() else None
            self._bundle_validator = load_validator(ep) if ep.is_file() else None

    # ------------------------------------------------------------------ валидация
    def validate(self, raw: dict[str, Any]) -> tuple[ImpactCandidateBatch, dict[int, list[str]], list[str]]:
        """→ (пакет, ошибки по индексам кандидатов, замечания). Ошибки уровня пакета — исключение."""
        if not isinstance(raw, dict):
            raise BatchValidationError(["/: пакет должен быть JSON-объектом"])
        batch_errors: list[str] = []
        cand_errors: dict[int, list[str]] = {}
        notes: list[str] = []
        if self._batch_validator is not None:
            for e in self._batch_validator.errors(raw):
                m = re.match(r"/candidates/(\d+)", e)
                if m:
                    cand_errors.setdefault(int(m.group(1)), []).append(e)
                else:
                    batch_errors.append(e)
        try:
            batch = ImpactCandidateBatch.model_validate(raw)
        except ValidationError as exc:
            raise BatchValidationError(batch_errors + _err_text(exc)) from None
        if batch_errors:
            raise BatchValidationError(batch_errors)

        req = batch.request
        if batch.status == "ready" and batch.empty_reason is not None:
            notes.append(f"status=ready, но empty_reason={batch.empty_reason}: причина проигнорирована")
        if batch.status == "empty" and batch.empty_reason is None:
            notes.append("status=empty без empty_reason")
        prefix = f"{req.company_id}:"
        foreign = [f.feature_id for f in batch.company_context.verified_facts if not f.feature_id.startswith(prefix)]
        if foreign:
            raise BatchValidationError([f"/company_context/verified_facts: признаки другой компании: {', '.join(foreign[:3])}"])

        seen: set[str] = set()
        for i, c in enumerate(raw.get("candidates") or []):
            errs = cand_errors.setdefault(i, [])
            try:
                cand = BatchCandidate.model_validate(c)
            except ValidationError as exc:
                errs.extend(_err_text(exc, f"/candidates/{i}"))
                continue
            if cand.candidate_id in seen:
                errs.append(f"/candidates/{i}: повтор candidate_id")
            seen.add(cand.candidate_id)
            errs.extend(self._semantic_errors(i, cand, prefix))
        return batch, {k: v for k, v in cand_errors.items() if v}, notes

    def _semantic_errors(self, i: int, cand: BatchCandidate, prefix: str) -> list[str]:
        errs: list[str] = []
        claim_ids: set[str] = set()
        for j, m in enumerate(cand.members):
            path = f"/candidates/{i}/members/{j}"
            b = m.bundle
            if self._bundle_validator is not None:
                errs.extend(f"{path}/bundle{e[1:] if e.startswith('/') else e}" for e in self._bundle_validator.errors(b)[:10])
            elif b.get("schema_version") != "0.2":
                errs.append(f"{path}/bundle: ожидалась schema_version 0.2")
            ev = b.get("event") or {}
            if ev.get("event_id") != m.snapshot.event_id:
                errs.append(f"{path}: snapshot.event_id не совпадает с event.event_id")
            if ev.get("event_version") not in (None, m.snapshot.event_version):
                errs.append(f"{path}: snapshot.event_version не совпадает с event.event_version")
            evidence = {e.get("evidence_id"): e for e in b.get("evidence") or [] if isinstance(e, dict)}
            sources = {s.get("source_id") for s in b.get("sources") or [] if isinstance(s, dict)}
            for e in evidence.values():
                if e.get("source_id") not in sources:
                    errs.append(f"{path}: цитата {e.get('evidence_id')} ссылается на несуществующий источник")
            for c in b.get("claims") or []:
                if not isinstance(c, dict) or not c.get("claim_id") or not isinstance(c.get("text"), str):
                    errs.append(f"{path}: утверждение без claim_id/text")
                    continue
                claim_ids.add(c["claim_id"])
                missing = [x for x in c.get("evidence_ids") or [] if x not in evidence]
                if missing:
                    errs.append(f"{path}: утверждение {c['claim_id']} ссылается на несуществующие цитаты {missing[:2]}")
        for ref in cand.match_context.claim_refs:
            if ref.get("claim_id") not in claim_ids:
                errs.append(f"/candidates/{i}/match_context/claim_refs: нет утверждения {ref.get('claim_id')}")
        bad = [f for f in cand.match_context.feature_ids if not f.startswith(prefix)]
        if bad:
            errs.append(f"/candidates/{i}/match_context/feature_ids: признаки другой компании {bad[:2]}")
        return errs

    # ------------------------------------------------------------------ профиль
    @staticmethod
    def company_context(batch: ImpactCandidateBatch) -> CompanyContext:
        cc = batch.company_context
        company_id = batch.request.company_id
        facts: list[VerifiedFact] = []
        exposures: list[EconomicExposure] = []
        for f in cc.verified_facts:
            path = f.path or f.feature_id.split(":", 1)[-1]
            if path.startswith(SKIP_PATHS):
                continue
            label = RANKING_PATH_LABELS.get(path, path.strip("/").replace("/", " → "))
            value = f.value
            if value is None or value == [] or f.quality == "unknown":
                continue
            if path.startswith("/products/"):
                label = f"{label} в Альфа-Банке («нет» — не пользуется продуктом в этом банке)"
            if path == "/revenue/raw":
                label = "Выручка (сырое значение REVENUE_COMPANY); единица и период неизвестны — сумму не использовать"
            if path == "/region":
                label = "Регион регистрации (не список площадок)"
            if path == "/city":
                label = "Город регистрации"
            if isinstance(value, bool):
                value = "есть" if value else "нет"
            origin = f.origin or {}
            facts.append(
                VerifiedFact(
                    feature_id=f.feature_id, field=path, value=value, description=label,
                    origin=Origin(type=str(origin.get("kind") or "profile"), ref=origin.get("source_path")),
                    quality="derived" if f.quality == "derived" else "observed",
                )
            )
            product_key = {"/products/loan": "loans", "/products/leasing": "leasing", "/products/deposit": "deposits",
                           "/products/factoring": "factoring", "/products/guarantee": "guarantees"}.get(path)
            if product_key and f.value is True:
                factor, conf = PRODUCT_EXPOSURES[product_key]
                exposures.append(EconomicExposure(exposure_id=f"exp.{product_key}", factor=factor, confidence=conf,  # type: ignore[arg-type]
                                                  basis_feature_ids=[f.feature_id],
                                                  description="Выведено из наличия продукта; условия договоров неизвестны."))
            if path == "/fts_present" and f.value is True:
                exposures.append(EconomicExposure(exposure_id="exp.fx_trade", factor="Валютный курс и условия внешней торговли",
                                                  confidence="medium", basis_feature_ids=[f.feature_id],
                                                  description="Выведено из наличия таможенных деклараций; направление и валюта неизвестны."))
        known = {f.feature_id for f in facts}
        for i, e in enumerate(cc.derived_exposures):
            if isinstance(e, dict) and e.get("factor"):
                exposures.append(EconomicExposure(
                    exposure_id=str(e.get("exposure_id") or f"exp.derived.{i}"), factor=str(e["factor"]),
                    confidence=e.get("confidence") if e.get("confidence") in ("low", "medium", "high") else "low",
                    basis_feature_ids=[x for x in e.get("basis_feature_ids") or [] if x in known],
                ))

        # Порядок важен: в промпт попадают первые 8 пунктов (самые важные для выводов — первыми).
        unknowns: list[str] = []
        pay = (cc.payment_context or {}).get("status")
        if pay in (None, "not_loaded"):
            unknowns.append("Платёжный контекст не загружен: контрагенты неизвестны (это не значит, что их нет).")
        unknowns += BATCH_UNKNOWNS
        grp = (cc.group_context or {}).get("status")
        if grp in (None, "not_loaded"):
            unknowns.append("Данные о группе компаний не загружены.")
        for u in cc.unknowns:
            path = u.split(":", 1)[-1]
            if path == "/source_snapshot/exported_at":
                continue
            unknowns.append(UNKNOWN_LABELS.get(path, f"{RANKING_PATH_LABELS.get(path, path)}: неизвестно."))
        if (cc.snapshot or {}).get("historical_applicability") in (None, "unknown") and UNKNOWN_LABELS["/source_snapshot/data_as_of"] not in unknowns:
            unknowns.append(UNKNOWN_LABELS["/source_snapshot/data_as_of"])
        if batch.status == "degraded":
            failed = batch.retrieval_context.get("failed_channels") or []
            unknowns.append("Отбор кандидатов выполнен в деградированном режиме" + (f" (не сработали каналы: {', '.join(map(str, failed))})" if failed else "") + ".")
        return CompanyContext(
            profile_version=f"{cc.profile_version}#{cc.profile_hash[:12]}",
            profile_snapshot_ref=f"profile:{company_id}@{cc.profile_hash[:12]}",
            verified_facts=facts,
            economic_exposures=exposures,
            related_entities=[],
            graph_context=GraphContext(included=False, reason=f"Граф платежей не загружен (payment_context.status={pay})"),
            unknowns=list(dict.fromkeys(unknowns)),
            synthetic_demo_profile=True,
        )

    # ------------------------------------------------------------------ кандидаты
    @staticmethod
    def _time_filter(bundle: dict[str, Any], as_of: datetime | None) -> tuple[dict[str, Any], list[str]]:
        """Убирает источники после ``as_of`` и всё, что опирается только на них."""
        if as_of is None:
            return bundle, []
        late = {s.get("source_id") for s in bundle.get("sources") or []
                if isinstance(s, dict) and (dt := parse_dt(s.get("published_at"))) is not None and dt > as_of}
        if not late:
            return bundle, []
        b = dict(bundle)
        b["sources"] = [s for s in bundle.get("sources") or [] if s.get("source_id") not in late]
        dropped_ev = {e.get("evidence_id") for e in bundle.get("evidence") or [] if e.get("source_id") in late}
        b["evidence"] = [e for e in bundle.get("evidence") or [] if e.get("evidence_id") not in dropped_ev]
        claims = []
        for c in bundle.get("claims") or []:
            ev_ids = [x for x in c.get("evidence_ids") or [] if x not in dropped_ev]
            if c.get("evidence_ids") and not ev_ids:
                continue
            claims.append(dict(c, evidence_ids=ev_ids))
        b["claims"] = claims
        return b, [f"Отброшено источников, опубликованных после as_of: {len(late)}"]

    @staticmethod
    def _unreadable(text: str) -> bool:
        if not text:
            return True
        bad = text.count("�")
        letters = sum(1 for ch in text if ch.isalpha())
        return bad >= 3 and bad >= 0.2 * max(1, letters + bad)

    def candidate(self, bc: BatchCandidate, ctx: CompanyContext, as_of: datetime | None, batch_limitations: list[str]) -> Candidate:
        member = bc.members[0]
        bundle, time_notes = self._time_filter(member.bundle, as_of)
        projection = projection_from_bundle(bundle)
        flags: list[str] = []
        unreadable = [c.claim_id for c in projection.claims if self._unreadable(c.text)]
        if unreadable:
            projection.limitations.append(
                "Текст части утверждений повреждён (ошибка кодировки) — их содержание не читается."
            )
            if len(unreadable) == len(projection.claims):
                flags.append("unreadable_claims")
        projection.limitations = list(dict.fromkeys(projection.limitations + time_notes))
        ev = projection.event
        snap = member.snapshot
        known = {f.feature_id for f in ctx.verified_facts}
        mc = bc.match_context
        signals = [
            MatchSignal(channel=ch, profile_facet=None,
                        reason=f"Отбор: {CHANNEL_RU.get(ch, ch)} по представлениям профиля "
                               + ", ".join(VIEW_RU.get(v, v) for v in mc.matched_views))
            for ch in mc.channels
        ]
        unverified = sorted({f.split(":", 1)[-1] for f in mc.feature_ids if f not in known})
        assumptions = [LIMITATION_RU.get(x, x) for x in bc.limitations]
        assumptions += [LIMITATION_RU[x] for x in batch_limitations if x in LIMITATION_RU and LIMITATION_RU[x] not in assumptions]
        if mc.event_paths and all(p.startswith("/event/") for p in mc.event_paths):
            assumptions.append("Совпадение при отборе найдено только в заголовке события.")
        if unverified:
            assumptions.append("Признаки, по которым отобран кандидат, но которых нет среди проверенных фактов: " + ", ".join(unverified) + ".")
        return Candidate(
            candidate_id=bc.candidate_id,
            cluster_id=str(bc.event_cluster_id or f"cluster:{snap.event_id}"),
            event_bundle_refs=[EventBundleRef(
                event_id=snap.event_id, event_version=snap.event_version, snapshot_sha256=snap.payload_sha256,
                snapshot_ref=f"ingest:{snap.ingest_id}" if snap.ingest_id else f"bundle:{snap.event_id}@v{snap.event_version}",
            )],
            analysis_projection=AnalysisProjection(event_bundles=[projection]),
            match_context=MatchContext(
                signals=signals,
                matched_company_feature_ids=[f for f in mc.feature_ids if f in known],
                matched_event_claim_ids=[r["claim_id"] for r in mc.claim_refs if r.get("claim_id")],
                confirmed_entity_match=None,
                impact_hypotheses=[str(h) for h in mc.hypotheses],
                assumptions_not_verified=list(dict.fromkeys(assumptions)),
            ),
            bank_fragments=[],  # Bank RAG в этих пакетах не запускался — позицию банка не формируем
            quality_flags=flags,
        )

    # ------------------------------------------------------------------ бюджет
    def _estimate(self, inp: AnalystInput, cand: Candidate) -> int:
        if self._estimator is not None:
            return self._estimator(inp, cand)
        from app.analyst.prompt_budget import estimate_prompt_tokens

        return estimate_prompt_tokens(inp, cand, self.prompt_version)

    def fit_to_budget(self, inp: AnalystInput, budget: int) -> tuple[AnalystInput, dict[str, dict[str, Any]], dict[str, int]]:
        """Отбрасывает целиком менее значимые утверждения, пока оценка промпта не уложится в бюджет."""
        limit = max(1, budget - self.reserve_tokens)
        report: dict[str, dict[str, Any]] = {}
        estimates: dict[str, int] = {}
        new_cands: list[Candidate] = []
        for cand in inp.candidates:
            before = tokens = self._estimate(inp, cand)
            dropped_claims: list[str] = []
            dropped_ev: list[str] = []
            dropped_src: list[str] = []
            while tokens > limit:
                victim = self._least_important_claim(cand)
                if victim is None:
                    break
                cand, ev_ids, src_ids = self._drop_claim(cand, victim)
                dropped_claims.append(victim)
                dropped_ev += ev_ids
                dropped_src += src_ids
                tokens = self._estimate(inp, cand)
            if dropped_claims:
                b = cand.analysis_projection.event_bundles[0]
                b.limitations.append(f"Из-за лимита контекста опущено утверждений источника: {len(dropped_claims)} (целиком, без обрезки цитат).")
                tokens = self._estimate(inp, cand)
            report[cand.candidate_id] = {
                "tokens_before": before, "tokens_after": tokens, "budget": limit, "over_budget": tokens > limit,
                "dropped_claim_ids": dropped_claims, "dropped_evidence_ids": dropped_ev, "dropped_source_ids": dropped_src,
            }
            estimates[cand.candidate_id] = tokens
            new_cands.append(cand)
        return inp.model_copy(update={"candidates": new_cands}), report, estimates

    @staticmethod
    def _least_important_claim(cand: Candidate) -> str | None:
        claims = [c for b in cand.analysis_projection.event_bundles for c in b.claims]
        if len(claims) <= 1:
            return None
        protected = set(cand.match_context.matched_event_claim_ids)
        for b in cand.analysis_projection.event_bundles:
            for conflict in b.conflicts:
                for v in conflict.get("variants") or []:
                    if isinstance(v, dict) and v.get("claim_id"):
                        protected.add(v["claim_id"])
        # Сначала незащищённые без измерений с конца, затем незащищённые с измерениями.
        for want_measure in (False, True):
            for c in reversed(claims):
                if c.claim_id in protected or bool(c.measurements) != want_measure:
                    continue
                return c.claim_id
        return None

    @staticmethod
    def _drop_claim(cand: Candidate, claim_id: str) -> tuple[Candidate, list[str], list[str]]:
        bundles: list[ProjectionBundle] = []
        dropped_ev: list[str] = []
        dropped_src: list[str] = []
        for b in cand.analysis_projection.event_bundles:
            claims = [c for c in b.claims if c.claim_id != claim_id]
            used_ev = {e for c in claims for e in c.evidence_ids}
            victim = next((c for c in b.claims if c.claim_id == claim_id), None)
            orphan = {e for e in (victim.evidence_ids if victim else []) if e not in used_ev}
            evidence = [e for e in b.evidence if e.evidence_id not in orphan]
            used_src = {e.source_id for e in evidence}
            sources = [s for s in b.sources if s.source_id in used_src]
            dropped_ev += sorted(orphan)
            dropped_src += [s.source_id for s in b.sources if s.source_id not in used_src]
            bundles.append(b.model_copy(update={"claims": claims, "evidence": evidence, "sources": sources}))
        proj = AnalysisProjection(event_bundles=bundles)
        mc = cand.match_context.model_copy(update={"matched_event_claim_ids": [x for x in cand.match_context.matched_event_claim_ids if x != claim_id]})
        return cand.model_copy(update={"analysis_projection": proj, "match_context": mc}), dropped_ev, dropped_src

    # ------------------------------------------------------------------ сборка
    def to_analyst_input(self, raw: dict[str, Any], role: str | None = None) -> AdapterResult:
        batch, cand_errors, notes = self.validate(raw)
        req = batch.request
        role = role or req.role
        if role not in ROLES:
            raise BatchValidationError([f"/request/role: неизвестная роль {role}"])
        as_of = parse_dt(req.as_of)
        window_start = parse_dt(req.window_start)
        ctx = self.company_context(batch)
        hours = 24
        if as_of and window_start and as_of > window_start:
            hours = max(1, math.ceil((as_of - window_start).total_seconds() / 3600))
        request_id = req.request_id if role == req.role else f"{req.request_id}:{role}"

        cands: list[Candidate] = []
        rejected: list[dict[str, str]] = []
        status = batch.status
        if status == "empty":
            if raw.get("candidates"):
                notes.append("status=empty, но кандидаты переданы — проигнорированы")
        else:
            for i, c in enumerate(raw.get("candidates") or []):
                cid = c.get("candidate_id") if isinstance(c, dict) else None
                if i in cand_errors:
                    rejected.append({"candidate_id": str(cid), "reason": "; ".join(cand_errors[i][:3])})
                    continue
                cands.append(self.candidate(BatchCandidate.model_validate(c), ctx, as_of, batch.limitations))
        inp = AnalystInput(
            request=AnalystRequest(
                request_id=request_id, company_id=req.company_id, role=role,  # type: ignore[arg-type]
                as_of=req.as_of, news_window_hours=hours, max_insights=req.requested_limit,
            ),
            company_context=ctx,
            candidates=cands,
        )
        empty_reason = batch.empty_reason if status == "empty" else None
        if not cands and empty_reason is None:
            empty_reason = "no_matches" if not rejected else "all_candidates_rejected"
        budget = self.token_budget
        truncation: dict[str, dict[str, Any]] = {}
        estimates: dict[str, int] = {}
        if cands:
            inp, truncation, estimates = self.fit_to_budget(inp, budget)
            over = [cid for cid, r in truncation.items() if r["over_budget"]]
            if over:
                notes.append(f"Кандидатов выше бюджета {budget} токенов даже после усечения: {len(over)}")
        return AdapterResult(
            analyst_input=inp, status=status, empty_reason=empty_reason, notes=notes, rejected=rejected,
            truncation=truncation, token_estimates=estimates,
            schema_checked=self._batch_validator is not None,
        )
