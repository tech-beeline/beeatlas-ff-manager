# Copyright (c) 2024 PJSC VimpelCom
"""Конфигурация сервиса и подключения к БД."""
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "fdm_db"
    scripts_dir: str = "scripts"
    # Базовый URL API для скриптов проверок (POST /api/v1/product/{alias}/ff)
    api_base_url: str = "http://127.0.0.1:8000"
    # Таймаут HTTP POST при вызове внешней проверки (fitness_function.method)
    external_ff_timeout_seconds: float = 30.0
    # Базовый URL FDM Products API (GET /api/v1/product/{app}) — получение structurizrApiKey/Secret
    fdm_product_api_base_url: str = ""
    fdm_product_api_timeout_seconds: float = 30.0
    # Базовый URL API Structurizr для исходящих запросов из скриптов (HMAC)
    structurizr_http_base_url: str = ""
    structurizr_http_timeout_seconds: float = 30.0
    # Базовый URL Documents API (GET /api/v1/documents/{document_id},
    # POST /api/v2/documents/{path_name}/{doc_type})
    documents_api_base_url: str = ""
    # Таймаут загрузки документа в Documents API (тело workspace.json — несколько МБ)
    documents_api_timeout_seconds: float = 60.0
    rabbit_host: str = Field(default="localhost", validation_alias="SPRING_RABBITMQ_HOST")
    rabbit_port: int = Field(default=5672, validation_alias="SPRING_RABBITMQ_PORT")
    rabbit_user: str = Field(default="guest", validation_alias="SPRING_RABBITMQ_USERNAME")
    rabbit_password: str = Field(default="guest", validation_alias="SPRING_RABBITMQ_PASSWORD")
    rabbit_virtual_host: str = Field(default="/", validation_alias="SPRING_RABBITMQ_VIRTUAL_HOST")
    ambassador_auth: bool = Field(default=True, validation_alias="APP_AMBASSADOR_AUTH")
    authsso_server_url: str = Field(default="", validation_alias="INTEGRATION_AUTHSSO_SERVER_URL")
    bpm_url: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    class Config:
        env_prefix = "FF_"
        env_file = ".env"


settings = Settings()
