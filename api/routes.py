import time
import uuid as uuid_module
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import requests

from config.logging_config import get_logger
from models.job import JobStatus


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


def send_webhook_notification(callback_url: str, payload: Dict[str, Any]) -> None:
    """Send webhook notification"""
    if not callback_url:
        return

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(
                callback_url, json=payload, timeout=30, headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            logger.info(
                "Webhook notification sent successfully",
                callback_url=callback_url,
                track_id=payload.get("track_id"),
            )
            return
        except requests.exceptions.RequestException as e:
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


def process_audio_background(
    track_id: str,
    search_query: str,
    max_file_size_mb: int,
    processing_timeout: Optional[int],
    callback_url: Optional[str],
    audio_processor,
) -> None:
    """Background task to process audio separation."""
    try:
        logger.info("Starting audio processing task", track_id=track_id, search_query=search_query)

        result = audio_processor.process_audio(
            track_id=track_id,
            search_query=search_query,
            max_file_size_mb=max_file_size_mb,
            processing_timeout=processing_timeout,
        )

        logger.info("Audio processing completed successfully", track_id=track_id)

        success_payload = {
            "status": "completed",
            "track_id": track_id,
            "result": result,
            "progress": 100,
            "created_at": time.time(),
        }

        if callback_url:
            send_webhook_notification(callback_url, success_payload)

    except Exception as e:
        error_msg = str(e)
        logger.error("Audio processing failed", track_id=track_id, error=error_msg, exc_info=True)

        failure_payload = {
            "status": "failed",
            "track_id": track_id,
            "error": error_msg,
            "progress": 0,
            "created_at": time.time(),
        }

        if callback_url:
            send_webhook_notification(callback_url, failure_payload)


class SeparationRequest(BaseModel):
    search_query: str = Field(..., min_length=1, max_length=500)
    callback_url: Optional[str] = Field(None, max_length=2000)

    @field_validator("search_query")
    @classmethod
    def validate_search_query(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("search_query must be a string")
        return v.strip()

    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError("callback_url must be a valid HTTP/HTTPS URL")
        return v.strip()


class HealthResponse(BaseModel):
    status: str
    timestamp: float
    metrics: Dict[str, int]
    services: Dict[str, bool]


def create_fastapi_app(config, audio_processor, storage):
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        audio_processor.initialize_model()
        # Startup
        yield
        # Shutdown
        await storage.close()

    app = FastAPI(
        title="YouTube Audio Separator API",
        description="API for separating audio tracks into vocals and instrumentals",
        version="1.0.0",
        lifespan=lifespan,
    )

    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter

    async def rate_limit_handler(request: Request, exc: Exception) -> Response:
        if isinstance(exc, RateLimitExceeded):
            return _rate_limit_exceeded_handler(request, exc)
        raise exc

    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

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

    @app.post("/separate-audio", status_code=status.HTTP_202_ACCEPTED)
    @limiter.limit(config.rate_limits.requests)
    async def separate_audio(
        request: Request, separation_request: SeparationRequest, background_tasks: BackgroundTasks
    ):  # pylint: disable=unused-argument
        try:
            search_query = sanitize_input(
                separation_request.search_query, config.input_limits.max_search_query_length
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input validation failed: {str(e)}"
            ) from e

        track_id = str(uuid_module.uuid4())

        # Queue the background task
        background_tasks.add_task(
            process_audio_background,
            track_id,
            search_query,
            config.input_limits.max_file_size_mb,
            (
                config.processing.processing_timeout
                if hasattr(config.processing, "processing_timeout")
                else None
            ),
            separation_request.callback_url,
            audio_processor,  # Pass audio_processor from closure
        )

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
            "metrics": {
                "active_jobs": 0,  # Background tasks don't have easy counting
                "max_active_jobs": 0,  # No longer relevant with background tasks
            },
            "services": {
                "storage_operational": False,
                "storage_configured": storage.client is not None,
            },
        }

        if storage.client:
            try:
                await storage.file_exists_async("_health_check")
                health_status["services"]["storage_operational"] = True
            except (ConnectionError, TimeoutError, OSError) as e:
                health_status["services"]["storage_operational"] = False
                logger.error("Storage health check failed: %s", e)

        return HealthResponse(**health_status)

    return app
