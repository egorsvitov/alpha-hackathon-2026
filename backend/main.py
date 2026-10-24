from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.api.v1.endpoints import api_router
from app.core.config import settings
from app.core.database import Base, engine
from app.models import entities as _entities  # noqa: F401
from app.services.store import store


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализация таблиц при старте и загрузка сохраненных событий из SQLite
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await store.init_db()
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# Корректная конфигурация CORS для локальной разработки и продакшена
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_origin_regex=settings.CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение роутов API v1
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/docs", include_in_schema=False)
async def redirect_docs() -> RedirectResponse:
    return RedirectResponse(url="/api/docs")


@app.get("/redoc", include_in_schema=False)
async def redirect_redoc() -> RedirectResponse:
    return RedirectResponse(url="/api/redoc")


@app.get("/openapi.json", include_in_schema=False)
async def legacy_openapi() -> dict[str, Any]:
    return app.openapi()


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "docs": "/api/docs",
    }
