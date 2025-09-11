import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from config.logging_config import get_logger
from utils.cloudflare_kv_limiter import CloudflareKVLimiter

logger = get_logger(__name__)


@dataclass
class AudioResult:
    """Result of audio processing"""

    track_id: str
    vocals_url: str
    instrumental_url: str
    processing_time: float
    created_at: float


@dataclass
class CacheEntry:
    """Cached audio entry metadata"""

    status: str
    search_query: str
    created_at: float
    last_accessed: float
    processing_time: float
    result: AudioResult


class AudioCacheManager:
    """Manages caching and deduplication for audio processing"""

    def __init__(self, kv_client: CloudflareKVLimiter, storage_client):
        self.kv = kv_client
        self.storage = storage_client
        self.cache_ttl = 30 * 24 * 3600  # 30 days
        self.processing_ttl = 3600  # 1 hour for in-progress items

    def _normalize_search_query(self, search_query: str) -> str:
        """Normalize search query for consistent cache keys"""
        normalized = search_query.lower().strip()

        normalized = re.sub(r"\s+", " ", normalized)

        normalized = re.sub(r"\b(official|video|lyric|lyrics|karaoke)\b", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()

        return normalized

    def _generate_cache_key(self, search_query: str) -> str:
        """Generate consistent cache key from search query"""
        normalized = self._normalize_search_query(search_query)
        hash_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return hash_digest[:32]

    async def _get_cache_entry(self, cache_key: str) -> Optional[CacheEntry]:
        """Get cache entry from KV store"""
        try:
            data = await self.kv.get_key(f"audio:{cache_key}")
            if not data:
                return None

            result = AudioResult(
                track_id=data["result"]["track_id"],
                vocals_url=data["result"]["vocals_url"],
                instrumental_url=data["result"]["instrumental_url"],
                processing_time=data["result"]["processing_time"],
                created_at=data["result"]["created_at"],
            )

            return CacheEntry(
                status=data["status"],
                search_query=data["search_query"],
                created_at=data["created_at"],
                last_accessed=data.get("last_accessed", data["created_at"]),
                processing_time=data["processing_time"],
                result=result,
            )
        except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.warning(f"Failed to get cache entry for {cache_key}: {e}")
            return None

    async def _update_access_time(self, cache_key: str) -> None:
        """Update last_accessed timestamp for cache entry"""
        try:
            data = await self.kv.get_key(f"audio:{cache_key}")
            if data:
                data["last_accessed"] = time.time()
                await self.kv.put_key(f"audio:{cache_key}", data, ttl=self.cache_ttl)
        except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.warning(f"Failed to update access time for {cache_key}: {e}")

    async def _files_exist(self, result: AudioResult) -> bool:
        """Check if audio files still exist in R2 storage"""
        try:
            vocals_filename = result.vocals_url.split("/")[-1]
            instrumentals_filename = result.instrumental_url.split("/")[-1]

            vocals_exists = await self.storage.file_exists_async(vocals_filename)
            instrumentals_exists = await self.storage.file_exists_async(instrumentals_filename)

            return vocals_exists and instrumentals_exists
        except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.warning(f"Error checking file existence: {e}")
            return False

    async def _cache_result(self, cache_key: str, search_query: str, result: AudioResult) -> None:
        """Cache processing result in KV store"""
        try:
            cache_data = {
                "status": "completed",
                "search_query": search_query,
                "created_at": result.created_at,
                "last_accessed": time.time(),
                "processing_time": result.processing_time,
                "result": {
                    "track_id": result.track_id,
                    "vocals_url": result.vocals_url,
                    "instrumental_url": result.instrumental_url,
                    "processing_time": result.processing_time,
                    "created_at": result.created_at,
                },
            }

            await self.kv.put_key(f"audio:{cache_key}", cache_data, ttl=self.cache_ttl)
            logger.info(f"Cached audio result for query: {search_query[:50]}")
        except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.error(f"Failed to cache result for {cache_key}: {e}")

    async def _mark_processing(self, cache_key: str, search_query: str, track_id: str) -> None:
        try:
            processing_data = {
                "status": "processing",
                "search_query": search_query,
                "track_id": track_id,
                "created_at": time.time(),
                "last_accessed": time.time(),
            }

            await self.kv.put_key(f"audio:{cache_key}", processing_data, ttl=self.processing_ttl)
        except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.error(f"Failed to mark processing for {cache_key}: {e}")

    async def get_cached_or_processing(self, search_query: str) -> Optional[Dict[str, Any]]:
        """
        Check if audio is already cached or currently processing
        Returns cached result or processing status, None if needs processing
        """
        cache_key = self._generate_cache_key(search_query)
        logger.info(f"Cache lookup for query: {search_query[:50]} -> key: {cache_key}")

        cached = await self._get_cache_entry(cache_key)
        if not cached:
            return None

        if cached.status == "completed":
            if await self._files_exist(cached.result):
                await self._update_access_time(cache_key)
                logger.info(f"Cache hit for query: {search_query[:50]}")

                return {
                    "status": "completed",
                    "track_id": cached.result.track_id,
                    "result": {
                        "vocals_url": cached.result.vocals_url,
                        "instrumental_url": cached.result.instrumental_url,
                        "track_id": cached.result.track_id,
                    },
                    "processing_time": cached.result.processing_time,
                    "created_at": cached.result.created_at,
                    "cached": True,
                }

            logger.warning(f"Cache entry exists but files missing for: {search_query[:50]}")
            await self._remove_cache_entry(cache_key)
            return None

        if cached.status == "processing":
            if time.time() - cached.created_at < self.processing_ttl:
                logger.info(f"Request already processing for: {search_query[:50]}")
                return {
                    "status": "processing",
                    "track_id": cached.result.track_id if hasattr(cached, "result") else "unknown",
                    "message": "Audio separation already in progress",
                }
            await self._remove_cache_entry(cache_key)
            return None
        return None

    async def _remove_cache_entry(self, cache_key: str) -> None:
        """Remove cache entry from KV store"""
        try:
            await self.kv.put_key(f"audio:{cache_key}", {"status": "deleted"}, ttl=1)
        except RuntimeError as e:
            logger.warning(f"Failed to remove cache entry {cache_key}: {e}")

    async def mark_processing_start(self, search_query: str, track_id: str) -> str:
        """Mark that processing has started, return cache key"""
        cache_key = self._generate_cache_key(search_query)
        await self._mark_processing(cache_key, search_query, track_id)
        return cache_key

    async def cache_completed_result(
        self, cache_key: str, search_query: str, result: Dict[str, Any]
    ) -> None:
        """Cache the completed processing result"""
        audio_result = AudioResult(
            track_id=result["track_id"],
            vocals_url=result["vocals_url"],
            instrumental_url=result["instrumental_url"],
            processing_time=result.get("processing_time", 0.0),
            created_at=result.get("created_at", time.time()),
        )

        await self._cache_result(cache_key, search_query, audio_result)
