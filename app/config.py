from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    serpapi_api_key: str = ""
    backboard_api_key: str = ""
    backboard_model: str = ""
    backboard_provider: str = ""
    app_env: str = "dev"
    cache_ttl_seconds: int = 3600
    llm_timeout_seconds: int = 30


settings = Settings()
