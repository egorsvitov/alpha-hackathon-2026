from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Alfa News Analytics API"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"

    # База данных SQLite
    DATABASE_URL: str = "sqlite+aiosqlite:///./alfa_hack.db"

    # Режим заглушек (удобно для фронтенда и демо)
    MOCK_MODE: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )


settings = Settings()
