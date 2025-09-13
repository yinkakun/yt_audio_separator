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


class R2Config(BaseModel):
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_domain: str


class RedisConfig(BaseModel):
    url: str
    max_connections: int = 10


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

    webhook_secret: str = ""
    webhook_url: str = ""
    api_secret_key: str = ""

    # Cloudflare R2 for storage
    cloudflare_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "audio-separation"
    r2_public_domain: str = ""

    # Cloudflare KV for rate limiting
    cloudflare_api_token: str = ""
    cloudflare_kv_namespace_id: str = ""

    # Redis for RQ job queue
    redis_url: str = ""

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
    def r2_storage(self) -> R2Config:
        return R2Config(
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

    @computed_field
    @property
    def redis_config(self) -> RedisConfig:
        return RedisConfig(url=self.redis_url)

    @computed_field
    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)

    @computed_field
    @property
    def kv_rate_limiting_enabled(self) -> bool:
        return bool(
            self.cloudflare_account_id
            and self.cloudflare_api_token
            and self.cloudflare_kv_namespace_id
        )

    def validate_for_production(self) -> None:
        if not self.api_secret_key:
            raise ValueError("API_SECRET_KEY must be configured")

        if not self.r2_storage_enabled:
            raise ValueError("R2 storage must be configured")
