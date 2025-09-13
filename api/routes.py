import time
import uuid as uuid_module
from contextlib import asynccontextmanager
from typing import Dict

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from config.logging_config import get_logger
from models.job import JobStatus
from services.audio_cache_manager import AudioCacheManager
from utils.cloudflare_kv_limiter import CloudflareKVLimiter, parse_rate_limit
from workers.queue_manager import QueueManager


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
    services: Dict[str, bool]


def create_fastapi_app(config, storage):
    kv_limiter = None
    cache_manager = None
    queue_manager = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Startup
        nonlocal kv_limiter, cache_manager, queue_manager

        if config.redis_enabled:
            try:
                queue_manager = QueueManager(config.redis_url)
                queue_info = queue_manager.get_queue_info()
                logger.info("RQ queue manager initialized", queue_info=queue_info)
            except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
                logger.error(f"Failed to initialize RQ queue manager: {e}")
                logger.warning("Continuing without RQ queue manager")
                queue_manager = None
            except Exception as e:
                logger.error(f"Unexpected error initializing RQ queue manager: {e}")
                logger.warning("Continuing without RQ queue manager")
                queue_manager = None
        else:
            logger.warning("Redis not configured - RQ queue manager disabled")

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

        if queue_manager:
            queue_manager.close()
        if kv_limiter:
            await kv_limiter.close()
        await storage.close()

    app = FastAPI(
        title="YouTube Audio Separator API",
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
    async def separate_audio(request: Request, separation_request: SeparationRequest):
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

        if not queue_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job queue unavailable - Redis not configured",
            )

        cache_key = ""
        if cache_manager:
            cache_key = await cache_manager.mark_processing_start(search_query, track_id)

        storage_config = {
            "account_id": config.r2_storage.account_id,
            "access_key_id": config.r2_storage.access_key_id,
            "secret_access_key": config.r2_storage.secret_access_key,
            "bucket_name": config.r2_storage.bucket_name,
            "public_domain": config.r2_storage.public_domain,
        }

        cache_manager_config = None
        if kv_limiter:
            cache_manager_config = {
                "kv_config": {
                    "account_id": config.cloudflare_account_id,
                    "namespace_id": config.cloudflare_kv_namespace_id,
                    "api_token": config.cloudflare_api_token,
                }
            }

        queue_manager.enqueue_audio_job(
            track_id=track_id,
            search_query=search_query,
            max_file_size_mb=config.input_limits.max_file_size_mb,
            processing_timeout=getattr(config.processing, "processing_timeout", None),
            webhook_url=config.webhook_url,
            webhook_secret=config.webhook_secret,
            cache_key=cache_key,
            storage_config=storage_config,
            cache_manager_config=cache_manager_config,
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

    @app.get("/job/{track_id}")
    async def get_job_status(request: Request, track_id: str):
        """Get job status by track ID"""
        await _verify_api_key(request)

        if not validate_track_id(track_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid track ID format"
            )

        if not queue_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job queue unavailable - Redis not configured",
            )

        try:
            job_status = queue_manager.get_job_status(track_id)
            return job_status
        except Exception as e:
            logger.error(f"Failed to get job status for {track_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve job status",
            ) from e

    @app.get("/queue/info")
    async def get_queue_info(request: Request):
        """Get queue information (admin endpoint)"""
        await _verify_api_key(request)

        if not queue_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job queue unavailable - Redis not configured",
            )

        try:
            queue_info = queue_manager.get_queue_info()
            return queue_info
        except Exception as e:
            logger.error(f"Failed to get queue info: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve queue information",
            ) from e

    return app
