import asyncio
import time
from typing import Any, Dict, Optional

import httpx
from rq import get_current_job

from config.logging_config import get_logger
from models.job import JobStatus
from services.audio_cache_manager import AudioCacheManager
from services.audio_processor import AudioProcessor
from services.storage import get_storage_client
from utils.cloudflare_kv_limiter import CloudflareKVLimiter
from utils.webhook_verification import generate_webhook_signature

logger = get_logger(__name__)


async def send_webhook_notification(
    callback_url: str, payload: Dict[str, Any], webhook_secret: str = ""
) -> None:
    """Send webhook notification"""
    if not callback_url:
        return

    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        signature = generate_webhook_signature(payload, webhook_secret)
        headers["X-Webhook-Signature"] = signature

    max_retries = 3
    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            try:
                response = await client.post(
                    callback_url,
                    json=payload,
                    timeout=30.0,
                    headers=headers,
                )
                response.raise_for_status()
                logger.info(
                    "Webhook notification sent successfully",
                    callback_url=callback_url,
                    track_id=payload.get("track_id"),
                )
                return
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                if attempt == max_retries - 1:
                    logger.error(
                        "Failed to send webhook after all retries",
                        callback_url=callback_url,
                        track_id=payload.get("track_id"),
                        error=str(e),
                    )
                else:
                    logger.warning(
                        "Webhook attempt failed, retrying",
                        attempt=attempt + 1,
                        callback_url=callback_url,
                        error=str(e),
                    )
                    time.sleep(2**attempt)


def process_audio_job(
    track_id: str,
    search_query: str,
    max_file_size_mb: int,
    processing_timeout: Optional[int],
    webhook_url: str,
    webhook_secret: str,
    cache_key: str,
    storage_config: Dict[str, Any],
    cache_manager_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    RQ job function for processing audio separation.
    This runs synchronously in the worker process.
    """

    job = get_current_job()
    if job:
        logger.info("Starting RQ job", job_id=job.id, track_id=track_id)

    try:
        storage = get_storage_client(**storage_config)

        audio_processor = AudioProcessor(storage)
        audio_processor.initialize_model()

        cache_manager = None
        if cache_manager_config:
            kv_limiter = CloudflareKVLimiter(**cache_manager_config["kv_config"])
            cache_manager = AudioCacheManager(kv_limiter, storage)

        logger.info(
            "Starting audio processing job",
            track_id=track_id,
            search_query=search_query[:50] + ("..." if len(search_query) > 50 else ""),
        )

        result = audio_processor.process_audio(
            track_id=track_id,
            search_query=search_query,
            max_file_size_mb=max_file_size_mb,
            processing_timeout=processing_timeout,
        )

        logger.info("Audio processing completed successfully", track_id=track_id)

        success_payload = {
            "status": JobStatus.COMPLETED.value,
            "track_id": track_id,
            "result": result,
            "progress": 100,
            "created_at": time.time(),
        }

        if cache_manager and cache_key:
            try:
                asyncio.run(cache_manager.cache_completed_result(cache_key, search_query, result))
            except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError) as e:
                logger.error(f"Failed to cache result: {e}")

        if webhook_url:
            asyncio.run(send_webhook_notification(webhook_url, success_payload, webhook_secret))

        return success_payload

    except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
        error_msg = str(e)
        logger.error("Audio processing failed", track_id=track_id, error=error_msg, exc_info=True)

        failure_payload = {
            "status": JobStatus.FAILED.value,
            "track_id": track_id,
            "error": error_msg,
            "progress": 0,
            "created_at": time.time(),
        }

        if webhook_url:
            asyncio.run(send_webhook_notification(webhook_url, failure_payload, webhook_secret))

        raise RuntimeError(error_msg) from e
