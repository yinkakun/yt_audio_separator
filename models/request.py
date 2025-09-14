from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StorageConfig:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_domain: str


@dataclass(frozen=True)
class RedisConfig:
    url: str


@dataclass(frozen=True)
class AudioCacheConfig:
    redis_config: RedisConfig


@dataclass(frozen=True)
class ProcessingJobRequest:
    track_id: str
    search_query: str
    max_file_size_mb: int
    processing_timeout: Optional[int]
    webhook_url: str
    webhook_secret: str
    cache_key: str
    storage_config: StorageConfig
    models_dir: str
    working_dir: str
    cache_manager_config: Optional[AudioCacheConfig] = None
