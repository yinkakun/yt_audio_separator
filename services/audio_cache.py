import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from config.constants import (
    CACHE_TTL_SECONDS,
    PROCESSING_TTL_SECONDS,
    CACHE_CLEANUP_TTL_SECONDS,
    URL_PATH_SEGMENTS_TO_EXTRACT,
)
from config.logger import get_logger

logger = get_logger(__name__)


class CacheProtocol(Protocol):
    async def get_key(self, key: str) -> Optional[dict]: ...
    async def put_key(self, key: str, value: Dict[str, Any], ttl: Optional[int] = None) -> bool: ...


@dataclass
class AudioResult:
    track_id: str
    vocals_url: str
    created_at: float
    instrumental_url: str
    processing_time: float

    def get_storage_paths(self) -> tuple[str, str]:
        """Extract storage paths from URLs"""
        vocals_path = "/".join(self.vocals_url.split("/")[-URL_PATH_SEGMENTS_TO_EXTRACT:])
        instrumental_path = "/".join(
            self.instrumental_url.split("/")[-URL_PATH_SEGMENTS_TO_EXTRACT:]
        )
        return vocals_path, instrumental_path


@dataclass
class CacheEntry:
    status: str
    search_query: str
    created_at: float
    last_accessed: float
    processing_time: float
    result: AudioResult


class AudioCache:
    def __init__(self, cache_client: CacheProtocol, storage_client):
        self.cache = cache_client
        self.storage = storage_client
        self.cache_ttl = CACHE_TTL_SECONDS
        self.processing_ttl = PROCESSING_TTL_SECONDS

    def _normalize_search_query(self, search_query: str) -> str:
        normalized = search_query.lower().strip()

        normalized = re.sub(r"\s+", " ", normalized)

        normalized = re.sub(r"\b(official|video|lyric|lyrics|karaoke)\b", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()

        return normalized

    def _generate_cache_key(self, search_query: str) -> str:
        normalized = self._normalize_search_query(search_query)
        hash_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return hash_digest[:32]

    def _parse_cache_response(self, raw_data: Any) -> Optional[Dict[str, Any]]:
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

            logger.warning(f"Unexpected response format: {type(raw_data)} - {raw_data}")
            return None

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON: {e}")
            return None

    async def _get_cache_entry(self, cache_key: str) -> Optional[CacheEntry]:
        try:
            raw_data = await self.cache.get_key(f"audio:{cache_key}")
            if not raw_data:
                return None

            data = self._parse_cache_response(raw_data)
            if not data:
                logger.error(f"Failed to parse cache data for {cache_key}")
                return None

            if "status" not in data:
                logger.error(f"Cache entry missing 'status' key for {cache_key}: {data}")
                return None

            if data["status"] == "deleted":
                logger.debug(f"Cache entry marked as deleted for {cache_key}")
                return None

            if data["status"] == "processing":
                result = AudioResult(
                    vocals_url="",
                    instrumental_url="",
                    processing_time=0.0,
                    track_id=data.get("track_id", ""),
                    created_at=data.get("created_at", time.time()),
                )

                return CacheEntry(
                    result=result,
                    processing_time=0.0,
                    status=data["status"],
                    search_query=data.get("search_query", ""),
                    created_at=data.get("created_at", time.time()),
                    last_accessed=data.get("last_accessed", data.get("created_at", time.time())),
                )

            if data["status"] == "completed" and "result" in data:
                result = AudioResult(
                    vocals_url=data["result"]["vocals_url"],
                    created_at=data["result"]["created_at"],
                    processing_time=data["result"]["processing_time"],
                    instrumental_url=data["result"]["instrumental_url"],
                    track_id=data.get("track_id", data["result"].get("track_id", "")),
                )

                return CacheEntry(
                    result=result,
                    status=data["status"],
                    search_query=data.get("search_query", ""),
                    created_at=data.get("created_at", time.time()),
                    last_accessed=data.get("last_accessed", data.get("created_at", time.time())),
                    processing_time=data.get("processing_time", data["result"]["processing_time"]),
                )

            logger.warning(f"Invalid cache entry structure for {cache_key}: {data}")
            return None

        except RuntimeError as e:
            logger.warning(f"Failed to get cache entry for {cache_key}: {e}")
            return None
        except KeyError as e:
            logger.error(f"KeyError in cache entry {cache_key}: missing key {e}")
            return None

    async def _update_access_time(self, cache_key: str) -> None:
        try:
            raw_data = await self.cache.get_key(f"audio:{cache_key}")
            if not raw_data:
                return

            data = self._parse_cache_response(raw_data)
            if not data:
                logger.error(f"Failed to parse cache data for access time update: {cache_key}")
                return

            data["last_accessed"] = time.time()

            await self.cache.put_key(f"audio:{cache_key}", data, ttl=self.cache_ttl)

        except RuntimeError as e:
            logger.warning(f"Failed to update access time for {cache_key}: {e}")

    async def _files_exist(self, result: AudioResult) -> bool:
        try:
            vocals_path, instrumental_path = result.get_storage_paths()

            vocals_exists = await self.storage.file_exists_async(vocals_path)
            instrumentals_exists = await self.storage.file_exists_async(instrumental_path)

            return vocals_exists and instrumentals_exists
        except RuntimeError as e:
            logger.warning(f"Error checking file existence: {e}")
            return False

    async def _cache_result(self, cache_key: str, search_query: str, result: AudioResult) -> None:
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

            await self.cache.put_key(f"audio:{cache_key}", cache_data, ttl=self.cache_ttl)
            logger.info(f"Cached audio result for query: {search_query[:50]}")
        except RuntimeError as e:
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

            await self.cache.put_key(f"audio:{cache_key}", processing_data, ttl=self.processing_ttl)
            logger.debug(f"Marked processing for cache key: {cache_key}")

        except RuntimeError as e:
            logger.error(f"Failed to mark processing for {cache_key}: {e}")

    async def get_cached_or_processing(self, search_query: str) -> Optional[Dict[str, Any]]:
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
        try:
            await self.cache.put_key(
                f"audio:{cache_key}", {"status": "deleted"}, ttl=CACHE_CLEANUP_TTL_SECONDS
            )
        except RuntimeError as e:
            logger.warning(f"Failed to remove cache entry {cache_key}: {e}")

    async def mark_processing_start(self, search_query: str, track_id: str) -> str:
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
