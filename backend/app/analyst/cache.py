"""Кэш результатов аналитика: память → рабочий каталог (запись) → сохранённые ответы репозитория.

Ключ анализа: ``company_id | event_id@version#sha | role | profile_version | prompt_version | model``.
Повторный запрос с тем же ключом не тратит бюджет. «Мягкий» ключ (без модели)
используется только для офлайн-режима, когда живой вызов невозможен.

Сохранённые ответы (``app/analyst/cache/``) — синтетика, без ключей и персональных
данных; в Docker-образе каталог доступен только для чтения, рабочий кэш пишется в
``ANALYST_CACHE_DIR`` (по умолчанию ``/data/analyst_cache`` или временная папка ОС).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

from app.analyst.llm_gateway import get_logger, mask_secrets

logger = get_logger("app.analyst.cache")


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", part)[:60]


class AnalysisCache:
    def __init__(self, runtime_dir: Path | None, bundled_dir: Path | None) -> None:
        self.runtime_dir = runtime_dir
        self.bundled_dir = bundled_dir
        self._memory: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._index: dict[str, dict[str, dict[str, Any]]] = {}  # dir -> key -> envelope
        self._loose_index: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.stats = {"memory_hits": 0, "runtime_hits": 0, "bundled_hits": 0, "loose_hits": 0, "misses": 0, "writes": 0}
        self._runtime_writable = True

    # ------------------------------------------------------------------ блокировки
    def key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock

    # ------------------------------------------------------------------ индекс каталога
    def _load_dir(self, directory: Path | None) -> dict[str, dict[str, Any]]:
        if directory is None:
            return {}
        name = str(directory)
        with self._lock:
            if name in self._index:
                return self._index[name]
        index: dict[str, dict[str, Any]] = {}
        loose: dict[str, list[dict[str, Any]]] = {}
        if directory.is_dir():
            for path in directory.rglob("*.json"):
                try:
                    env = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    logger.warning("Пропущен повреждённый файл кэша %s", path.name)
                    continue
                if not isinstance(env, dict) or "key" not in env or "record" not in env:
                    continue
                index[env["key"]] = env
                if env.get("loose_key"):
                    loose.setdefault(env["loose_key"], []).append(env)
        with self._lock:
            self._index[name] = index
            self._loose_index[name] = loose
        return index

    def _invalidate(self, directory: Path | None) -> None:
        if directory is not None:
            with self._lock:
                self._index.pop(str(directory), None)
                self._loose_index.pop(str(directory), None)

    # ------------------------------------------------------------------ чтение
    def get(self, key: str) -> tuple[dict[str, Any] | None, str | None]:
        """Точное совпадение ключа. Возвращает (запись, источник: memory|runtime|bundled)."""
        with self._lock:
            if key in self._memory:
                self.stats["memory_hits"] += 1
                return self._memory[key], "memory"
        for source, directory in (("runtime", self.runtime_dir), ("bundled", self.bundled_dir)):
            env = self._load_dir(directory).get(key)
            if env is not None:
                with self._lock:
                    self._memory[key] = env["record"]
                    self.stats[f"{source}_hits"] += 1
                return env["record"], source
        with self._lock:
            self.stats["misses"] += 1
        return None, None

    def get_loose(self, loose_key: str, prefer_models: list[str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
        """Офлайн-поиск без учёта модели (сначала предпочтительные модели)."""
        for source, directory in (("runtime", self.runtime_dir), ("bundled", self.bundled_dir)):
            self._load_dir(directory)
            with self._lock:
                envs = list(self._loose_index.get(str(directory), {}).get(loose_key, [])) if directory else []
            if not envs:
                continue
            order = {m: i for i, m in enumerate(prefer_models or [])}
            envs.sort(key=lambda e: (order.get(e.get("model"), 99), e.get("created_at") or ""), reverse=False)
            with self._lock:
                self.stats["loose_hits"] += 1
            return envs[0]["record"], source
        return None, None

    # ------------------------------------------------------------------ запись
    def put(
        self,
        key: str,
        record: dict[str, Any],
        *,
        loose_key: str | None = None,
        label: str = "",
        model: str | None = None,
        created_at: str | None = None,
        bundled: bool = False,
    ) -> None:
        with self._lock:
            self._memory[key] = record
        directory = self.bundled_dir if bundled else self.runtime_dir
        if directory is None or (not bundled and not self._runtime_writable):
            return
        env = {"key": key, "loose_key": loose_key, "model": model, "created_at": created_at, "record": record}
        sub = _safe(label.split("/")[0]) if label else "misc"
        name = (_safe(label.replace("/", "__")) + "__" if label else "") + key_hash(key) + ".json"
        path = directory / sub / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(env, ensure_ascii=False, indent=1, sort_keys=False)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(mask_secrets(text) if "sk-" in text else text, encoding="utf-8")
            tmp.replace(path)
            with self._lock:
                self.stats["writes"] += 1
            self._invalidate(directory)
        except OSError as exc:
            if not bundled:
                self._runtime_writable = False
            logger.warning("Кэш недоступен для записи (%s): %s", directory, type(exc).__name__)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = dict(self.stats)
        hits = s["memory_hits"] + s["runtime_hits"] + s["bundled_hits"] + s["loose_hits"]
        total = hits + s["misses"]
        s["hit_ratio_pct"] = round(100.0 * hits / total, 1) if total else None
        return s
