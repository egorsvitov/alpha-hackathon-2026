"""Детерминированная проверка оснований (QA) ответа модели.

Что проверяется и как исправляется (без повторного вызова LLM):

* ссылки: псевдонимы ``C*``/``F*``/``B*`` переводятся в исходные ID; несуществующие
  отбрасываются (``unknown_ref``); факт без утверждения-источника удаляется, вывод без
  единой ссылки удаляется или ослабляется до гипотезы;
* числа: каждое число в тексте должно встречаться в данных (утверждения, измерения,
  признаки компании, фрагменты банка). В ``impact_estimate`` допустимы числа из явно
  перечисленных допущений. Иначе утверждение удаляется, оценка эффекта снимается;
* запрещённые паттерны: ущерб/потери как свершившийся факт; сильные события
  («закрыт», «отменены», «банкротство»…), которых нет в источниках; «платёж ⇒
  поставщик» как факт; изменение условий действующих договоров без данных;
  отождествление обезличенной компании; пересчёт выручки в рубли за период;
* модальность: прогноз/сценарий/опровержение не выдаётся за свершившийся факт
  (``event_status`` и метка «Прогноз:» у фактов), противоречия источников сохраняются;
* позиция банка: ``found`` только при существующем фрагменте, иначе ``not_found``;
* согласованность решения: ``include`` без связи с признаками компании → ``watch``,
  без оснований → ``insufficient_evidence``.

Итог — ``qa_status``: ``passed`` (замечаний нет), ``passed_with_fixes`` (исправлено),
``failed`` (выводы сняты, статус ``insufficient_evidence``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from app.analyst.schemas import (
    BankAssessment,
    ChangesSincePrevious,
    DraftStatement,
    ImpactEstimateOut,
    LLMDraft,
    QAIssue,
    Statement,
    UnknownOut,
)
from app.analyst.view import PromptView

HEDGE_RE = re.compile(
    r"(мож\w*|мог\w*|возможн\w*|вероятн\w*|если|при условии|в случае|потенциальн\w*|риск\w*|предполож\w*|"
    r"не исключ\w*|допуска\w*|сценари\w*|ориентировочн\w*|гипотез\w*|провер\w*|уточн\w*|способн\w*|грозит|угроз\w*|при\s)",
    re.I,
)
FORECAST_MARK_RE = re.compile(r"(прогноз\w*|ожида\w*|допуска\w*|предполага\w*|может|могут|сценари\w*|оценк\w*|по мнению|по оценк\w*)", re.I)
NEGATION_MARK_RE = re.compile(r"(опроверг\w*|не подтвер\w*|отрица\w*|не\s)", re.I)
ALIAS_RE = re.compile(r"\s*[\[(]\s*(?:[CFBЕ]\d+\s*[,;]?\s*)+[\])]|\b[CFB]\d{1,3}\b")
EXTRA_ALIAS_RE = re.compile(r"\s*[\[(]\s*(?:[SH]\d+\s*[,;]?\s*)+[\])]|\b[SH]\d{1,3}\b")
NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)")

DAMAGE_PATTERNS = [
    re.compile(r"\b(понес(?:ла|ёт|ет|ли)|потерял\w*|лишил\w*)\s", re.I),
    re.compile(r"(ущерб|убыт\w*|потер\w*)\s+(?:уже\s+)?(составил\w*|состав\w*|причин\w*|возник\w*)", re.I),
    re.compile(r"(выручк\w*|прибыл\w*|продаж\w*|маржа|маржинальн\w*)\s+(?:уже\s+)?(упал\w*|снизил\w*|сократил\w*|рухнул\w*|обвалил\w*)", re.I),
    re.compile(r"(сорвал\w*|остановил\w*|прекратил\w*)\s+(производств\w*|выпуск\w*|поставк\w*)", re.I),
]
STRONG_EVENT_STEMS = ["закрыт", "закрыл", "отмен", "банкрот", "дефолт", "национализ", "арест", "забастов", "ликвидац", "запрет"]
SUPPLIER_PATTERNS = [
    re.compile(r"(ваш\w*|ключев\w*|основн\w*|главн\w*|критичн\w*|крупн\w*|единствен\w*)\s+(поставщик\w*|покупател\w*|клиент\w*)", re.I),
    re.compile(r"(поставщик\w*|покупател\w*)\s+(компании|вашей|вас\b)", re.I),
    re.compile(r"(являет\w*|выступа\w*)\s+(?:\w+\s+)?(поставщик\w*|покупател\w*)", re.I),
    re.compile(r"поставля\w*\s+(компании|вам\b)", re.I),
]
CONTRACT_PATTERNS = [
    re.compile(r"(действующ\w+|текущ\w+|имеющ\w+|существующ\w+)\s+(кредит\w*|договор\w*|займ\w*|долг\w*|обязательств\w*|лизинг\w*)", re.I),
    re.compile(r"плавающ\w+\s+ставк\w*", re.I),
]
CONTRACT_CHANGE_RE = re.compile(r"(вырас\w*|увелич\w*|подорож\w*|снизит\w*|снизят\w*|измен\w*|пересмотр\w*|повыс\w*)", re.I)
IDENTIFICATION_RE = re.compile(r"(компания|клиент|ваша компания)\s*(—|-|является|это)\s*(ПАО|ОАО|АО|ООО|ЗАО|«)", re.I)
REVENUE_PERIOD_RE = re.compile(r"выручк\w*.{0,40}(квартал\w*|месяц\w*|в месяц|за период)", re.I)


@dataclass
class QAResult:
    decision: str
    inference_status: str
    event_status: str
    headline: str
    facts: list[Statement] = field(default_factory=list)
    company_relevance: Statement | None = None
    impact_mechanism: Statement | None = None
    risks: list[Statement] = field(default_factory=list)
    opportunities: list[Statement] = field(default_factory=list)
    next_steps: list[Statement] = field(default_factory=list)
    ai_conclusion: Statement | None = None
    unknowns: list[UnknownOut] = field(default_factory=list)
    recommended_checks: list[str] = field(default_factory=list)
    impact_estimate: ImpactEstimateOut | None = None
    bank_assessment: BankAssessment = field(default_factory=BankAssessment)
    changes: ChangesSincePrevious | None = None
    limitations: list[str] = field(default_factory=list)
    issues: list[QAIssue] = field(default_factory=list)
    status: str = "passed"
    penalty: float = 0.0
    statements_total: int = 0
    statements_with_refs: int = 0

    def all_statements(self) -> list[Statement]:
        out = list(self.facts)
        for s in (self.company_relevance, self.impact_mechanism, self.ai_conclusion):
            if s:
                out.append(s)
        return out + self.risks + self.opportunities + self.next_steps

    def support_ids(self) -> tuple[list[str], list[str], list[str], list[str]]:
        feats, claims, evs, banks = [], [], [], []
        for s in self.all_statements():
            feats += s.feature_ids
            claims += s.claim_ids
            evs += s.evidence_ids
            banks += s.bank_chunk_ids
        if self.impact_estimate:
            feats += self.impact_estimate.feature_ids
            claims += self.impact_estimate.claim_ids
            evs += self.impact_estimate.evidence_ids
        banks += self.bank_assessment.bank_chunk_ids
        return _uniq(feats), _uniq(claims), _uniq(evs), _uniq(banks)


def _uniq(items: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def numbers_in(text: str) -> set[float]:
    clean = ALIAS_RE.sub(" ", text or "")
    out = set()
    for m in NUMBER_RE.finditer(clean):
        v = _num(m.group(1))
        if v is not None:
            out.add(round(v, 4))
    return out


def clean_text(text: str) -> str:
    t = ALIAS_RE.sub("", text or "")
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.sub(r"\s+([.,;:])", r"\1", t)
    return t


_ALIAS_RANGE_RE = re.compile(r"\s*\(?\b[CFBP]\d+(?:\.\d+)?\s*[–—-]\s*[CFBP]\d+(?:\.\d+)?\)?")
_EMPTY_PARENS_RE = re.compile(r"\(\s*[,;]?\s*\)")


def clean_reason(text: str) -> str:
    """Причина исключения для человека: без псевдонимов ID (F8, C1, F10–F15) и пустых скобок."""
    t = _ALIAS_RANGE_RE.sub("", text or "")
    t = re.sub(r"\(\s*(?:[CFBP]\d+(?:\.\d+)?\s*[,;]?\s*)+\)", "", t)
    t = clean_text(t)
    t = _EMPTY_PARENS_RE.sub("", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _norm_text(text: str) -> str:
    return re.sub(r"[^\wа-яё]+", " ", (text or "").lower()).strip()


class Validator:
    def __init__(self, view: PromptView, role: str, *, as_of: str | None = None) -> None:
        self.view = view
        self.role = role
        self.issues: list[QAIssue] = []
        self.limitations: list[str] = []
        self.extra_unknowns: list[UnknownOut] = []
        self.claim_text = " ".join(c.text for c in view.all_claims()).lower()
        self.limitations.extend(view.notes)  # неполнота кандидата, пропущенные цитаты
        allowed: set[float] = set()
        for c in view.all_claims():
            allowed |= numbers_in(c.text)
            for m in c.measurements:
                if isinstance(m, dict) and m.get("value") is not None:
                    allowed |= numbers_in(str(m.get("value")))
                    v = _num(str(m.get("value")).lstrip("+-"))
                    if v is not None:
                        allowed.add(round(v, 4))
            allowed |= numbers_in(str(c.time or ""))
        for f in view.features.values():
            allowed |= numbers_in(f"{f.value} {f.description}")
        for b in view.bank.values():
            allowed |= numbers_in(f"{b.title} {b.quote} {b.published_at or ''}")
        for s in view.sources.values():
            allowed |= numbers_in(f"{s.published_at or ''} {s.title or ''}")
        for q in view.quotes.values():
            allowed |= numbers_in(q.quote)  # дословные цитаты первоисточника — тоже данные
        for h in view.interests.values():
            allowed |= numbers_in(h.text)
        event = view.data.get("event", {}) if isinstance(view.data.get("event"), dict) else {}
        for title in [event.get("title") or ""] + list(event.get("other_titles") or []):
            allowed |= numbers_in(title)  # заголовок события — тоже текст источника
        allowed |= numbers_in(str(view.data.get("event", {}).get("published_at", "")))
        allowed |= numbers_in(as_of or "")
        self.allowed_numbers = allowed

    # ------------------------------------------------------------------ утилиты
    def issue(self, code: str, message: str, severity: str = "warn", path: str | None = None) -> None:
        self.issues.append(QAIssue(code=code, severity=severity, message=message[:300], path=path))  # type: ignore[arg-type]

    def _resolve(self, st: DraftStatement, path: str) -> tuple[list[str], list[str], list[str]]:
        claim_ids, evidence_ids, feature_ids = self._resolve_full(st, path)[:3]
        return claim_ids, evidence_ids, feature_ids

    def _resolve_full(self, st: DraftStatement, path: str) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
        """Псевдонимы → исходные ID. Модель иногда кладёт F/B в claim_ids и наоборот — принимаем.

        Пятый элемент — интересы-гипотезы (H*). Цитаты первоисточника (S*) добавляют свои
        ``evidence_id``; ссылка на несуществующий алиас цитаты/интереса отбрасывается (``unknown_ref``).
        """
        claim_ids, evidence_ids, feature_ids, bank_ids, interest_ids = [], [], [], [], []
        for ref in list(st.claim_ids) + list(st.feature_ids) + list(st.quote_ids) + list(st.interest_ids):
            c = self.view.resolve_claim(ref)
            if c is not None:
                claim_ids.append(c.claim_id)
                evidence_ids += c.evidence_ids
                continue
            f = self.view.resolve_feature(ref)
            if f is not None:
                feature_ids.append(f.feature_id)
                continue
            b = self.view.resolve_bank(ref)
            if b is not None:
                bank_ids.append(b.bank_chunk_id)
                continue
            q = self.view.resolve_quote(ref)
            if q is not None:
                evidence_ids.append(q.evidence_id)
                continue
            h = self.view.resolve_interest(ref)
            if h is not None:
                interest_ids.append(h.interest_id)
                continue
            self.issue("unknown_ref", f"Ссылка {ref} не существует и отброшена", "warn", path)
        return _uniq(claim_ids), _uniq(evidence_ids), _uniq(feature_ids), _uniq(bank_ids), _uniq(interest_ids)

    def _unsupported_numbers(self, text: str, extra_allowed: set[float] | None = None) -> set[float]:
        allowed = self.allowed_numbers | (extra_allowed or set())
        return {n for n in numbers_in(text) if n not in allowed}

    def _claims_modality(self, claim_ids: list[str]) -> tuple[bool, bool]:
        """(есть прогноз/сценарий, есть отрицание) среди указанных утверждений."""
        forecast = negated = False
        for c in self.view.all_claims():
            if c.claim_id in claim_ids:
                if (c.modality or "").lower() in ("forecast", "scenario", "hypothetical", "conditional", "planned_possible"):
                    forecast = True
                if c.scenario:
                    forecast = True
                if (c.polarity or "").lower() in ("negated", "denied", "refuted"):
                    negated = True
        return forecast, negated

    def _mentions_counterparty(self, text: str, feature_ids: list[str]) -> bool:
        if any(fid.startswith("counterparty") for fid in feature_ids):
            return True
        low = text.lower()
        for f in self.view.features.values():
            if f.feature_id.startswith("counterparty") and isinstance(f.value, str):
                name = f.value.split(":")[0]
                core = re.sub(r"^(ооо|ао|пао|оао|зао)\s*", "", name.lower()).strip("«»\" ")
                if core and core in low:
                    return True
        return True if re.search(r"контрагент|платеж|платёж", low) else False

    # ------------------------------------------------------------------ проверки
    def check_patterns(
        self, text: str, kind: str, feature_ids: list[str], path: str, section: str = "analysis"
    ) -> tuple[str | None, str, bool]:
        """Возвращает (действие, новый kind, conditional). Действие: None | 'drop' | 'weaken'."""
        hedged = bool(HEDGE_RE.search(text))
        action: str | None = None
        new_kind, conditional = kind, kind == "hypothesis"

        if IDENTIFICATION_RE.search(text):
            self.issue("identification", "Попытка отождествить обезличенную компанию", "error", path)
            return "drop", kind, conditional

        low = text.lower()
        for stem in STRONG_EVENT_STEMS if section != "step" else []:
            if stem in low and stem not in self.claim_text:
                self.issue("unsupported_strong_claim", f"«{stem}…» нет в источниках события", "error", path)
                if kind == "fact" or not hedged:
                    return "drop", kind, conditional
                action, new_kind, conditional = "weaken", "hypothesis", True

        for pat in DAMAGE_PATTERNS:
            if pat.search(text) and not hedged:
                self.issue("damage_as_fact", "Потенциальное влияние описано как свершившийся ущерб", "error", path)
                if kind == "fact":
                    return "drop", kind, conditional
                action, new_kind, conditional = "weaken", "hypothesis", True
                break

        if any(p.search(text) for p in SUPPLIER_PATTERNS) and self._mentions_counterparty(text, feature_ids):
            if kind != "hypothesis" or not hedged:
                self.issue("payment_as_supplier", "Тип отношений с контрагентом выведен из платежей", "warn", path)
                self.limitations.append("Тип отношений с контрагентом по платежам не подтверждён (это гипотеза).")
                if kind == "fact" and path.startswith("facts"):
                    return "drop", kind, conditional
                action, new_kind, conditional = "weaken", "hypothesis", True

        if any(p.search(text) for p in CONTRACT_PATTERNS) and CONTRACT_CHANGE_RE.search(text) and not hedged:
            self.issue("contract_terms", "Изменение условий действующих договоров без данных о них", "warn", path)
            self.extra_unknowns.append(
                UnknownOut(
                    text="Тип ставки и условия действующих кредитных договоров неизвестны.",
                    question="Есть ли у компании кредиты с плавающей ставкой и какова их доля?",
                )
            )
            action, new_kind, conditional = "weaken", "hypothesis", True

        if REVENUE_PERIOD_RE.search(text) and numbers_in(text):
            self.issue("revenue_period", "Выручка пересчитана в период, которого нет в данных", "error", path)
            return "drop", kind, conditional

        return action, new_kind, conditional

    def statement(self, st: DraftStatement | None, path: str, *, section: str) -> Statement | None:
        """section: facts | analysis | step."""
        if st is None or not (st.text or "").strip():
            return None
        text = clean_text(st.text)
        if text and (self.view.quotes or self.view.interests):
            text = clean_text(EXTRA_ALIAS_RE.sub("", text))  # алиасы S*/H* — только если такие блоки были в промпте
        if not text:
            return None
        claim_ids, evidence_ids, feature_ids, bank_ids, interest_ids = self._resolve_full(st, path)
        kind = st.kind
        if section != "facts" and interest_ids and not (claim_ids or feature_ids or bank_ids or evidence_ids) and kind == "fact":
            # Интерес ретривера — гипотеза о канале влияния: опираясь только на него, фактом быть нельзя.
            self.issue("interest_as_fact", "Утверждение опирается только на интерес-гипотезу — понижено до гипотезы", "warn", path)
            kind = "hypothesis"
        if section == "facts":
            kind = "fact"
            if not claim_ids:
                self.issue("fact_without_source", "Факт без утверждения-источника удалён", "error", path)
                return None
        elif kind == "fact" and not evidence_ids:
            # Контракт фронта: «факт» — только с цитатой источника (evidence_ids).
            kind = "inference"
        if section != "facts" and not (claim_ids or feature_ids or bank_ids or interest_ids or evidence_ids):
            self.issue("unsupported_statement", "Вывод без ссылок на данные", "error", path)
            return None

        bad_numbers = self._unsupported_numbers(text)
        if bad_numbers:
            nums = ", ".join(str(int(n) if n == int(n) else n) for n in sorted(bad_numbers))
            self.issue("number_not_in_data", f"Числа не из данных: {nums}", "error", path)
            return None

        action, kind2, conditional = self.check_patterns(text, kind, feature_ids, path, section)
        if action == "drop":
            return None
        if section == "facts":
            forecast, negated = self._claims_modality(claim_ids)
            if forecast and not FORECAST_MARK_RE.search(text):
                self.issue("forecast_as_fact", "Прогноз подан как факт — добавлена пометка", "warn", path)
                text = "Прогноз источника: " + text[0].lower() + text[1:]
            if negated and not NEGATION_MARK_RE.search(text):
                self.issue("negation_lost", "Отрицание потеряно — добавлена пометка", "warn", path)
                text = "Не подтверждено / опровергнуто: " + text[0].lower() + text[1:]
            kind2, conditional = "fact", False
        roles = list(st.roles) if section == "step" else []
        return Statement(
            text=text,
            kind=kind2,  # type: ignore[arg-type]
            claim_ids=claim_ids,
            evidence_ids=evidence_ids,
            feature_ids=feature_ids,
            bank_chunk_ids=bank_ids if section != "facts" else [],
            interest_ids=interest_ids if section != "facts" else [],
            conditional=conditional or kind2 == "hypothesis",
            roles=roles,  # type: ignore[arg-type]
        )

    def statements(self, items: list[DraftStatement], path: str, *, section: str, limit: int) -> list[Statement]:
        out, seen = [], set()
        for i, st in enumerate(items):
            res = self.statement(st, f"{path}[{i}]", section=section)
            if res is None:
                continue
            key = _norm_text(res.text)
            if key in seen:
                self.issue("duplicate_statement", "Повтор утверждения удалён", "info", f"{path}[{i}]")
                continue
            seen.add(key)
            out.append(res)
        return out[:limit]


RELATION_STATUSES = ("continuation", "escalation", "deescalation")


def validate_changes(draft: LLMDraft, view: PromptView, v: Validator) -> ChangesSincePrevious | None:
    """Проверка «что изменилось по сравнению с прошлым».

    * блока памяти в промпте не было (``view.previous is None``) — поле не формируем;
    * истории нет — ``new`` (память проверена) или ``unknown`` (не подключена), без ссылок;
    * ссылки — только на переданные записи памяти (``P*``) и утверждения (текущие ``C*``
      и сохранённые ``P*.*``); иначе ссылка отбрасывается, а связь без записи → ``unknown``;
    * числа в ``summary`` — только из текущих данных и сохранённых фактов.
    """
    prev = view.previous
    if prev is None:
        return None
    ch = draft.changes_since_previous
    path = "changes_since_previous"
    if not prev.items:
        if ch is not None and (ch.status in RELATION_STATUSES or ch.previous_insight_id):
            v.issue("changes_without_history", "Ссылка на историю, которой нет в памяти, — статус исправлен", "warn", path)
        return ChangesSincePrevious(status="new" if prev.checked else "unknown")
    if ch is None:
        v.issue("changes_missing", "Модель не сравнила событие с сохранённой историей", "info", path)
        return ChangesSincePrevious(status="unknown")

    status = ch.status
    item = None
    if ch.previous_insight_id:
        item = view.resolve_previous_item(ch.previous_insight_id)
        if item is None:
            v.issue("unknown_previous_ref", f"Запись памяти {ch.previous_insight_id} не передавалась в запрос", "error", path)
    claim_ids: list[str] = []
    prev_items_cited = []
    for ref in ch.claim_ids:
        c = view.resolve_claim(ref)
        if c is not None:
            claim_ids.append(c.claim_id)
            continue
        p = view.resolve_previous_claim(ref)
        if p is not None:
            claim_ids.append(p.claim_id)
            owner = next((it for it in prev.items if any(f.claim_id == p.claim_id for f in it.facts)), None)
            if owner is not None:
                prev_items_cited.append(owner)
            continue
        v.issue("unknown_ref", f"Ссылка {ref} в сравнении с прошлым не существует и отброшена", "warn", path)
    if status in RELATION_STATUSES and item is None:
        owners = {it.insight_id: it for it in prev_items_cited}
        if len(owners) == 1:
            item = next(iter(owners.values()))
        else:
            v.issue("changes_invalid_previous", "Связь с прошлым без существующей записи памяти — статус «unknown»", "error", path)
            status = "unknown"
    if status not in RELATION_STATUSES:
        item = None
        if status == "new":
            claim_ids = [x for x in claim_ids if view.resolve_claim(x) is not None]

    summary = clean_text(ch.summary) if ch.summary else None
    if summary:
        prev_text = " ".join(f.text for f in prev.all_facts())
        allowed = set(numbers_in(prev_text))
        bad = v._unsupported_numbers(summary, allowed)
        low = summary.lower()
        strong = [s for s in STRONG_EVENT_STEMS if s in low and s not in v.claim_text and s not in prev_text.lower()]
        if bad or strong:
            what = f"числа не из данных: {', '.join(str(n) for n in sorted(bad))}" if bad else f"«{strong[0]}…» нет в источниках"
            v.issue("changes_unsupported", f"Сравнение с прошлым снято: {what}", "error", path)
            summary = None
            if status in RELATION_STATUSES:
                status, item = "unknown", None
    return ChangesSincePrevious(
        status=status,  # type: ignore[arg-type]
        previous_insight_id=item.insight_id if item is not None else None,
        summary=summary,
        claim_ids=_uniq(claim_ids),
    )


def validate_draft(draft: LLMDraft, view: PromptView, role: str, *, as_of: str | None = None) -> QAResult:
    v = Validator(view, role, as_of=as_of)
    event_title = (view.data.get("event") or {}).get("title") or draft.headline

    headline = clean_text(draft.headline) or event_title
    if v._unsupported_numbers(headline) or any(p.search(headline) for p in DAMAGE_PATTERNS) or any(
        stem in headline.lower() and stem not in v.claim_text for stem in STRONG_EVENT_STEMS
    ) or IDENTIFICATION_RE.search(headline):
        v.issue("headline_rewritten", "Заголовок содержал неподтверждённое утверждение и заменён заголовком события", "warn", "headline")
        headline = event_title

    facts = v.statements(draft.what_happened, "facts", section="facts", limit=5)
    # Противоречия не должны исчезать: все варианты из conflicts остаются среди фактов.
    for conflict in view.conflicts:
        desc = conflict.get("description") or "Источники расходятся"
        v.limitations.append(f"Источники расходятся: {desc}")
        v.extra_unknowns.append(UnknownOut(text=f"Источники расходятся: {desc}", how_to_clarify="Дождаться официального подтверждения или первоисточника."))
        present = {cid for s in facts for cid in s.claim_ids}
        for variant in conflict.get("variants") or []:
            cid = variant.get("claim_id") if isinstance(variant, dict) else None
            claim = next((c for c in view.all_claims() if c.claim_id == cid), None)
            if claim is not None and claim.claim_id not in present:
                v.issue("conflict_variant_restored", f"Вариант противоречия {cid} возвращён в факты", "warn", "facts")
                facts.append(Statement(text=claim.text, kind="fact", claim_ids=[claim.claim_id], evidence_ids=list(claim.evidence_ids)))
    if not facts and view.all_claims():
        # Факты — только дословные утверждения источников (без пересказа модели).
        first = view.all_claims()[0]
        v.issue("facts_restored", "Факты взяты дословно из утверждений источника", "warn", "facts")
        facts = [Statement(text=first.text, kind="fact", claim_ids=[first.claim_id], evidence_ids=list(first.evidence_ids))]

    decision = draft.decision
    inference_status = draft.inference_status
    event_status = draft.event_status

    # Модальность события: прогноз/сценарий не становятся «произошло».
    claims = view.all_claims()
    modalities = {(c.modality or "").lower() for c in claims}
    polarities = {(c.polarity or "").lower() for c in claims}
    if claims and modalities <= {"forecast", "scenario", "hypothetical", "conditional"} and event_status == "occurred":
        v.issue("event_status_fixed", "Все утверждения — прогноз/сценарий; статус «произошло» исправлен", "warn", "event_status")
        event_status = "forecast" if "forecast" in modalities else "scenario"
    if claims and polarities <= {"negated", "denied", "refuted"} and event_status == "occurred":
        v.issue("event_status_fixed", "Утверждения опровергнуты; статус «произошло» исправлен", "warn", "event_status")
        event_status = "refuted"
    if view.conflicts and event_status == "occurred":
        event_status = "mixed"

    result = QAResult(decision=decision, inference_status=inference_status, event_status=event_status, headline=headline, facts=facts)
    result.changes = validate_changes(draft, view, v)

    if decision == "exclude":
        result.issues, result.limitations = v.issues, _uniq(v.limitations)
        result.unknowns = v.extra_unknowns
        return _finish(result, v)

    result.company_relevance = v.statement(draft.company_relevance, "company_relevance", section="analysis")
    result.impact_mechanism = v.statement(draft.impact_mechanism, "impact_mechanism", section="analysis")
    result.risks = v.statements(draft.risks, "risks", section="analysis", limit=3)
    result.opportunities = v.statements(draft.opportunities, "opportunities", section="analysis", limit=3)
    steps = v.statements(draft.next_steps, "next_steps", section="step", limit=4)
    for s in steps:
        if s.kind == "fact":
            s.kind = "inference"
    result.next_steps = sorted(steps, key=lambda s: 0 if role in s.roles else (1 if not s.roles else 2))
    result.ai_conclusion = v.statement(draft.ai_conclusion, "ai_conclusion", section="analysis")
    result.recommended_checks = [clean_text(c) for c in draft.recommended_checks if clean_text(c) and not v._unsupported_numbers(c)][:4]

    unknowns = []
    for i, u in enumerate(draft.unknowns):
        t = clean_text(u.text)
        if not t:
            continue
        if v._unsupported_numbers(t):
            v.issue("number_not_in_data", "Число не из данных в «неизвестном»", "warn", f"unknowns[{i}]")
            continue
        unknowns.append(UnknownOut(text=t, question=clean_text(u.question) if u.question else None))
    result.unknowns = unknowns[:4] + v.extra_unknowns

    # Позиция банка: только существующие фрагменты.
    bank_ids = []
    for ref in draft.bank_assessment.bank_chunk_ids:
        fr = view.resolve_bank(ref)
        if fr is None:
            v.issue("unknown_bank_ref", f"Фрагмент банка {ref} не существует", "error", "bank_assessment")
        else:
            bank_ids.append(fr.bank_chunk_id)
    status = draft.bank_assessment.status
    if not view.bank:
        if status != "not_found":
            v.issue("bank_invented", "Позиция банка заявлена без фрагментов аналитики", "error", "bank_assessment")
        status, bank_ids = "not_found", []
    elif status in ("found", "outdated") and not bank_ids:
        v.issue("bank_without_ref", "Позиция банка без ссылки на фрагмент", "warn", "bank_assessment")
        status = "not_found"
    elif status == "not_found":
        bank_ids = []
    if status == "found" and any(
        (fr.applicability == "outdated") for fr in view.bank.values() if fr.bank_chunk_id in bank_ids
    ):
        v.issue("bank_outdated", "Фрагмент банка старше события — помечен как устаревший", "warn", "bank_assessment")
        status = "outdated"
    result.bank_assessment = BankAssessment(status=status, bank_chunk_ids=_uniq(bank_ids))  # type: ignore[arg-type]
    if status == "outdated":
        v.limitations.append("Материал банка опубликован заметно раньше события и может быть неактуален.")

    # Оценка эффекта: только сценарий с допущениями и без чисел не из данных.
    est = draft.impact_estimate
    if est is not None:
        assumptions = [clean_text(a) for a in est.assumptions if clean_text(a)]
        st = DraftStatement(text=est.scenario, kind="hypothesis", claim_ids=est.claim_ids, feature_ids=est.feature_ids)
        claim_ids, evidence_ids, feature_ids = v._resolve(st, "impact_estimate")
        extra = set()
        for a in assumptions:
            extra |= numbers_in(a)
        scenario = clean_text(est.scenario)
        if not assumptions:
            v.issue("estimate_without_assumptions", "Оценка эффекта без допущений снята", "warn", "impact_estimate")
        elif v._unsupported_numbers(scenario + " " + est.metric, extra):
            v.issue("estimate_number_not_in_data", "В оценке эффекта числа не из данных и не из допущений — снята", "error", "impact_estimate")
        elif REVENUE_PERIOD_RE.search(scenario) or any(p.search(scenario) for p in DAMAGE_PATTERNS) and not HEDGE_RE.search(scenario):
            v.issue("estimate_as_fact", "Оценка эффекта подана как факт — снята", "error", "impact_estimate")
        elif not (claim_ids or feature_ids):
            v.issue("estimate_without_refs", "Оценка эффекта без ссылок на данные — снята", "warn", "impact_estimate")
        else:
            if not HEDGE_RE.search(scenario):
                scenario = "Сценарий: " + scenario
            result.impact_estimate = ImpactEstimateOut(
                metric=clean_text(est.metric), scenario=scenario, assumptions=assumptions,
                claim_ids=claim_ids, evidence_ids=evidence_ids, feature_ids=feature_ids,
            )

    # Согласованность решения.
    analysis = [s for s in (result.company_relevance, result.impact_mechanism, result.ai_conclusion) if s] + result.risks
    link_features = [f for s in (result.company_relevance, result.impact_mechanism) if s for f in s.feature_ids]
    if not analysis:
        v.issue("no_supported_analysis", "Не осталось подтверждённых выводов", "error", "decision")
        result.inference_status = "insufficient_evidence"
        if result.decision == "include":
            result.decision = "watch"
    elif result.decision == "include" and not link_features:
        v.issue("no_company_link", "Связь с компанией не опирается на признаки профиля — понижено до watch", "warn", "decision")
        result.decision = "watch"
        result.unknowns.append(UnknownOut(text="Связь события с компанией не подтверждена признаками профиля (неизвестно, затрагивает ли событие её бизнес)."))
        if result.inference_status == "supported":
            result.inference_status = "partially_supported"
    if result.inference_status == "insufficient_evidence" and result.decision == "include":
        result.decision = "watch"
    if result.company_relevance and result.company_relevance.kind == "hypothesis" and result.inference_status == "supported":
        result.inference_status = "partially_supported"

    # Язык.
    joined = " ".join(s.text for s in result.all_statements()) + " " + result.headline
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", joined)
    if letters and sum(1 for ch in letters if re.match(r"[А-Яа-яЁё]", ch)) / len(letters) < 0.6:
        v.issue("not_russian", "Ответ преимущественно не на русском языке", "error", "language")

    result.issues, result.limitations = v.issues, _uniq(v.limitations)
    return _finish(result, v)


def _finish(result: QAResult, v: Validator) -> QAResult:
    errors = sum(1 for i in result.issues if i.severity == "error")
    warns = sum(1 for i in result.issues if i.severity == "warn")
    result.penalty = min(0.2, 0.05 * errors)
    if result.inference_status == "insufficient_evidence" and errors:
        result.status = "failed"
    elif errors or warns:
        result.status = "passed_with_fixes"
    else:
        result.status = "passed"
    stmts = result.all_statements()
    result.statements_total = len(stmts)
    result.statements_with_refs = sum(1 for s in stmts if s.claim_ids or s.feature_ids or s.evidence_ids or s.bank_chunk_ids or s.interest_ids)
    return result
