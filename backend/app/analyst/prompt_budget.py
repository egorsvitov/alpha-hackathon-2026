"""Оценка размера полного промпта аналитика (системный + пользовательский + примеры).

Оценка — байты UTF-8 / 4 (как у ретривера в ``manifest.json``): для русского текста
она завышает реальное число токенов, то есть даёт запас. Точный токенизатор
провайдера недоступен без вызова API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.analyst import prompts
from app.analyst.schemas import AnalystInput, Candidate
from app.analyst.view import build_prompt_view

if TYPE_CHECKING:  # pragma: no cover
    from app.analyst.memory import PreviousContext


def messages_tokens(messages: list[dict[str, str]]) -> int:
    return sum(len((m.get("content") or "").encode("utf-8")) for m in messages) // 4


def estimate_prompt_tokens(
    inp: AnalystInput,
    cand: Candidate,
    prompt_version: str | None = None,
    previous: "PreviousContext | None" = None,
) -> int:
    version = prompt_version or prompts.PROMPT_VERSION
    if previous is None and prompts.supports_memory(version):
        from app.analyst.memory import PreviousContext

        previous = PreviousContext(items=[], checked=True)
    view = build_prompt_view(inp, cand, previous=previous)
    return messages_tokens(prompts.build_analyst_messages(view.data, version=version))
