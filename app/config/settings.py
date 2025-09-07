import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def safe_int_from_env(env_var: str, default: int) -> int:
    try:
        value = int(os.getenv(env_var, str(default)))
        return value
    except ValueError as e:
        if "invalid literal for int()" in str(e):
            raise ValueError(f"Invalid integer value for {env_var}: {os.getenv(env_var)}") from e
        raise


@dataclass
class InputLimits:
    max_search_query_length: int = field(
        default_factory=lambda: safe_int_from_env(
            "MAX_SEARCH_QUERY_LENGTH",
            200,
        )
    )
    max_file_size_mb: int = field(
        default_factory=lambda: safe_int_from_env(
            "MAX_FILE_SIZE_MB",
            50,
        )
    )


@dataclass
class ProcessingSettings:
    timeout: int = field(
        default_factory=lambda: safe_int_from_env(
            "PROCESSING_TIMEOUT",
            60,
        )
    )
    max_workers: int = field(
        default_factory=lambda: safe_int_from_env(
            "MAX_WORKERS",
            2,
        )
    )
    max_active_jobs: int = field(
        default_factory=lambda: safe_int_from_env(
            "MAX_ACTIVE_JOBS",
            2,
        )
    )
    cleanup_interval: int = field(
        default_factory=lambda: safe_int_from_env(
            "CLEANUP_INTERVAL",
            3600,
        )
    )


@dataclass
class RateLimits:
    requests: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_REQUESTS", "100 per hour"))
    separation: str = field(
        default_factory=lambda: os.getenv("RATE_LIMIT_SEPARATION", "5 per minute")
    )


@dataclass
class ServerSettings:
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: safe_int_from_env("PORT", 5500))
    debug: bool = field(
        default_factory=lambda: os.getenv("DEBUG", "false").lower() in ("true", "1", "yes", "on")
    )


@dataclass
class WebhookSettings:
    url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))
    secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", ""))
    timeout: int = field(
        default_factory=lambda: safe_int_from_env(
            "WEBHOOK_TIMEOUT",
            30,
        )
    )
    max_retries: int = field(
        default_factory=lambda: safe_int_from_env(
            "WEBHOOK_MAX_RETRIES",
            3,
        )
    )
    retry_delay: int = field(
        default_factory=lambda: safe_int_from_env(
            "WEBHOOK_RETRY_DELAY",
            5,
        )
    )


@dataclass
class R2StorageSettings:
    account_id: str = field(default_factory=lambda: os.getenv("CLOUDFLARE_ACCOUNT_ID", ""))
    access_key_id: str = field(default_factory=lambda: os.getenv("R2_ACCESS_KEY_ID", ""))
    secret_access_key: str = field(default_factory=lambda: os.getenv("R2_SECRET_ACCESS_KEY", ""))
    bucket_name: str = field(
        default_factory=lambda: os.getenv("R2_BUCKET_NAME", "audio-separation")
    )
    public_domain: str = field(default_factory=lambda: os.getenv("R2_PUBLIC_DOMAIN", ""))


@dataclass
class Config:
    input_limits: InputLimits = field(default_factory=InputLimits)
    processing: ProcessingSettings = field(default_factory=ProcessingSettings)
    rate_limits: RateLimits = field(default_factory=RateLimits)
    server: ServerSettings = field(default_factory=ServerSettings)
    webhooks: WebhookSettings = field(default_factory=WebhookSettings)
    r2_storage: R2StorageSettings = field(default_factory=R2StorageSettings)

    @property
    def R2_STORAGE(self) -> bool:
        return bool(
            self.r2_storage.account_id
            and self.r2_storage.access_key_id
            and self.r2_storage.secret_access_key
            and self.r2_storage.public_domain
        )

    def validate(self):
        """Validate configuration values"""
        if not self.R2_STORAGE:
            raise ValueError("R2 storage must be configured - filesystem storage disabled")

        if self.webhooks.url and self.webhooks.secret and len(self.webhooks.secret) < 32:
            raise ValueError("WEBHOOK_SECRET must be at least 32 characters for security")
