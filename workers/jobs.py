import asyncio
import time
from typing import Any, Dict

import httpx
from rq import get_current_job

from config.constants import WEBHOOK_RETRY_BASE_DELAY, WEBHOOK_RETRY_MAX_ATTEMPTS
from config.logger import get_logger
from models.job import JobStatus
from models.request import ProcessingJobRequest
from services.audio_processor import AudioProcessor
from services.audio_cache import AudioCache
from services.storage import get_storage_client
from utils.redis_cache import RedisCache
from workers.globals import get_global_separator_provider
from utils.webhook import generate_webhook_signature

logger = get_logger(__name__)


async def send_webhook_notification(
    callback_url: str, payload: Dict[str, Any], webhook_secret: str = ""
) -> None:
    if not callback_url:
        return

    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        signature = generate_webhook_signature(payload, webhook_secret)
        headers["X-Webhook-Signature"] = signature

    max_retries = WEBHOOK_RETRY_MAX_ATTEMPTS
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
                    time.sleep(WEBHOOK_RETRY_BASE_DELAY**attempt)


def process_audio_job(job_request: ProcessingJobRequest) -> Dict[str, Any]:
    job = get_current_job()
    if job:
        logger.info("Starting RQ job", job_id=job.id, track_id=job_request.track_id)

    try:
        storage = get_storage_client(
            account_id=job_request.storage_config.account_id,
            access_key_id=job_request.storage_config.access_key_id,
            secret_access_key=job_request.storage_config.secret_access_key,
            bucket_name=job_request.storage_config.bucket_name,
            public_domain=job_request.storage_config.public_domain,
        )

        # Use pre-initialized global separator provider if available
        global_separator = get_global_separator_provider()
        audio_processor = AudioProcessor(
            storage,
            models_dir=job_request.models_dir,
            working_dir=job_request.working_dir,
            separator_provider=global_separator,
        )

        # Only initialize model if we don't have a pre-initialized provider
        if not global_separator:
            audio_processor.initialize_model()

        cache_manager = None
        if job_request.cache_manager_config:
            redis_cache = RedisCache(job_request.cache_manager_config.redis_config.url)
            cache_manager = AudioCache(redis_cache, storage)

        logger.info(
            "Starting audio processing job",
            track_id=job_request.track_id,
            search_query=job_request.search_query[:50]
            + ("..." if len(job_request.search_query) > 50 else ""),
        )

        result = audio_processor.process_audio(
            track_id=job_request.track_id,
            search_query=job_request.search_query,
            max_file_size_mb=job_request.max_file_size_mb,
        )

        logger.info("Audio processing completed successfully", track_id=job_request.track_id)

        success_payload = {
            "result": result,
            "progress": 100,
            "created_at": time.time(),
            "status": JobStatus.COMPLETED.value,
            "track_id": job_request.track_id,
        }

        if cache_manager and job_request.cache_key:
            try:
                asyncio.run(
                    cache_manager.cache_completed_result(
                        job_request.cache_key, job_request.search_query, result
                    )
                )
            except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError) as e:
                logger.error(f"Failed to cache result: {e}")

        if job_request.webhook_url:
            asyncio.run(
                send_webhook_notification(
                    job_request.webhook_url, success_payload, job_request.webhook_secret
                )
            )

        return success_payload

    except (ConnectionError, TimeoutError, RuntimeError) as e:
        error_msg = str(e)
        logger.error(
            "Audio processing failed", track_id=job_request.track_id, error=error_msg, exc_info=True
        )

        failure_payload = {
            "progress": 0,
            "error": error_msg,
            "created_at": time.time(),
            "status": JobStatus.FAILED.value,
            "track_id": job_request.track_id,
        }

        if job_request.webhook_url:
            asyncio.run(
                send_webhook_notification(
                    job_request.webhook_url, failure_payload, job_request.webhook_secret
                )
            )

        raise RuntimeError(error_msg) from e
