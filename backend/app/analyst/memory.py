"""Память фактов и решений аналитика: «что изменилось по сравнению с прошлым».

Хранилище — SQLite из стандартной библиотеки (путь настраивается, по умолчанию
``/data/analyst_memory.sqlite3`` в Docker или временная папка ОС; в репозиторий
не попадает). Ключ записи — ``(company_id, role, event_id)``; повторная запись того
же содержимого ничего не меняет (идемпотентность), более старая версия события не
затирает новую.

Что хранится по событию: дата публикации, тема, заголовок, решение аналитика
(include/watch/exclude), краткий вывод и ключевые факты — ДОСЛОВНЫЕ утверждения
источника с ``claim_id``, числа и сущности из них.

Поиск предыдущего контекста для нового кандидата (без новых зависимостей):

* та же компания и роль, другое событие;
* событие опубликовано не позже ``as_of`` и строго раньше текущего (порядок
  ``(published_at, event_id)``) — никаких «знаний из будущего»;
* решение не ``exclude`` (шум не превращается в историю);
* лексическая близость: пересечение основ слов (первые 5 букв) заголовка и
  утверждений ≥ ``MIN_OVERLAP`` и доля пересечения ≥ ``MIN_RATIO`` (или совпала тема);
* в промпт попадает не более ~600 токенов (оценка — байты UTF-8 / 4); утверждения
  включаются целиком или не включаются, цитаты не обрезаются.

Детерминизм: результат для кандидата зависит только от содержимого памяти по
событиям, опубликованным раньше него. ``BusinessAnalyst`` обрабатывает пакет по
«уровням»: связанные кандидаты — в хронологическом порядке, несвязанные —
параллельно, поэтому порядок кандидатов во входе на результат не влияет, а
повторный запуск того же пакета даёт тот же контекст (и попадает в кэш).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.analyst.llm_gateway import get_logger
from app.analyst.schemas import Candidate, CandidateOutcome

logger = get_logger("app.analyst.memory")

MEMORY_SCHEMA_VERSION = 1
CONTEXT_TOKEN_BUDGET = 600
TOP_K = 3
MIN_OVERLAP = 3
MIN_RATIO = 0.25
MAX_FACTS_PER_ITEM = 3
SUMMARY_CHARS = 220

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9-]*")
_NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?")
_STOP_STEMS = {
    "котор", "также", "сообщ", "завил", "заяви", "росси", "москв", "этого", "этом", "после", "более", "менее",
    "около", "года", "году", "время", "врем", "будет", "может", "могут", "котор", "через", "между", "чтобы",
    "только", "такж", "своих", "свои", "своей", "ранее", "сейча", "сегод", "вчера", "четве", "пятни", "среду",
    "вторн", "понед", "суббо", "воскр", "январ", "февра", "марта", "апрел", "июня", "июля", "авгус", "сентя",
    "октяб", "ноябр", "декаб", "inter", "интер", "котор", "однак", "всего", "этой", "этих", "этот",
}
_TOPIC_RULES: list[tuple[str, str]] = [
    ("rates", r"ключев\w* ставк|ставк\w* ЦБ|Банк\w* России|ОФЗ|облигац|ДКП|денежно-кредит"),
    ("fx", r"курс\w* (рубл|валют|доллар|евро|юан)|ослаблен\w* рубл|укреплен\w* рубл"),
    ("inflation", r"инфляц|потребительск\w* цен|подорож"),
    ("taxes", r"налог|НДС|вычет|ФНС"),
    ("trade", r"пошлин|экспорт|импорт|таможен|санкц|квот"),
    ("transport", r"аэропорт|рейс|авиа|железнодорож|перевоз|логист|порт\b"),
    ("energy", r"нефт|газ\b|топлив|нефтепродукт|электроэнерг"),
]


def stems(text: str) -> set[str]:
    """Основы слов (первые 5 букв) для лексического сравнения; цифры и служебные слова не учитываются."""
    out = set()
    for w in _WORD_RE.findall(text or ""):
        w = w.lower().replace("ё", "е")
        if len(w) <= 3:
            continue
        s = w[:5]
        if s not in _STOP_STEMS:
            out.add(s)
    return out


def detect_topic(title: str, text: str, event_type: str | None = None) -> str:
    if event_type and event_type not in ("other", "unknown"):
        return event_type
    joined = f"{title} {text}"
    for topic, pattern in _TOPIC_RULES:
        if re.search(pattern, joined, flags=re.I):
            return topic
    return "other"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v + "T00:00:00" if len(v) == 10 else v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
    return dt


def _ts(value: str | None) -> float:
    dt = _parse_dt(value)
    return dt.timestamp() if dt else 0.0


def _tokens(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) // 4


def _short(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:—-") + "…"


# =========================================================================== записи


@dataclass(frozen=True)
class MemoryFact:
    claim_id: str
    text: str
    numbers: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"claim_id": self.claim_id, "text": self.text, "numbers": list(self.numbers), "entities": list(self.entities)}


@dataclass(frozen=True)
class MemoryItem:
    company_id: str
    role: str
    event_id: str
    event_version: int
    published_at: str
    topic: str
    title: str
    decision: str
    summary: str
    facts: tuple[MemoryFact, ...]
    stems: frozenset[str] = frozenset()

    @property
    def insight_id(self) -> str:
        return f"mem:{self.company_id}:{self.role}:{self.event_id}@v{self.event_version}"

    def content(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "event_version": self.event_version, "published_at": self.published_at,
            "topic": self.topic, "title": self.title, "decision": self.decision, "summary": self.summary,
            "facts": [f.as_dict() for f in self.facts],
        }

    def content_hash(self) -> str:
        raw = json.dumps(self.content(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CandidateSignature:
    """Что нужно памяти о кандидате до анализа: идентичность, дата, тема, основы слов."""

    event_id: str
    event_version: int
    published_at: str
    title: str
    topic: str
    stems: frozenset[str]

    @property
    def order_key(self) -> tuple[float, str]:
        return (_ts(self.published_at), self.event_id)


def _bundle_text(cand: Candidate) -> tuple[str, str, str | None, str, str, int]:
    bundle = cand.analysis_projection.event_bundles[0] if cand.analysis_projection.event_bundles else None
    if bundle is None:
        ref = cand.event_bundle_refs[0]
        return "", "", None, ref.event_id, "", ref.event_version
    ev = bundle.event
    claims_text = " ".join(c.text for b in cand.analysis_projection.event_bundles for c in b.claims)
    return ev.title or "", claims_text, ev.event_type, ev.event_id, ev.first_published_at or "", int(ev.event_version or 1)


def signature(cand: Candidate) -> CandidateSignature:
    title, text, event_type, event_id, published, version = _bundle_text(cand)
    return CandidateSignature(
        event_id=event_id, event_version=version, published_at=published, title=title,
        topic=detect_topic(title, text, event_type), stems=frozenset(stems(f"{title} {text}")),
    )


def similarity(a_stems: Iterable[str], a_topic: str, b_stems: Iterable[str], b_topic: str) -> tuple[bool, float]:
    """(связаны ли события, балл). Балл — пересечение основ + 2 за совпадение темы."""
    a, b = set(a_stems), set(b_stems)
    overlap = len(a & b)
    same_topic = a_topic == b_topic and a_topic != "other"
    if not a or not b:
        return False, 0.0
    ratio = overlap / min(len(a), len(b))
    related = overlap >= MIN_OVERLAP and (ratio >= MIN_RATIO or same_topic)
    return related, overlap + (2.0 if same_topic else 0.0)


def related(a: CandidateSignature, b: CandidateSignature) -> bool:
    return similarity(a.stems, a.topic, b.stems, b.topic)[0]


def _entities(claim: Any) -> list[str]:
    out: list[str] = []
    for key in ("participants", "geographies"):
        for x in getattr(claim, key, None) or []:
            name = x.get("name") if isinstance(x, dict) else x
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    obj = getattr(claim, "object", None)
    if isinstance(obj, str) and obj.strip():
        out.append(obj.strip())
    # Имена собственные из текста (кроме первого слова предложения).
    text = claim.text or ""
    for m in re.finditer(r"\b[А-ЯЁA-Z][а-яёa-z]{3,}(?:\s[А-ЯЁA-Z][а-яёa-z]{2,})?", text):
        before = text[: m.start()].rstrip()
        if before and before[-1] not in ".!?":
            out.append(m.group(0))
    seen, uniq = set(), []
    for x in out:
        if x.lower() not in seen:
            seen.add(x.lower())
            uniq.append(x)
    return uniq[:8]


def _numbers(claim: Any) -> list[str]:
    nums = [m.group(0) for m in _NUM_RE.finditer(claim.text or "")]
    for m in getattr(claim, "measurements", None) or []:
        if isinstance(m, dict) and m.get("value") is not None:
            unit = m.get("unit") or ""
            nums.append(f"{m.get('value')}{(' ' + unit) if unit else ''}")
    return list(dict.fromkeys(nums))[:8]


def item_from_outcome(company_id: str, role: str, cand: Candidate, outcome: CandidateOutcome) -> MemoryItem | None:
    """Нормализованная запись памяти из результата анализа (правила precheck не сохраняются)."""
    if outcome.run.generated_by == "rules":
        return None
    sig = signature(cand)
    claims = [c for b in cand.analysis_projection.event_bundles for c in b.claims]
    if not claims:
        return None
    ins = outcome.insight
    if ins is not None:
        decision = ins.decision
        conclusion = ins.ai_conclusion or ins.company_relevance or ins.impact_mechanism
        summary = conclusion.text if conclusion else ins.headline
    elif outcome.excluded is not None:
        decision = "exclude"
        summary = outcome.excluded.reason
    else:
        return None
    facts = tuple(
        MemoryFact(claim_id=c.claim_id, text=c.text, numbers=tuple(_numbers(c)), entities=tuple(_entities(c)))
        for c in claims[:MAX_FACTS_PER_ITEM]
    )
    return MemoryItem(
        company_id=company_id, role=role, event_id=sig.event_id, event_version=sig.event_version,
        published_at=sig.published_at, topic=sig.topic, title=sig.title, decision=decision,
        summary=_short(summary), facts=facts, stems=sig.stems,
    )


# =========================================================================== контекст для промпта


@dataclass
class PreviousContext:
    """Блок «предыдущий контекст» для одного кандидата.

    ``checked`` — память подключена и просмотрена (тогда отсутствие записей означает
    «нового события», иначе — «неизвестно»). Псевдонимы: ``P1`` — запись, ``P1.1`` — её факт.
    """

    items: list[MemoryItem] = field(default_factory=list)
    checked: bool = False
    _facts: dict[str, list[MemoryFact]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for i, it in enumerate(self.items, start=1):
            self._facts.setdefault(f"P{i}", list(it.facts))

    @classmethod
    def build(cls, scored: list[MemoryItem], *, budget_tokens: int = CONTEXT_TOKEN_BUDGET, top_k: int = TOP_K) -> "PreviousContext":
        ctx = cls(items=[], checked=True)
        blocks: list[dict[str, Any]] = []
        for it in scored:
            if len(ctx.items) >= top_k:
                break
            alias = f"P{len(ctx.items) + 1}"
            header = {"id": alias, "date": it.published_at[:10], "title": it.title, "decision": it.decision,
                      "conclusion": it.summary, "facts": []}
            if _tokens(blocks + [header]) > budget_tokens:
                break
            kept: list[MemoryFact] = []
            for j, f in enumerate(it.facts, start=1):
                trial = dict(header, facts=header["facts"] + [{"id": f"{alias}.{j}", "text": f.text}])
                if _tokens(blocks + [trial]) > budget_tokens:
                    break  # утверждение не помещается целиком — не обрезаем, останавливаемся
                header = trial
                kept.append(f)
            if not kept:
                break
            blocks.append(header)
            ctx.items.append(it)
            ctx._facts[alias] = kept
        return ctx

    # ------------------------------------------------------------------ псевдонимы
    def facts_of(self, alias: str) -> list[MemoryFact]:
        return self._facts.get(alias, [])

    def block(self) -> list[dict[str, Any]]:
        out = []
        for i, it in enumerate(self.items, start=1):
            alias = f"P{i}"
            out.append({
                "id": alias, "date": it.published_at[:10], "title": it.title, "decision": it.decision,
                "conclusion": it.summary,
                "facts": [{"id": f"{alias}.{j}", "text": f.text} for j, f in enumerate(self.facts_of(alias), start=1)],
            })
        return out

    def item_by_alias(self, ref: str) -> MemoryItem | None:
        ref = ref.strip().upper()
        if re.fullmatch(r"P\d+", ref):
            i = int(ref[1:])
            if 1 <= i <= len(self.items):
                return self.items[i - 1]
        return None

    def item_by_id(self, insight_id: str) -> MemoryItem | None:
        return next((it for it in self.items if it.insight_id == insight_id.strip()), None)

    def fact_by_alias(self, ref: str) -> MemoryFact | None:
        m = re.fullmatch(r"(P\d+)\.(\d+)", ref.strip().upper())
        if not m:
            return None
        facts = self.facts_of(m.group(1))
        j = int(m.group(2))
        return facts[j - 1] if 1 <= j <= len(facts) else None

    def fact_by_id(self, claim_id: str) -> MemoryFact | None:
        for alias in [f"P{i}" for i in range(1, len(self.items) + 1)]:
            for f in self.facts_of(alias):
                if f.claim_id == claim_id.strip():
                    return f
        return None

    def all_facts(self) -> list[MemoryFact]:
        return [f for i in range(1, len(self.items) + 1) for f in self.facts_of(f"P{i}")]

    def ref(self) -> str | None:
        """Отпечаток блока (по исходным ID и тексту) — входит в ключ кэша."""
        if not self.items:
            return None
        payload = [
            {"insight_id": it.insight_id, "summary": it.summary, "decision": it.decision,
             "facts": [(f.claim_id, f.text) for f in self.facts_of(f"P{i}")]}
            for i, it in enumerate(self.items, start=1)
        ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"mem{MEMORY_SCHEMA_VERSION}:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# =========================================================================== хранилище


def default_memory_path() -> Path:
    data_dir = Path("/data")
    if data_dir.is_dir() and os.access(data_dir, os.W_OK):
        return data_dir / "analyst_memory.sqlite3"
    return Path(tempfile.gettempdir()) / "alfa_analyst_memory.sqlite3"


class MemoryStore:
    """Потокобезопасное хранилище (одно соединение SQLite под блокировкой)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path) if path is not None else str(default_memory_path())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            c = self._conn
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is not None and row[0] != str(MEMORY_SCHEMA_VERSION):
                logger.warning("Схема памяти %s устарела (нужна %s) — память пересоздана", row[0], MEMORY_SCHEMA_VERSION)
                c.execute("DROP TABLE IF EXISTS memory_items")
            c.execute(
                """CREATE TABLE IF NOT EXISTS memory_items (
                    company_id TEXT NOT NULL, role TEXT NOT NULL, event_id TEXT NOT NULL,
                    event_version INTEGER NOT NULL, published_at TEXT NOT NULL, published_ts REAL NOT NULL,
                    topic TEXT NOT NULL, title TEXT NOT NULL, decision TEXT NOT NULL, summary TEXT NOT NULL,
                    facts_json TEXT NOT NULL, stems_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (company_id, role, event_id))"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS ix_memory_scope ON memory_items (company_id, role, published_ts)")
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(MEMORY_SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __del__(self) -> None:  # pragma: no cover - закрытие при сборке мусора
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ запись
    def record(self, item: MemoryItem) -> bool:
        """Сохраняет запись. True — содержимое изменилось; False — уже было (идемпотентно) или устарело."""
        h = item.content_hash()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT event_version, content_hash FROM memory_items WHERE company_id=? AND role=? AND event_id=?",
                (item.company_id, item.role, item.event_id),
            ).fetchone()
            if row is not None and (row[0] > item.event_version or row[1] == h):
                return False
            self._conn.execute(
                """INSERT OR REPLACE INTO memory_items (company_id, role, event_id, event_version, published_at,
                   published_ts, topic, title, decision, summary, facts_json, stems_json, content_hash, recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item.company_id, item.role, item.event_id, item.event_version, item.published_at, _ts(item.published_at),
                    item.topic, item.title, item.decision, item.summary,
                    json.dumps([f.as_dict() for f in item.facts], ensure_ascii=False),
                    json.dumps(sorted(item.stems), ensure_ascii=False), h,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            return True

    def record_outcome(self, company_id: str, role: str, cand: Candidate, outcome: CandidateOutcome) -> bool:
        item = item_from_outcome(company_id, role, cand, outcome)
        return self.record(item) if item is not None else False

    # ------------------------------------------------------------------ чтение
    @staticmethod
    def _row_to_item(row: tuple[Any, ...]) -> MemoryItem:
        (company_id, role, event_id, event_version, published_at, topic, title, decision, summary, facts_json, stems_json) = row
        facts = tuple(
            MemoryFact(claim_id=f["claim_id"], text=f["text"], numbers=tuple(f.get("numbers") or ()),
                       entities=tuple(f.get("entities") or ()))
            for f in json.loads(facts_json)
        )
        return MemoryItem(
            company_id=company_id, role=role, event_id=event_id, event_version=int(event_version),
            published_at=published_at, topic=topic, title=title, decision=decision, summary=summary,
            facts=facts, stems=frozenset(json.loads(stems_json)),
        )

    def items(self, company_id: str, role: str) -> list[MemoryItem]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT company_id, role, event_id, event_version, published_at, topic, title, decision, summary,
                   facts_json, stems_json FROM memory_items WHERE company_id=? AND role=?
                   ORDER BY published_ts, event_id""",
                (company_id, role),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0])

    def candidates_for(self, company_id: str, role: str, sig: CandidateSignature, as_of: datetime | None) -> list[tuple[float, MemoryItem]]:
        """Связанные записи, опубликованные не позже ``as_of`` и раньше текущего события."""
        as_of_ts = as_of.timestamp() if as_of else None
        out: list[tuple[float, MemoryItem]] = []
        for it in self.items(company_id, role):
            if it.event_id == sig.event_id or it.decision == "exclude":
                continue
            ts = _ts(it.published_at)
            if not it.published_at or (as_of_ts is not None and ts > as_of_ts):
                continue
            if (ts, it.event_id) >= sig.order_key:
                continue
            ok, score = similarity(sig.stems, sig.topic, it.stems, it.topic)
            if ok:
                out.append((score, it))
        out.sort(key=lambda x: (-x[0], -_ts(x[1].published_at), x[1].event_id))
        return out

    def context_for(
        self,
        company_id: str,
        role: str,
        cand: Candidate,
        as_of: datetime | None,
        *,
        budget_tokens: int = CONTEXT_TOKEN_BUDGET,
        top_k: int = TOP_K,
    ) -> PreviousContext:
        scored = self.candidates_for(company_id, role, signature(cand), as_of)
        return PreviousContext.build([it for _, it in scored], budget_tokens=budget_tokens, top_k=top_k)


def schedule_levels(cands: list[Candidate]) -> list[list[Candidate]]:
    """Уровни обработки пакета: связанный кандидат идёт после всех более ранних связанных.

    Внутри уровня кандидаты независимы (можно параллельно); уровни — по возрастанию.
    Порядок внутри уровня — по ``(published_at, event_id, candidate_id)``.
    """
    sigs = {c.candidate_id: signature(c) for c in cands}
    order = sorted(cands, key=lambda c: (*sigs[c.candidate_id].order_key, c.candidate_id))
    level: dict[str, int] = {}
    for i, c in enumerate(order):
        lv = 0
        for prev in order[:i]:
            if sigs[prev.candidate_id].order_key < sigs[c.candidate_id].order_key and related(sigs[c.candidate_id], sigs[prev.candidate_id]):
                lv = max(lv, level[prev.candidate_id] + 1)
        level[c.candidate_id] = lv
    out: list[list[Candidate]] = []
    for c in order:
        lv = level[c.candidate_id]
        while len(out) <= lv:
            out.append([])
        out[lv].append(c)
    return out
