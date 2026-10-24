"""Единый шлюз к LLM организаторов (OpenAI-совместимый Chat Completions).

Все вызовы генеративных моделей в бэкенде проходят только через этот модуль:

* модели — только из списка организаторов (``models.json``), иначе ``ValueError``;
* учёт токенов и рублей: фактическая стоимость берётся из ``usage.cost`` шлюза
  (так считает и ``/key/info``), при её отсутствии — по тарифу из ``models.json``;
* дневной потолок расхода команды (по умолчанию 400 ₽ из 500 ₽, сброс в 03:00 МСК),
  резерв бюджета до запроса и сверка с ``GET /key/info`` (поле ``info.spend``);
* таймауты, ограниченные ретраи с экспоненциальной паузой, запасной адрес шлюза;
* защита от невалидного JSON: не более двух повторов с ремонтным промптом;
* маскирование секретов в логах и сообщениях об ошибках; ключ нигде не выводится;
* потокобезопасность (FastAPI вызывает синхронный код из пула потоков).

Интерфейс подменяемый: ``FakeLLM`` отдаёт записанные ответы без сети (тесты, офлайн).
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

import httpx

from app.analyst import prompts

T = TypeVar("T")
MSK = timezone(timedelta(hours=3), name="MSK")
RESET_HOUR_MSK = 3
MODELS_FILE = Path(__file__).resolve().parent / "models.json"
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]{6,}"),
    re.compile(r"(?i)(\"?(?:api[_-]?key|authorization)\"?\s*[:=]\s*\"?)[^\s\",]{6,}"),
]
_known_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Запомнить секрет, чтобы вычищать его из любых сообщений."""
    if value and len(value) >= 6:
        with _secrets_lock:
            _known_secrets.add(value)


def mask_secrets(text: Any) -> str:
    """Возвращает строку без ключей: известные секреты, ``sk-…``, ``Bearer …``, ``api_key=…``."""
    s = str(text)
    with _secrets_lock:
        secrets = list(_known_secrets)
    for secret in secrets:
        if secret in s:
            s = s.replace(secret, "***")
    s = _SECRET_PATTERNS[0].sub("sk-***", s)
    s = _SECRET_PATTERNS[1].sub(lambda m: m.group(1) + "***", s)
    s = _SECRET_PATTERNS[2].sub(lambda m: m.group(1) + "***", s)
    return s


class SecretMaskingFilter(logging.Filter):
    """Фильтр логов: прогоняет итоговое сообщение через ``mask_secrets``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        masked = mask_secrets(message)
        if masked != message:
            record.msg = masked
            record.args = None
        return True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(isinstance(f, SecretMaskingFilter) for f in logger.filters):
        logger.addFilter(SecretMaskingFilter())
    return logger


logger = get_logger("app.analyst.llm")


# --------------------------------------------------------------------------- модели


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    display_name: str
    input_rub_per_m: float
    output_rub_per_m: float
    json_mode: bool = True
    extra_body: dict[str, Any] = field(default_factory=dict)

    def tariff_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.input_rub_per_m + completion_tokens * self.output_rub_per_m) / 1e6


@dataclass(frozen=True)
class ModelRegistry:
    models: dict[str, ModelInfo]
    billing_safety_multiplier: float = 4.0

    def get(self, model_id: str) -> ModelInfo:
        if model_id not in self.models:
            raise ValueError(
                f"Модель «{model_id}» не входит в список разрешённых организаторами: {sorted(self.models)}"
            )
        return self.models[model_id]


_registry_cache: ModelRegistry | None = None


def load_model_registry(path: Path = MODELS_FILE) -> ModelRegistry:
    global _registry_cache
    if _registry_cache is not None and path == MODELS_FILE:
        return _registry_cache
    raw = json.loads(path.read_text(encoding="utf-8"))
    models = {
        mid: ModelInfo(
            model_id=mid,
            display_name=m.get("display_name", mid),
            input_rub_per_m=float(m["input_rub_per_m"]),
            output_rub_per_m=float(m["output_rub_per_m"]),
            json_mode=bool(m.get("json_mode", True)),
            extra_body=dict(m.get("extra_body") or {}),
        )
        for mid, m in raw["models"].items()
    }
    registry = ModelRegistry(models=models, billing_safety_multiplier=float(raw.get("billing_safety_multiplier", 4.0)))
    if path == MODELS_FILE:
        _registry_cache = registry
    return registry


# --------------------------------------------------------------------------- ошибки


class LLMError(Exception):
    """Базовая ошибка шлюза. Текст уже очищен от секретов."""

    def __init__(self, message: str, *, code: str = "llm_error") -> None:
        super().__init__(mask_secrets(message))
        self.code = code


class LLMUnavailable(LLMError):
    """Нет ключа, шлюз недоступен или отказал в доступе."""

    def __init__(self, message: str, *, code: str = "unavailable") -> None:
        super().__init__(message, code=code)


class BudgetExceeded(LLMError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="budget_exhausted")


class JsonRepairFailed(LLMError):
    def __init__(self, message: str, *, usage: "JsonCallInfo | None" = None) -> None:
        super().__init__(message, code="invalid_json")
        self.usage = usage


# --------------------------------------------------------------------------- бюджет


def budget_window_start(now: datetime) -> datetime:
    """Начало текущих бюджетных суток: последний момент 03:00 по Москве."""
    msk = now.astimezone(MSK)
    start = msk.replace(hour=RESET_HOUR_MSK, minute=0, second=0, microsecond=0)
    if msk < start:
        start -= timedelta(days=1)
    return start


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BudgetLedger:
    """Учёт расхода: локальный счёт процесса + последнее известное ``info.spend`` команды.

    Оценка расхода команды = ``spend`` из ``/key/info`` + то, что этот процесс
    потратил после последней сверки. Если сверки не было — только локальный счёт.
    Перед запросом резервируется оценка его максимальной стоимости, после ответа
    резерв заменяется фактической стоимостью. Сутки — с 03:00 МСК.
    """

    def __init__(
        self,
        ceiling_rub: float,
        *,
        session_cap_rub: float | None = None,
        state_path: Path | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.ceiling_rub = float(ceiling_rub)
        self.session_cap_rub = session_cap_rub
        self.state_path = state_path
        self._clock = clock
        self._lock = threading.Lock()
        self.window_start = budget_window_start(clock())
        self.spent_window_rub = 0.0
        self.session_spent_rub = 0.0
        self.reserved_rub = 0.0
        self.remote_spend_rub: float | None = None
        self.remote_max_budget_rub: float | None = None
        self.remote_reset_at: str | None = None
        self.remote_fetched_at: datetime | None = None
        self._local_at_remote_fetch = 0.0
        self._load_state()

    # -- состояние на диске (чтобы перезапуск не обнулял счёт за сутки)
    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data.get("window_start") == self.window_start.isoformat():
                self.spent_window_rub = float(data.get("spent_window_rub", 0.0))
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось прочитать состояние бюджета, начинаю с нуля")

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"window_start": self.window_start.isoformat(), "spent_window_rub": self.spent_window_rub}
                ),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось сохранить состояние бюджета")

    def _roll(self) -> None:
        start = budget_window_start(self._clock())
        if start != self.window_start:
            self.window_start = start
            self.spent_window_rub = 0.0
            self.remote_spend_rub = None
            self.remote_fetched_at = None
            self._local_at_remote_fetch = 0.0

    @property
    def effective_ceiling_rub(self) -> float:
        if self.remote_max_budget_rub is not None:
            return min(self.ceiling_rub, self.remote_max_budget_rub)
        return self.ceiling_rub

    def team_spend_estimate(self) -> float:
        if self.remote_spend_rub is None:
            return self.spent_window_rub
        return self.remote_spend_rub + max(0.0, self.spent_window_rub - self._local_at_remote_fetch)

    def local_spent_since_remote(self) -> float:
        return max(0.0, self.spent_window_rub - self._local_at_remote_fetch)

    def update_remote(self, spend: float, max_budget: float | None = None, reset_at: str | None = None) -> None:
        with self._lock:
            self._roll()
            self.remote_spend_rub = float(spend)
            self.remote_max_budget_rub = float(max_budget) if max_budget is not None else None
            self.remote_reset_at = reset_at
            self.remote_fetched_at = self._clock()
            self._local_at_remote_fetch = self.spent_window_rub

    def reserve(self, estimate_rub: float) -> float:
        with self._lock:
            self._roll()
            projected = self.team_spend_estimate() + self.reserved_rub + estimate_rub
            if projected > self.effective_ceiling_rub:
                raise BudgetExceeded(
                    f"Дневной потолок {self.effective_ceiling_rub:.2f} ₽ будет превышен "
                    f"(оценка расхода команды {self.team_spend_estimate():.2f} ₽ + резерв {estimate_rub:.3f} ₽)"
                )
            if self.session_cap_rub is not None and (
                self.session_spent_rub + self.reserved_rub + estimate_rub > self.session_cap_rub
            ):
                raise BudgetExceeded(
                    f"Лимит процесса {self.session_cap_rub:.2f} ₽ будет превышен "
                    f"(уже {self.session_spent_rub:.3f} ₽)"
                )
            self.reserved_rub += estimate_rub
            return estimate_rub

    def commit(self, reserved_rub: float, actual_rub: float) -> None:
        with self._lock:
            self._roll()
            self.reserved_rub = max(0.0, self.reserved_rub - reserved_rub)
            self.spent_window_rub += actual_rub
            self.session_spent_rub += actual_rub
            self._save_state()

    def release(self, reserved_rub: float) -> None:
        with self._lock:
            self.reserved_rub = max(0.0, self.reserved_rub - reserved_rub)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._roll()
            next_reset = self.window_start + timedelta(days=1)
            return {
                "ceiling_rub": round(self.effective_ceiling_rub, 2),
                "spent_window_rub": round(self.spent_window_rub, 4),
                "session_spent_rub": round(self.session_spent_rub, 4),
                "reserved_rub": round(self.reserved_rub, 4),
                "team_spend_rub": None if self.remote_spend_rub is None else round(self.team_spend_estimate(), 4),
                "team_spend_checked_at": self.remote_fetched_at.isoformat() if self.remote_fetched_at else None,
                "remaining_rub": round(max(0.0, self.effective_ceiling_rub - self.team_spend_estimate()), 2),
                "window_start": self.window_start.isoformat(),
                "next_reset_at": next_reset.isoformat(),
                "reset_at": "03:00 MSK",
            }


# --------------------------------------------------------------------------- запрос/ответ


@dataclass(frozen=True)
class ChatRequest:
    messages: list[dict[str, str]]
    model: str
    max_tokens: int = 1200
    temperature: float = 0.1
    json_mode: bool = True
    purpose: str = "analysis"


@dataclass
class ChatResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_rub: float = 0.0
    cost_rub_tariff: float = 0.0
    cost_source: str = "tariff"
    latency_ms: int = 0
    attempts: int = 1
    finish_reason: str | None = None


@dataclass
class JsonCallInfo:
    model: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_rub: float = 0.0
    cost_rub_tariff: float = 0.0
    latency_ms: int = 0
    retries: int = 0
    json_repairs: int = 0
    repair_errors: list[str] = field(default_factory=list)

    def add(self, r: ChatResult) -> None:
        self.calls += 1
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.reasoning_tokens += r.reasoning_tokens
        self.cost_rub += r.cost_rub
        self.cost_rub_tariff += r.cost_rub_tariff
        self.latency_ms += r.latency_ms
        self.retries += max(0, r.attempts - 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_rub": round(self.cost_rub, 6),
            "cost_rub_tariff": round(self.cost_rub_tariff, 6),
            "latency_ms": self.latency_ms,
            "retries": self.retries,
            "json_repairs": self.json_repairs,
        }


class GatewayStats:
    """Счётчики для ``/metrics`` (потокобезопасно)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.failures = 0
        self.retries = 0
        self.json_repairs = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0
        self.cost_rub = 0.0
        self.latency_ms_total = 0
        self.per_model: dict[str, dict[str, float]] = {}

    def record(self, r: ChatResult) -> None:
        with self._lock:
            self.calls += 1
            self.retries += max(0, r.attempts - 1)
            self.prompt_tokens += r.prompt_tokens
            self.completion_tokens += r.completion_tokens
            self.reasoning_tokens += r.reasoning_tokens
            self.cost_rub += r.cost_rub
            self.latency_ms_total += r.latency_ms
            m = self.per_model.setdefault(r.model, {"calls": 0, "cost_rub": 0.0, "tokens": 0})
            m["calls"] += 1
            m["cost_rub"] = round(m["cost_rub"] + r.cost_rub, 6)
            m["tokens"] += r.prompt_tokens + r.completion_tokens

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1

    def record_repair(self) -> None:
        with self._lock:
            self.json_repairs += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "llm_calls": self.calls,
                "llm_failures": self.failures,
                "llm_retries": self.retries,
                "json_repairs": self.json_repairs,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "cost_rub": round(self.cost_rub, 4),
                "average_llm_latency_ms": round(self.latency_ms_total / self.calls) if self.calls else None,
                "per_model": json.loads(json.dumps(self.per_model)),
            }


# --------------------------------------------------------------------------- JSON


def extract_json_object(text: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа модели (снимает ```-обёртку и текст вокруг)."""
    if text is None:
        raise ValueError("пустой ответ модели")
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, flags=re.S)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("в ответе нет JSON-объекта")
    data = json.loads(s[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("ответ — не JSON-объект")
    return data


def estimate_tokens(messages: Iterable[dict[str, str]]) -> int:
    """Грубая оценка входных токенов: ~2,6 символа на токен для русского текста."""
    chars = sum(len(m.get("content") or "") for m in messages)
    return int(chars / 2.6) + 16


# --------------------------------------------------------------------------- клиенты


class BaseLLM:
    """Общий интерфейс: ``chat`` (один ответ) и ``chat_json`` (ответ + проверка + ремонт)."""

    def __init__(
        self,
        *,
        registry: ModelRegistry | None = None,
        ledger: BudgetLedger | None = None,
        stats: GatewayStats | None = None,
        max_json_repairs: int = 2,
    ) -> None:
        self.registry = registry or load_model_registry()
        self.ledger = ledger
        self.stats = stats or GatewayStats()
        self.max_json_repairs = min(2, max(0, max_json_repairs))

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    def chat(self, request: ChatRequest) -> ChatResult:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def chat_json(
        self,
        request: ChatRequest,
        validate: Callable[[dict[str, Any]], T],
    ) -> tuple[T, JsonCallInfo]:
        """Запрос с проверкой JSON. При ошибке разбора или схемы — не более 2 ремонтов."""
        info = JsonCallInfo(model=request.model)
        messages = list(request.messages)
        last_error = ""
        for attempt in range(self.max_json_repairs + 1):
            result = self.chat(replace(request, messages=messages))
            info.add(result)
            try:
                data = extract_json_object(result.text)
                return validate(data), info
            except Exception as exc:  # noqa: BLE001 - любая ошибка схемы ведёт к ремонту
                last_error = _short_error(exc)
                info.repair_errors.append(last_error)
                if attempt >= self.max_json_repairs:
                    break
                info.json_repairs += 1
                self.stats.record_repair()
                logger.info("Ответ %s не прошёл проверку JSON (%s), ремонт %d", request.model, last_error, attempt + 1)
                messages = list(request.messages) + [
                    {"role": "assistant", "content": (result.text or "")[:6000]},
                    {
                        "role": "user",
                        "content": prompts.repair_prompt(last_error, truncated=result.finish_reason == "length"),
                    },
                ]
        raise JsonRepairFailed(f"Модель не вернула валидный JSON: {last_error}", usage=info)


def _short_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"For further information visit \S+", "", text)
    return mask_secrets(text[:700])


class KeyInfoClient:
    """``GET <домен>/key/info`` (без ``/v1``): расход ключа за текущие сутки."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client, ttl_s: float = 60.0) -> None:
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        self.url = f"{root}/key/info"
        self._api_key = api_key
        self._client = client
        self.ttl_s = ttl_s
        self.last_error: str | None = None

    def fetch(self) -> dict[str, Any] | None:
        try:
            r = self._client.get(self.url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=10.0)
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}"
                return None
            info = (r.json() or {}).get("info") or {}
            spend = info.get("spend")
            if not isinstance(spend, (int, float)):
                self.last_error = "нет поля info.spend"
                return None
            self.last_error = None
            return {
                "spend": float(spend),
                "max_budget": info.get("max_budget") if isinstance(info.get("max_budget"), (int, float)) else None,
                "budget_reset_at": info.get("budget_reset_at"),
            }
        except Exception as exc:  # noqa: BLE001
            self.last_error = mask_secrets(type(exc).__name__)
            return None


class LLMGateway(BaseLLM):
    """Живой клиент шлюза организаторов."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        fallback_base_url: str = "",
        ledger: BudgetLedger,
        registry: ModelRegistry | None = None,
        stats: GatewayStats | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        max_json_repairs: int = 2,
        check_key_info: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        key_info_ttl_s: float = 60.0,
    ) -> None:
        super().__init__(registry=registry, ledger=ledger, stats=stats, max_json_repairs=max_json_repairs)
        register_secret(api_key)
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.fallback_base_url = (fallback_base_url or "").rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max(0, max_retries)
        self.check_key_info = check_key_info
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
            follow_redirects=False,
        )
        self.key_info = KeyInfoClient(self.base_url, api_key, self._client, ttl_s=key_info_ttl_s)
        self._key_info_lock = threading.Lock()
        self.ledger: BudgetLedger = ledger

    def __repr__(self) -> str:  # ключ не попадает в repr
        return f"LLMGateway(base_url={self.base_url!r}, key=***)"

    def available(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "no_key"
        snap = self.ledger.snapshot()
        if snap["remaining_rub"] <= 0:
            return False, "budget_exhausted"
        return True, "ok"

    def refresh_remote_spend(self, force: bool = False) -> dict[str, Any] | None:
        if not self.check_key_info or not self._api_key:
            return None
        with self._key_info_lock:
            fetched = self.ledger.remote_fetched_at
            stale = fetched is None or (utcnow() - fetched).total_seconds() > self.key_info.ttl_s
            drift = self.ledger.local_spent_since_remote() > 2.0
            if not (force or stale or drift):
                return None
            info = self.key_info.fetch()
            if info is not None:
                self.ledger.update_remote(info["spend"], info.get("max_budget"), info.get("budget_reset_at"))
            return info

    def _estimate_cost(self, model: ModelInfo, request: ChatRequest) -> float:
        prompt_tokens = estimate_tokens(request.messages)
        return model.tariff_cost(prompt_tokens, request.max_tokens) * self.registry.billing_safety_multiplier

    def chat(self, request: ChatRequest) -> ChatResult:
        model = self.registry.get(request.model)
        if not self._api_key:
            raise LLMUnavailable("Ключ LLM_API_KEY не задан", code="no_key")
        self.refresh_remote_spend()
        reserved = self.ledger.reserve(self._estimate_cost(model, request))
        try:
            result = self._send_with_retries(model, request)
        except Exception:
            self.ledger.release(reserved)
            self.stats.record_failure()
            raise
        self.ledger.commit(reserved, result.cost_rub)
        self.stats.record(result)
        return result

    def _body(self, model: ModelInfo, request: ChatRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model.model_id,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.json_mode and model.json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(model.extra_body)
        return body

    def _send_with_retries(self, model: ModelInfo, request: ChatRequest) -> ChatResult:
        body = self._body(model, request)
        urls = [self.base_url] * (self.max_retries + 1)
        if self.fallback_base_url and len(urls) > 1:
            urls[-1] = self.fallback_base_url
        last_error = "нет ответа"
        for attempt, base in enumerate(urls):
            if attempt:
                pause = min(8.0, 0.8 * (2 ** (attempt - 1))) + random.uniform(0, 0.3)
                logger.info("Повтор запроса к LLM через %.1f с (попытка %d): %s", pause, attempt + 1, last_error)
                self._sleep(pause)
            started = time.perf_counter()
            try:
                response = self._client.post(
                    f"{base}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            except httpx.TimeoutException as exc:
                last_error = f"таймаут ({type(exc).__name__})"
                continue
            except httpx.TransportError as exc:
                last_error = f"сеть ({type(exc).__name__})"
                continue
            latency_ms = int((time.perf_counter() - started) * 1000)
            if response.status_code == 200:
                return self._parse(model, response, latency_ms, attempt + 1)
            snippet = mask_secrets(response.text[:300])
            if "budget" in snippet.lower() and response.status_code in (400, 402, 429):
                raise BudgetExceeded(f"Шлюз сообщил об исчерпании бюджета (HTTP {response.status_code})")
            if response.status_code in (401, 403):
                raise LLMUnavailable(f"Шлюз отказал в доступе (HTTP {response.status_code})", code="auth")
            if response.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}"
                continue
            raise LLMError(f"Шлюз вернул HTTP {response.status_code}: {snippet}", code="http_error")
        raise LLMUnavailable(f"LLM недоступна после {len(urls)} попыток: {last_error}", code="unavailable")

    def _parse(self, model: ModelInfo, response: httpx.Response, latency_ms: int, attempts: int) -> ChatResult:
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Неожиданный формат ответа шлюза: {type(exc).__name__}", code="bad_response") from None
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = int(details.get("reasoning_tokens") or 0)
        tariff = model.tariff_cost(prompt_tokens, completion_tokens)
        reported = usage.get("cost")
        if isinstance(reported, (int, float)) and reported >= 0:
            cost, source = float(reported), "gateway"
        else:
            cost, source = tariff, "tariff"
        return ChatResult(
            text=message.get("content") or "",
            model=model.model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_rub=cost,
            cost_rub_tariff=tariff,
            cost_source=source,
            latency_ms=latency_ms,
            attempts=attempts,
            finish_reason=choice.get("finish_reason"),
        )


@dataclass
class FakeReply:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str = "stop"
    latency_ms: int = 5


class FakeLLM(BaseLLM):
    """Подмена шлюза без сети: отдаёт заранее записанные ответы.

    ``responder`` — список ответов (строка, ``FakeReply`` или исключение) по порядку
    либо функция ``ChatRequest -> str | FakeReply``. Стоимость считается по тарифу.
    """

    def __init__(
        self,
        responder: Callable[[ChatRequest], Any] | list[Any],
        *,
        ledger: BudgetLedger | None = None,
        registry: ModelRegistry | None = None,
        stats: GatewayStats | None = None,
        max_json_repairs: int = 2,
    ) -> None:
        super().__init__(registry=registry, ledger=ledger, stats=stats, max_json_repairs=max_json_repairs)
        self._responder = responder
        self._queue = list(responder) if isinstance(responder, list) else None
        self.requests: list[ChatRequest] = []
        self._lock = threading.Lock()

    def chat(self, request: ChatRequest) -> ChatResult:
        model = self.registry.get(request.model)
        with self._lock:
            self.requests.append(request)
            if self._queue is not None:
                if not self._queue:
                    raise LLMUnavailable("FakeLLM: записанные ответы закончились")
                reply = self._queue.pop(0)
            else:
                reply = self._responder(request)  # type: ignore[operator]
        if isinstance(reply, Exception):
            self.stats.record_failure()
            raise reply
        if isinstance(reply, str):
            reply = FakeReply(text=reply)
        prompt_tokens = reply.prompt_tokens if reply.prompt_tokens is not None else estimate_tokens(request.messages)
        completion_tokens = (
            reply.completion_tokens if reply.completion_tokens is not None else int(len(reply.text) / 2.6) + 1
        )
        cost = model.tariff_cost(prompt_tokens, completion_tokens)
        reserved = 0.0
        if self.ledger is not None:
            reserved = self.ledger.reserve(model.tariff_cost(estimate_tokens(request.messages), request.max_tokens))
        result = ChatResult(
            text=reply.text,
            model=model.model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_rub=cost,
            cost_rub_tariff=cost,
            cost_source="tariff",
            latency_ms=reply.latency_ms,
            finish_reason=reply.finish_reason,
        )
        if self.ledger is not None:
            self.ledger.commit(reserved, cost)
        self.stats.record(result)
        return result
