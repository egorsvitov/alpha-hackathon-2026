from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Alfa News Analytics API"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"

    # База данных SQLite
    DATABASE_URL: str = "sqlite+aiosqlite:///./alfa_hack.db"

    # Режим заглушек (удобно для фронтенда и демо)
    MOCK_MODE: bool = True

    # extra="ignore": в .env лежат и чужие для этих настроек переменные (LLM_API_KEY,
    # ANALYST_MODE и т. п.). С запретом лишних полей pydantic падал при старте и печатал
    # их значения (в том числе ключ) в текст ошибки.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
