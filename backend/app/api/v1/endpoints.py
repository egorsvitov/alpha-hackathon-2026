from typing import Any
from fastapi import APIRouter, HTTPException, Query, Response, status
from starlette.concurrency import run_in_threadpool

from app.schemas.base import (
    CompanyProfile,
    FeedbackRequest,
    FeedItem,
    Insight,
    NewsEvent,
    NewsUploadRequest,
    QaAnswer,
    QaRequest,
    Role,
)
from app.services.store import store

api_router = APIRouter()


@api_router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@api_router.get("/companies", response_model=list[CompanyProfile])
async def get_companies() -> list[CompanyProfile]:
    return await run_in_threadpool(store.get_companies)


@api_router.get(
    "/companies/{company_id}/news-feed",
    response_model=list[FeedItem],
)
async def get_news_feed(
    company_id: str,
    role: Role = Query(default="cfo"),
) -> list[FeedItem]:
    return await run_in_threadpool(store.get_feed, company_id=company_id, role=role)


@api_router.get(
    "/news/{news_id}/analysis",
    response_model=Insight,
)
async def get_analysis_card(
    news_id: str,
    company_id: str = Query(...),
    role: Role = Query(default="cfo"),
) -> Insight:
    try:
        return await run_in_threadpool(
            store.get_insight,
            event_id=news_id,
            company_id=company_id,
            role=role,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Insight not found",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Insight not found",
        ) from exc


@api_router.post("/chat/ask", response_model=QaAnswer)
async def ask_question(body: QaRequest) -> QaAnswer:
    return await run_in_threadpool(
        store.ask,
        company_id=body.company_id,
        news_id=body.news_id,
        role=body.role,
        run_id=body.run_id,
        question=body.question,
    )


@api_router.post(
    "/news",
    response_model=NewsEvent,
    status_code=status.HTTP_201_CREATED,
)
async def upload_news(body: NewsUploadRequest) -> NewsEvent:
    return await store.ingest_news_async(title=body.title, text=body.text)


@api_router.post(
    "/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def send_feedback(body: FeedbackRequest) -> Response:
    await store.record_feedback_async(run_id=body.run_id, useful=body.useful)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api_router.get("/metrics")
async def get_metrics() -> dict[str, Any]:
    return await run_in_threadpool(store.get_metrics)
