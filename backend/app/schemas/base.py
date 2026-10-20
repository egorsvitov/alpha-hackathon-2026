from typing import Any
from pydantic import BaseModel, ConfigDict


# Базовые схемы-заглушки (будут согласованы и детализированы с Максом)
class CompanyBase(BaseModel):
    synthetic_u: str
    mainokved_name: str | None = None
    revenue_company: float | None = None
    city: str | None = None

    model_config = ConfigDict(from_attributes=True)


class NewsItemBase(BaseModel):
    id: str
    title: str
    published_at: str | None = None
    summary: str | None = None

    model_config = ConfigDict(from_attributes=True)


class AnalysisCard(BaseModel):
    news_id: str
    company_id: str
    importance_score: float = 0.8
    what_happened: str = "Событие..."
    why_matters: str = "Влияние на бизнес..."
    bank_position: str = "Позиция Альфа-Банка..."
    ai_interpretation: str = "Вывод AI-модели..."

    model_config = ConfigDict(from_attributes=True)


class ChatRequest(BaseModel):
    company_id: str
    news_id: str | None = None
    question: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[dict[str, Any]] = []
