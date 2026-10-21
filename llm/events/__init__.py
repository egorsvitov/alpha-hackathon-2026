"""B2/B3 — модуль извлечения событий из статей.

  - state.py     — B2: версии статей, хэши, журнал, повторный запуск;
  - extract.py   — B3: извлечение SourceClaim через разрешённую LLM;
  - models.py    — B1: Pydantic-модели SourceClaim / CanonicalEvent;
  - pipeline.py  — оркестратор process_batch / get_event / export_events.
"""
from llm.events import extract, models, pipeline, state

__all__ = ["extract", "models", "pipeline", "state"]