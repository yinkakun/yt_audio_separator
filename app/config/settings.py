from pydantic import BaseModel, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class InputLimitsSettings(BaseModel):
    max_search_query_length: int
    max_file_size_mb: int


class ProcessingSettings(BaseModel):
    timeout: int
    max_workers: int
    max_active_jobs: int
    cleanup_interval: int


class RateLimitsSettings(BaseModel):
    requests: str
    separation: str


class ServerSettings(BaseModel):
    host: str
    port: int
    debug: bool


class WebhookSettings(BaseModel):
    url: str
    secret: str
    timeout: int
    max_retries: int
    retry_delay: int


class R2StorageSettings(BaseModel):
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_domain: str


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        validate_default=True,
        extra="ignore",
    )

    max_search_query_length: int = 200
    max_file_size_mb: int = 50

    max_workers: int = 2
    max_active_jobs: int = 2
    cleanup_interval: int = 3600
    processing_timeout: int = 60

    rate_limit_requests: str = "100 per hour"
    rate_limit_separation: str = "5 per minute"

    port: int = 5500
    debug: bool = False
    host: str = "0.0.0.0"

    secret_key: str = ""

    webhook_url: str = ""
    webhook_secret: str = ""
    webhook_timeout: int = 30
    webhook_max_retries: int = 3
    webhook_retry_delay: int = 5

    cloudflare_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "audio-separation"
    r2_public_domain: str = ""

    @computed_field
    @property
    def input_limits(self) -> InputLimitsSettings:
        return InputLimitsSettings(
            max_search_query_length=self.max_search_query_length,
            max_file_size_mb=self.max_file_size_mb,
        )

    @computed_field
    @property
    def processing(self) -> ProcessingSettings:
        return ProcessingSettings(
            timeout=self.processing_timeout,
            max_workers=self.max_workers,
            max_active_jobs=self.max_active_jobs,
            cleanup_interval=self.cleanup_interval,
        )

    @computed_field
    @property
    def rate_limits(self) -> RateLimitsSettings:
        return RateLimitsSettings(
            requests=self.rate_limit_requests, separation=self.rate_limit_separation
        )

    @computed_field
    @property
    def server(self) -> ServerSettings:
        return ServerSettings(host=self.host, port=self.port, debug=self.debug)

    @computed_field
    @property
    def webhooks(self) -> WebhookSettings:
        return WebhookSettings(
            url=self.webhook_url,
            secret=self.webhook_secret,
            timeout=self.webhook_timeout,
            max_retries=self.webhook_max_retries,
            retry_delay=self.webhook_retry_delay,
        )

    @computed_field
    @property
    def r2_storage(self) -> R2StorageSettings:
        return R2StorageSettings(
            account_id=self.cloudflare_account_id,
            access_key_id=self.r2_access_key_id,
            secret_access_key=self.r2_secret_access_key,
            bucket_name=self.r2_bucket_name,
            public_domain=self.r2_public_domain,
        )

    @computed_field
    @property
    def r2_storage_enabled(self) -> bool:
        return bool(
            self.cloudflare_account_id
            and self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_public_domain
        )

    def validate_for_production(self) -> None:
        """Validate configuration"""
        if not self.secret_key:
            raise ValueError("SECRET_KEY must be configured")

        if not self.r2_storage_enabled:
            raise ValueError("R2 storage must be configured")

        if self.webhook_url and self.webhook_secret and len(self.webhook_secret) < 32:
            raise ValueError("WEBHOOK_SECRET must be at least 32 characters for security")
