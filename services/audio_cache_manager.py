import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from config.logging_config import get_logger
from utils.cloudflare_kv_limiter import CloudflareKVLimiter

logger = get_logger(__name__)


@dataclass
class AudioResult:
    track_id: str
    vocals_url: str
    instrumental_url: str
    processing_time: float
    created_at: float


@dataclass
class CacheEntry:
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
        self.processing_ttl = 60 * 60  # 60 minutes

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

    def _parse_kv_response(self, raw_data: Any) -> Optional[Dict[str, Any]]:
        """Parse Cloudflare KV response format"""
        if not raw_data:
            return None

        try:
            if isinstance(raw_data, dict) and "value" in raw_data:
                if isinstance(raw_data["value"], str):
                    return json.loads(raw_data["value"])
                return raw_data["value"]

            if isinstance(raw_data, dict):
                return raw_data

            if isinstance(raw_data, str):
                return json.loads(raw_data)

            logger.warning(f"Unexpected KV response format: {type(raw_data)} - {raw_data}")
            return None

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from KV response: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error parsing KV response: {e}")
            return None

    async def _get_cache_entry(self, cache_key: str) -> Optional[CacheEntry]:
        """Get cache entry from KV store"""
        try:
            raw_data = await self.kv.get_key(f"audio:{cache_key}")
            if not raw_data:
                return None

            logger.debug(f"Raw cache data retrieved for key {cache_key}: {raw_data}")

            data = self._parse_kv_response(raw_data)
            if not data:
                logger.error(f"Failed to parse cache data for {cache_key}")
                return None

            logger.debug(f"Parsed cache data for key {cache_key}: {data}")

            if "status" not in data:
                logger.error(f"Cache entry missing 'status' key for {cache_key}: {data}")
                return None

            if data["status"] == "deleted":
                logger.debug(f"Cache entry marked as deleted for {cache_key}")
                return None

            if data["status"] == "processing":
                result = AudioResult(
                    track_id=data.get("track_id", ""),
                    vocals_url="",
                    instrumental_url="",
                    processing_time=0.0,
                    created_at=data.get("created_at", time.time()),
                )

                return CacheEntry(
                    status=data["status"],
                    search_query=data.get("search_query", ""),
                    created_at=data.get("created_at", time.time()),
                    last_accessed=data.get("last_accessed", data.get("created_at", time.time())),
                    processing_time=0.0,
                    result=result,
                )

            if data["status"] == "completed" and "result" in data:
                result = AudioResult(
                    track_id=data.get("track_id", data["result"].get("track_id", "")),
                    vocals_url=data["result"]["vocals_url"],
                    instrumental_url=data["result"]["instrumental_url"],
                    processing_time=data["result"]["processing_time"],
                    created_at=data["result"]["created_at"],
                )

                return CacheEntry(
                    status=data["status"],
                    search_query=data.get("search_query", ""),
                    created_at=data.get("created_at", time.time()),
                    last_accessed=data.get("last_accessed", data.get("created_at", time.time())),
                    processing_time=data.get("processing_time", data["result"]["processing_time"]),
                    result=result,
                )

            # Invalid or unknown cache entry structure
            logger.warning(f"Invalid cache entry structure for {cache_key}: {data}")
            return None

        except RuntimeError as e:
            logger.warning(f"Failed to get cache entry for {cache_key}: {e}")
            return None
        except KeyError as e:
            logger.error(f"KeyError in cache entry {cache_key}: missing key {e}")
            return None

    async def _update_access_time(self, cache_key: str) -> None:
        """Update last_accessed timestamp for cache entry"""
        try:
            raw_data = await self.kv.get_key(f"audio:{cache_key}")
            if not raw_data:
                return

            data = self._parse_kv_response(raw_data)
            if not data:
                logger.error(f"Failed to parse cache data for access time update: {cache_key}")
                return

            data["last_accessed"] = time.time()

            await self.kv.put_key(f"audio:{cache_key}", data, ttl=self.cache_ttl)
            logger.debug(f"Updated access time for cache key: {cache_key}")

        except RuntimeError as e:
            logger.warning(f"Failed to update access time for {cache_key}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error updating access time for {cache_key}: {e}")

    async def _files_exist(self, result: AudioResult) -> bool:
        """Check if audio files still exist in R2 storage"""
        try:
            vocals_path = "/".join(result.vocals_url.split("/")[-2:])
            instrumental_path = "/".join(result.instrumental_url.split("/")[-2:])

            logger.debug(
                f"Checking file existence - Vocals: {vocals_path}, Instrumental: {instrumental_path}"
            )

            vocals_exists = await self.storage.file_exists_async(vocals_path)
            instrumentals_exists = await self.storage.file_exists_async(instrumental_path)

            logger.debug(
                f"File existence check - Vocals: {vocals_exists}, Instrumental: {instrumentals_exists}"
            )

            return vocals_exists and instrumentals_exists
        except RuntimeError as e:
            logger.warning(f"Error checking file existence: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error checking file existence: {e}")
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
        except RuntimeError as e:
            logger.error(f"Failed to cache result for {cache_key}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error caching result for {cache_key}: {e}")

    async def _mark_processing(self, cache_key: str, search_query: str, track_id: str) -> None:
        """Mark cache entry as processing"""
        try:
            processing_data = {
                "status": "processing",
                "search_query": search_query,
                "track_id": track_id,
                "created_at": time.time(),
                "last_accessed": time.time(),
            }

            await self.kv.put_key(f"audio:{cache_key}", processing_data, ttl=self.processing_ttl)
            logger.debug(f"Marked processing for cache key: {cache_key}")

        except RuntimeError as e:
            logger.error(f"Failed to mark processing for {cache_key}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error marking processing for {cache_key}: {e}")

    async def get_cached_or_processing(self, search_query: str) -> Optional[Dict[str, Any]]:
        """
        Check if audio is already cached or currently processing
        Returns cached result or processing status, None if needs processing
        """
        cache_key = self._generate_cache_key(search_query)
        logger.info(f"Cache lookup for query: {search_query[:50]} -> key: {cache_key}")

        cached = await self._get_cache_entry(cache_key)
        if not cached:
            logger.debug(f"No cache entry found for: {search_query[:50]}")
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
                    "track_id": cached.result.track_id if cached.result.track_id else "unknown",
                    "message": "Audio separation already in progress",
                }
            logger.info(f"Processing entry expired for: {search_query[:50]}")
            await self._remove_cache_entry(cache_key)
            return None

        logger.warning(f"Unknown cache status '{cached.status}' for: {search_query[:50]}")
        return None

    async def _remove_cache_entry(self, cache_key: str) -> None:
        """Remove cache entry from KV store"""
        try:
            await self.kv.put_key(f"audio:{cache_key}", {"status": "deleted"}, ttl=60)
        except RuntimeError as e:
            logger.warning(f"Failed to remove cache entry {cache_key}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error removing cache entry {cache_key}: {e}")

    async def mark_processing_start(self, search_query: str, track_id: str) -> str:
        """Mark that processing has started, return cache key"""
        cache_key = self._generate_cache_key(search_query)
        await self._mark_processing(cache_key, search_query, track_id)
        return cache_key

    async def cache_completed_result(
        self, cache_key: str, search_query: str, result: Dict[str, Any]
    ) -> None:
        """Cache the completed processing result"""
        try:
            audio_result = AudioResult(
                track_id=result["track_id"],
                vocals_url=result["vocals_url"],
                instrumental_url=result["instrumental_url"],
                processing_time=result.get("processing_time", 0.0),
                created_at=result.get("created_at", time.time()),
            )

            await self._cache_result(cache_key, search_query, audio_result)
            logger.info(f"Cached completed result for: {search_query[:50]}")

        except KeyError as e:
            logger.error(f"Missing required key in result data: {e}")
        except Exception as e:
            logger.error(f"Unexpected error caching completed result: {e}")
