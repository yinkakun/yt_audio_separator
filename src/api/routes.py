import asyncio
import logging
import time

import uuid as uuid_module
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from src.models.job import JobStatus


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


logger = logging.getLogger(__name__)


class SeparationRequest(BaseModel):
    search_query: str = Field(..., min_length=1, max_length=500)

    @field_validator("search_query")
    @classmethod
    def validate_search_query(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("search_query must be a string")
        return v.strip()


class JobStatusResponse(BaseModel):
    track_id: str
    status: str
    progress: int
    created_at: float
    completed_at: Optional[float] = None
    processing_time: Optional[float] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    status: str
    timestamp: float
    metrics: Dict[str, int]
    services: Dict[str, bool]


def create_fastapi_app(config, task_manager, audio_processor, r2_client, webhook_manager):
    app = FastAPI(
        title="YouTube Audio Separator API",
        description="API for separating audio tracks into vocals and instrumentals",
        version="1.0.0",
    )

    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter

    async def rate_limit_handler(request: Request, exc: Exception) -> Response:
        if isinstance(exc, RateLimitExceeded):
            return _rate_limit_exceeded_handler(request, exc)
        raise exc

    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

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
    async def validation_exception_handler(_: Request, exc: ValueError):
        logger.warning("Validation error: %s", str(exc))
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(exc)})

    @app.exception_handler(Exception)
    async def general_exception_handler(_: Request, exc: Exception):
        logger.error("Unexpected error: %s", str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal server error"},
        )

    @app.get("/")
    async def read_root():
        return {
            "status": "healthy",
        }

    @app.post("/separate-audio", status_code=status.HTTP_202_ACCEPTED)
    @limiter.limit(config.rate_limits.requests)
    async def separate_audio(request: SeparationRequest):
        if task_manager.count_active_jobs() >= config.processing.max_active_jobs:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Server busy. Please try again later.",
            )

        try:
            search_query = sanitize_input(
                request.search_query, config.input_limits.max_search_query_length
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input validation failed: {str(e)}"
            ) from e

        track_id = str(uuid_module.uuid4())
        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=0)

        asyncio.create_task(
            process_audio_background_async(
                track_id,
                search_query,
                config,
                task_manager,
                audio_processor,
                r2_client,
                webhook_manager,
            )
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

    @app.get("/status/{track_id}", response_model=JobStatusResponse)
    async def get_job_status(track_id: str):
        if not validate_track_id(track_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid track ID format"
            )

        job = task_manager.get_job(track_id)
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

        response_data = {
            "track_id": track_id,
            "status": job.status.value,
            "progress": job.progress,
            "created_at": job.created_at,
        }

        if job.completed_at:
            response_data["completed_at"] = job.completed_at
            response_data["processing_time"] = job.completed_at - job.created_at

        if job.error:
            response_data["error"] = job.error
        if job.result:
            response_data["result"] = job.result

        return JobStatusResponse(**response_data)

    @app.get("/health", response_model=HealthResponse)
    async def health_check():
        health_status = {
            "status": "healthy",
            "timestamp": time.time(),
            "metrics": {
                "active_jobs": task_manager.count_active_jobs(),
                "max_active_jobs": config.processing.max_active_jobs,
            },
            "services": {
                "r2_operational": False,
                "r2_configured": r2_client.client is not None,
            },
        }

        if r2_client.client:
            try:
                await r2_client.file_exists_async("_health_check")
                health_status["services"]["r2_operational"] = True
            except (ConnectionError, TimeoutError, OSError) as e:
                health_status["services"]["r2_operational"] = False
                logger.error("R2 health check failed: %s", e)

        return HealthResponse(**health_status)

    return app


async def process_audio_background_async(
    track_id: str,
    search_query: str,
    config,
    task_manager,
    audio_processor,
    r2_client,
    webhook_manager,
):
    try:
        loop = asyncio.get_event_loop()

        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=10)

        downloaded_file = await loop.run_in_executor(
            None,
            audio_processor.download_audio_simple,
            search_query,
            config.input_limits.max_file_size_mb,
        )

        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=40)

        vocals_file, instrumental_file = await loop.run_in_executor(
            None, audio_processor.separate_audio, downloaded_file
        )

        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=70)

        file_uploads = [
            (vocals_file, f"{track_id}/vocals.mp3"),
            (instrumental_file, f"{track_id}/instrumental.mp3"),
        ]
        upload_results = await r2_client.upload_files_parallel(file_uploads)

        if not all(upload_results):
            raise RuntimeError("Failed to upload audio files to R2")

        vocals_url = r2_client.get_download_url(f"{track_id}/vocals.mp3", r2_client.public_domain)
        instrumental_url = r2_client.get_download_url(
            f"{track_id}/instrumental.mp3", r2_client.public_domain
        )

        result = {
            "vocals_url": vocals_url,
            "instrumental_url": instrumental_url,
            "track_id": track_id,
        }

        task_manager.update_job(track_id, JobStatus.COMPLETED, progress=100, result=result)
        await webhook_manager.notify_job_completed_async(track_id, result)
        logger.info("Job %s completed successfully", track_id)

    except (OSError, RuntimeError, ValueError, ConnectionError, TimeoutError) as e:
        error_msg = str(e)
        logger.error("Job %s failed: %s", track_id, error_msg)
        task_manager.update_job(track_id, JobStatus.FAILED, error=error_msg)
        await webhook_manager.notify_job_failed_async(track_id, error_msg)

    except (ImportError, AttributeError, TypeError, MemoryError) as e:
        error_msg = f"Unexpected error: {str(e)}"
        logger.error("Job %s failed with unexpected error: %s", track_id, error_msg)
        task_manager.update_job(track_id, JobStatus.FAILED, error=error_msg)
        await webhook_manager.notify_job_failed_async(track_id, error_msg)
