from fastapi import APIRouter
from app.schemas.base import AnalysisCard, ChatRequest, ChatResponse, CompanyBase, NewsItemBase

api_router = APIRouter()


@api_router.get("/companies", response_model=list[CompanyBase])
async def get_companies():
    """Список тестовых компаний для селектора клиента."""
    return [
        CompanyBase(
            synthetic_u="U_COMP_001",
            mainokved_name="Производство металлургической продукции",
            revenue_company=25_000_000_000.0,
            city="Москва",
        ),
        CompanyBase(
            synthetic_u="U_COMP_002",
            mainokved_name="Оптовая торговля оборудованием",
            revenue_company=18_000_000_000.0,
            city="Санкт-Петербург",
        ),
    ]


@api_router.get("/companies/{company_id}/news-feed", response_model=list[NewsItemBase])
async def get_news_feed(company_id: str):
    """Персонализированная новостная лента под компанию."""
    return [
        NewsItemBase(
            id="news_01",
            title="Банк России сохранил ключевую ставку на уровне 14%",
            summary="Решение совета директоров Банка России от 11 сентября 2026 года.",
        )
    ]


@api_router.get("/news/{news_id}/analysis", response_model=AnalysisCard)
async def get_analysis_card(news_id: str, company_id: str):
    """Карточка события с персонализированной интерпретацией."""
    return AnalysisCard(
        news_id=news_id,
        company_id=company_id,
        importance_score=0.92,
        what_happened="ЦБ РФ оставил ключевую ставку на уровне 14% годовых.",
        why_matters=f"Для компании {company_id} с кредитной нагрузкой стоимость обслуживания долга остается высокой.",
        bank_position="Аналитика Альфа-Банка: риторика ужесточилась, быстрого снижения ставок в 2026 году ждать не стоит.",
        ai_interpretation="Рекомендуется рассмотреть хеджирование процентных рисков и пересмотр инвестпрограммы.",
    )


@api_router.post("/chat/ask", response_model=ChatResponse)
async def ask_question(body: ChatRequest):
    """Уточняющий диалог ЛПР (критерий №10)."""
    return ChatResponse(
        answer=f"Ответ на вопрос: '{body.question}' с учетом профиля компании {body.company_id}.",
        sources=[{"title": "Обзор ЦБ РФ Альфа-Банк", "type": "alfa_research"}],
    )
