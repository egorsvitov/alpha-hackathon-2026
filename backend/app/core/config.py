from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Alfa News Analytics API"
    VERSION: str = "0.2.0"
    API_V1_STR: str = "/api/v1"

    # База данных SQLite
    DATABASE_URL: str = "sqlite+aiosqlite:///./alfa_hack.db"

    # Разрешенные источники CORS (локальная разработка и прод-домен)
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
        "https://alfa.sesen.su",
    ]
    CORS_ORIGIN_REGEX: str = (
        r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^https://([a-zA-Z0-9-]+\.)?sesen\.su$"
    )

    # Режим заглушек (удобно для фронтенда и демо)
    MOCK_MODE: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
