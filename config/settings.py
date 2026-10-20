"""
Application settings management using Pydantic BaseSettings.

This module provides centralized configuration management for the AETCG platform.
Settings are loaded from multiple sources with the following precedence:
1. Environment variables (highest priority)
2. Values from .env file in project root
3. Default values defined in this file (lowest priority)

To configure the application for your environment:
1. Copy .env.example to .env: cp .env.example .env
2. Edit .env to set your desired configuration values
3. The application will automatically load these settings

All settings are validated for type safety and constraints.
"""
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
import os
from typing import Optional


class AppSettings(BaseSettings):
    """Application configuration settings."""

    APP_ENV: str = Field(default="development", description="Application environment (development/production)")
    API_PORT: int = Field(default=8000, description="API service port", ge=1, le=65535)
    REDIS_HOST: str = Field(default="queue_broker", description="Redis host for queue broker")
    REDIS_PORT: int = Field(default=6379, description="Redis port", ge=1, le=65535)
    EXTERNAL_COMPLIANCE_API_URL: str = Field(
        default="https://api.mockcompliance.org/v1/validate",
        description="External compliance API endpoint"
    )
    EXTERNAL_API_TIMEOUT_SEC: float = Field(
        default=5.0,
        description="Timeout for external API calls in seconds",
        gt=0
    )

    LLM_ENDPOINT_URL: str = Field(
        default="",
        description="LLM service endpoint URL"
    )
    LLM_ACCESS_TOKEN: str = Field(
        default="",
        description="Access token for LLM service"
    )

    STATE_TTL_SEC: int = Field(
        default=7 * 24 * 3600,
        description="How long a ticket's final state stays readable in Redis (seconds)",
        gt=0
    )

    # Metrics-related settings
    METRICS_ENABLED: bool = Field(
        default=True,
        description="Enable Prometheus metrics collection and exposure"
    )
    METRICS_PORT: int = Field(
        default=8000,
        description="Port for metrics endpoint (same as API port in this implementation)",
        ge=1,
        le=65535
    )
    METRICS_PATH: str = Field(
        default="/metrics",
        description="Path for metrics endpoint"
    )

    # Security-related settings
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = Field(
        default=60,
        description="Maximum API requests per minute per IP",
        gt=0
    )
    SUPERVISOR_TOKEN: str = Field(
        default="SECURE_GOVERNANCE_TOKEN_XYZ",
        description="Token for supervisor approval endpoint"
    )
    CORS_ALLOWED_ORIGINS: str = Field(
        default="http://localhost:8000",
        description="Comma-separated browser origins allowed to call the API. '*' allows any origin but then credentials are never allowed."
    )

    def cors_origins(self) -> list:
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    def cors_allow_credentials(self) -> bool:
        # The Fetch spec forbids Access-Control-Allow-Origin: * together with
        # credentials; browsers reject it and permissive middleware echoes the
        # caller's origin instead, which is worse.
        return "*" not in self.cors_origins()

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator('APP_ENV')
    @classmethod
    def validate_app_env(cls, v):
        allowed_envs = ['development', 'staging', 'production']
        if v not in allowed_envs:
            raise ValueError(f'APP_ENV must be one of {allowed_envs}')
        return v

    @field_validator('LLM_ACCESS_TOKEN')
    @classmethod
    def validate_llm_token(cls, v):
        # In production, token should not be empty
        # This is a soft warning - in real implementation might check APP_ENV
        if not v and os.getenv('APP_ENV') == 'production':
            # In a real implementation, we might raise an error or log a warning
            pass
        return v

settings = AppSettings()
