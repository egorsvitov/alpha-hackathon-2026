"""Минимальный валидатор JSON Schema (draft 2020-12, подмножество) без внешних зависимостей.

Пакета ``jsonschema`` в зависимостях бэкенда нет, а новых зависимостей не добавляем.
Поддерживаются ключевые слова, которые используются в схемах ретривера
(``impact-candidate-batch.v1``, ``event-bundle.v0.2``): ``type``, ``enum``, ``const``,
``required``, ``properties``, ``additionalProperties`` (bool/схема), ``patternProperties``,
``items``, ``minItems``/``maxItems``, ``uniqueItems``, ``minLength``, ``pattern``,
``minimum``/``maximum``, ``minProperties``, ``anyOf``, ``$ref`` (только ``#/$defs/...``)
и ``format: date-time`` (проверка разбора ISO 8601). Неизвестные ключевые слова
игнорируются — это проверка формы, а не полная реализация стандарта.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


@lru_cache(maxsize=256)
def _regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _is_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return "T" in value or " " in value
    except ValueError:
        return False


class SchemaValidator:
    """``errors(instance)`` возвращает список ошибок вида ``/path: сообщение`` (не более ``limit``)."""

    def __init__(self, schema: dict[str, Any], *, limit: int = 50) -> None:
        self.schema = schema
        self.limit = limit

    def errors(self, instance: Any) -> list[str]:
        out: list[str] = []
        self._check(instance, self.schema, "", out)
        return out[: self.limit]

    def _resolve(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise ValueError(f"внешние $ref не поддерживаются: {ref}")
        node: Any = self.schema
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def _check(self, v: Any, s: Any, path: str, out: list[str]) -> None:
        if len(out) >= self.limit or s is True or s is None:
            return
        if s is False:
            out.append(f"{path or '/'}: значение не допускается схемой")
            return
        if "$ref" in s:
            self._check(v, self._resolve(s["$ref"]), path, out)
        where = path or "/"
        if "anyOf" in s:
            if not any(not self._sub_errors(v, alt, path) for alt in s["anyOf"]):
                out.append(f"{where}: не подходит ни под один вариант anyOf")
                return
        t = s.get("type")
        if t is not None:
            types = t if isinstance(t, list) else [t]
            if not any(_TYPES.get(x, lambda _v: True)(v) for x in types):
                out.append(f"{where}: ожидался тип {t}, получено {type(v).__name__}")
                return
        if "const" in s and v != s["const"]:
            out.append(f"{where}: ожидалось значение {s['const']!r}")
        if "enum" in s and v not in s["enum"]:
            out.append(f"{where}: значение {v!r} не из списка {s['enum']}")
        if isinstance(v, str):
            if len(v) < s.get("minLength", 0):
                out.append(f"{where}: строка короче {s['minLength']}")
            if "pattern" in s and not _regex(s["pattern"]).search(v):
                out.append(f"{where}: строка не соответствует шаблону {s['pattern']}")
            if s.get("format") == "date-time" and not _is_datetime(v):
                out.append(f"{where}: не дата-время ISO 8601")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in s and v < s["minimum"]:
                out.append(f"{where}: меньше {s['minimum']}")
            if "maximum" in s and v > s["maximum"]:
                out.append(f"{where}: больше {s['maximum']}")
        if isinstance(v, list):
            if len(v) < s.get("minItems", 0):
                out.append(f"{where}: элементов меньше {s['minItems']}")
            if "maxItems" in s and len(v) > s["maxItems"]:
                out.append(f"{where}: элементов больше {s['maxItems']}")
            if s.get("uniqueItems"):
                seen = [json.dumps(x, sort_keys=True, ensure_ascii=False) for x in v]
                if len(seen) != len(set(seen)):
                    out.append(f"{where}: элементы повторяются")
            items = s.get("items")
            if isinstance(items, dict):
                for i, x in enumerate(v):
                    self._check(x, items, f"{path}/{i}", out)
        if isinstance(v, dict):
            for key in s.get("required", []):
                if key not in v:
                    out.append(f"{where}: нет обязательного поля «{key}»")
            if len(v) < s.get("minProperties", 0):
                out.append(f"{where}: полей меньше {s['minProperties']}")
            props = s.get("properties", {})
            pprops = s.get("patternProperties", {})
            extra = s.get("additionalProperties", True)
            for key, val in v.items():
                sub = f"{path}/{key}"
                matched = False
                if key in props:
                    matched = True
                    self._check(val, props[key], sub, out)
                for pat, psch in pprops.items():
                    if _regex(pat).search(key):
                        matched = True
                        self._check(val, psch, sub, out)
                if not matched:
                    if extra is False:
                        out.append(f"{where}: лишнее поле «{key}»")
                    elif isinstance(extra, dict):
                        self._check(val, extra, sub, out)

    def _sub_errors(self, v: Any, s: Any, path: str) -> list[str]:
        tmp: list[str] = []
        self._check(v, s, path, tmp)
        return tmp


def load_validator(path: Path) -> SchemaValidator:
    return SchemaValidator(json.loads(Path(path).read_text(encoding="utf-8")))
