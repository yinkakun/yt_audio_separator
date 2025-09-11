import time
import uuid as uuid_module
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from config.logging_config import get_logger
from models.job import JobStatus
from services.audio_cache_manager import AudioCacheManager
from utils.cloudflare_kv_limiter import CloudflareKVLimiter, parse_rate_limit
from utils.webhook_verification import generate_webhook_signature


def sanitize_input(input_str: str, max_length: int) -> str:
    if not isinstance(input_str, str):
        raise ValueError("Input must be a string")

    cleaned = input_str.strip()
    if not cleaned:
        raise ValueError("Input cannot be empty")

    if len(cleaned) > max_length:
        raise ValueError(f"Input too long (max {max_length} characters)")

    return cleaned


def validate_track_id(track_id: str) -> bool:
    """Validate UUID format for track ID"""
    try:
        uuid_module.UUID(track_id)
        return True
    except (ValueError, TypeError):
        return False


logger = get_logger(__name__)


@dataclass
class AudioProcessingTask:
    """Configuration for audio processing background task"""

    track_id: str
    search_query: str
    max_file_size_mb: int
    processing_timeout: Optional[int]
    webhook_url: Optional[str]
    audio_processor: Any
    webhook_secret: str = ""
    cache_manager: Any = None
    cache_key: str = ""


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
                    time.sleep(2**attempt)  # Exponential backoff


async def process_audio_background(task: AudioProcessingTask) -> None:
    """Background task to process audio separation."""
    try:
        logger.info(
            "Starting audio processing task", track_id=task.track_id, search_query=task.search_query
        )

        result = task.audio_processor.process_audio(
            track_id=task.track_id,
            search_query=task.search_query,
            max_file_size_mb=task.max_file_size_mb,
            processing_timeout=task.processing_timeout,
        )

        logger.info("Audio processing completed successfully", track_id=task.track_id)

        success_payload = {
            "status": "completed",
            "track_id": task.track_id,
            "result": result,
            "progress": 100,
            "created_at": time.time(),
        }

        if task.cache_manager and task.cache_key:
            try:
                await task.cache_manager.cache_completed_result(
                    task.cache_key, task.search_query, result
                )
            except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError) as e:
                logger.error(f"Failed to cache result: {e}")

        if task.webhook_url:
            await send_webhook_notification(task.webhook_url, success_payload, task.webhook_secret)

    except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
        error_msg = str(e)
        logger.error(
            "Audio processing failed", track_id=task.track_id, error=error_msg, exc_info=True
        )

        failure_payload = {
            "status": "failed",
            "track_id": task.track_id,
            "error": error_msg,
            "progress": 0,
            "created_at": time.time(),
        }

        if task.webhook_url:
            await send_webhook_notification(task.webhook_url, failure_payload, task.webhook_secret)


class SeparationRequest(BaseModel):
    search_query: str = Field(..., min_length=1, max_length=500)

    @field_validator("search_query")
    @classmethod
    def validate_search_query(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("search_query must be a string")
        return v.strip()


class HealthResponse(BaseModel):
    status: str
    timestamp: float
    metrics: Dict[str, int]
    services: Dict[str, bool]


def create_fastapi_app(config, audio_processor, storage):
    kv_limiter = None
    cache_manager = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Startup
        nonlocal kv_limiter, cache_manager

        audio_processor.initialize_model()

        try:
            await storage.file_exists_async("_startup_test")
            logger.info("R2 storage connectivity verified")
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error(f"Storage startup validation failed: {e}")

        if config.kv_rate_limiting_enabled:
            kv_limiter = CloudflareKVLimiter(
                account_id=config.cloudflare_account_id,
                namespace_id=config.cloudflare_kv_namespace_id,
                api_token=config.cloudflare_api_token,
            )

            try:
                await kv_limiter.get_key("_startup_test")
                logger.info("Cloudflare KV connectivity verified")
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.error(f"KV startup validation failed: {e}")
                logger.warning("Continuing without KV rate limiting")
                await kv_limiter.close()
                kv_limiter = None

            if kv_limiter:
                logger.info("Cloudflare KV rate limiting enabled")
                cache_manager = AudioCacheManager(kv_limiter, storage)
                logger.info("Audio cache manager initialized")
        else:
            logger.warning("Cloudflare KV rate limiting disabled - missing configuration")

        # Shutdown
        yield

        if kv_limiter:
            await kv_limiter.close()
        await storage.close()

    app = FastAPI(
        title="YouTube Audio Separator API",
        description="API for separating audio tracks into vocals and instrumental",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "media-src 'self'; "
            "frame-src 'none';"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        return response

    @app.exception_handler(ValueError)
    async def validation_exception_handler(_, exc: ValueError):
        logger.warning("Validation error: %s", str(exc))
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(exc)})

    @app.exception_handler(Exception)
    async def general_exception_handler(_: Request, exc: Exception):
        try:
            logger.error("Unexpected error: %s", str(exc))
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "Internal server error"},
            )
        except Exception as handler_exc:  # pylint: disable=broad-except
            logger.error("Exception handler failed: %s", str(handler_exc))
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "Internal server error"},
            )

    async def _apply_rate_limit(request: Request, rate_limit: str):
        """Apply rate limiting if KV limiter is configured"""
        if not kv_limiter:
            return

        try:
            limit_requests, window_seconds = parse_rate_limit(rate_limit)
        except ValueError as e:
            logger.warning(str(e))
            return

        client_ip = request.client.host if request.client else "unknown"

        allowed, info = await kv_limiter.is_allowed(client_ip, limit_requests, window_seconds)

        if not allowed:
            logger.warning(f"Rate limit exceeded for {client_ip}: {rate_limit}")
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Rate limit exceeded",
                    "limit": info["limit"],
                    "remaining": info["remaining"],
                    "reset_time": info["reset_time"],
                    "retry_after": info["retry_after"],
                },
                headers={"Retry-After": str(info["retry_after"])},
            )

    async def _verify_api_key(request: Request):
        """Verify API key from request headers"""
        api_key = request.headers.get("X-API-Key") or request.headers.get(
            "Authorization", ""
        ).replace("Bearer ", "")

        if not api_key or api_key != config.api_secret_key:
            logger.warning(
                "Unauthorized API access attempt",
                client_ip=request.client.host if request.client else "unknown",
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.post("/separate-audio", status_code=status.HTTP_202_ACCEPTED)
    async def separate_audio(
        request: Request, separation_request: SeparationRequest, background_tasks: BackgroundTasks
    ):  # pylint: disable=unused-argument
        await _verify_api_key(request)

        await _apply_rate_limit(request, config.rate_limits.requests)
        try:
            search_query = sanitize_input(
                separation_request.search_query, config.input_limits.max_search_query_length
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input validation failed: {str(e)}"
            ) from e

        if cache_manager:
            cached_result = await cache_manager.get_cached_or_processing(search_query)
            if cached_result:
                logger.info(f"Returning cached/processing result for: {search_query[:50]}")
                return cached_result

        track_id = str(uuid_module.uuid4())

        cache_key = ""
        if cache_manager:
            cache_key = await cache_manager.mark_processing_start(search_query, track_id)

        task = AudioProcessingTask(
            track_id=track_id,
            search_query=search_query,
            max_file_size_mb=config.input_limits.max_file_size_mb,
            processing_timeout=(
                config.processing.processing_timeout
                if hasattr(config.processing, "processing_timeout")
                else None
            ),
            webhook_url=config.webhook_url,
            audio_processor=audio_processor,
            webhook_secret=config.webhook_secret,
            cache_manager=cache_manager,
            cache_key=cache_key,
        )

        background_tasks.add_task(process_audio_background, task)

        logger.info(
            "Accepted job %s for search query: %s",
            track_id,
            search_query[:50] + ("..." if len(search_query) > 50 else ""),
        )

        return {
            "track_id": track_id,
            "status": JobStatus.PROCESSING.value,
            "message": "Audio separation started",
        }

    @app.get("/health", response_model=HealthResponse)
    async def health_check():
        health_status = {
            "status": "healthy",
            "timestamp": time.time(),
            "services": {
                "storage_operational": False,
                "storage_configured": storage.client is not None,
                "rate_limiting_operational": False,
                "rate_limiting_configured": kv_limiter is not None,
            },
        }

        if storage.client:
            try:
                await storage.file_exists_async("_health_check")
                health_status["services"]["storage_operational"] = True
            except (ConnectionError, TimeoutError, OSError) as e:
                health_status["services"]["storage_operational"] = False
                logger.error("Storage health check failed: %s", e)

        if kv_limiter:
            try:
                test_key = "_health_check"
                await kv_limiter.get_key(test_key)
                health_status["services"]["rate_limiting_operational"] = True
            except (ConnectionError, TimeoutError, OSError) as e:
                health_status["services"]["rate_limiting_operational"] = False
                logger.error("KV rate limiter health check failed: %s", e)

        return HealthResponse(**health_status)

    return app
