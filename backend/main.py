from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.full_cycle.storage import PipelineError
from app.full_cycle.api import MESSAGES
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.endpoints import api_router
from app.core.config import settings
from app.core.database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализация таблиц при старте (если еще не созданы)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

# Разрешаем CORS для локальной разработки фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение роутов
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "docs": "/docs",
    }


@app.exception_handler(PipelineError)
async def pipeline_error(request: Request, exc: PipelineError):
    status = 404 if exc.code in ("company_profile_missing", "card_not_found") else 409
    return JSONResponse(status_code=status, content={"detail":{"code":exc.code,
        "message":MESSAGES.get(exc.code,"Данные полного цикла недоступны: " + exc.code)}})
