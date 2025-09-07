import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import boto3
import requests
import validators
import yt_dlp
from audio_separator.separator import Separator
from botocore.exceptions import ClientError, NoCredentialsError
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from yt_dlp.utils import DownloadError, ExtractorError

load_dotenv()


class TrackType(Enum):
    VOCALS = "vocals"
    INSTRUMENTAL = "instrumental"


class JobStatus(Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


def safe_int_from_env(env_var: str, default: int) -> int:
    try:
        value = int(os.getenv(env_var, str(default)))
        return value
    except ValueError as e:
        if "invalid literal for int()" in str(e):
            raise ValueError(f"Invalid integer value for {env_var}: {os.getenv(env_var)}") from e
        raise


@dataclass
class InputLimits:
    max_search_query_length: int = field(
        default_factory=lambda: safe_int_from_env(
            "MAX_SEARCH_QUERY_LENGTH",
            200,
        )
    )
    max_file_size_mb: int = field(
        default_factory=lambda: safe_int_from_env(
            "MAX_FILE_SIZE_MB",
            50,
        )
    )


@dataclass
class ProcessingSettings:
    timeout: int = field(
        default_factory=lambda: safe_int_from_env(
            "PROCESSING_TIMEOUT",
            60,
        )
    )
    max_workers: int = field(
        default_factory=lambda: safe_int_from_env(
            "MAX_WORKERS",
            2,
        )
    )
    max_active_jobs: int = field(
        default_factory=lambda: safe_int_from_env(
            "MAX_ACTIVE_JOBS",
            2,
        )
    )
    cleanup_interval: int = field(
        default_factory=lambda: safe_int_from_env(
            "CLEANUP_INTERVAL",
            3600,
        )
    )


@dataclass
class RateLimits:
    requests: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_REQUESTS", "100 per hour"))
    separation: str = field(
        default_factory=lambda: os.getenv("RATE_LIMIT_SEPARATION", "5 per minute")
    )


@dataclass
class ServerSettings:
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: safe_int_from_env("PORT", 5500))
    debug: bool = field(
        default_factory=lambda: os.getenv("DEBUG", "false").lower() in ("true", "1", "yes", "on")
    )


@dataclass
class WebhookSettings:
    url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))
    secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", ""))
    timeout: int = field(
        default_factory=lambda: safe_int_from_env(
            "WEBHOOK_TIMEOUT",
            30,
        )
    )
    max_retries: int = field(
        default_factory=lambda: safe_int_from_env(
            "WEBHOOK_MAX_RETRIES",
            3,
        )
    )
    retry_delay: int = field(
        default_factory=lambda: safe_int_from_env(
            "WEBHOOK_RETRY_DELAY",
            5,
        )
    )


@dataclass
class R2StorageSettings:
    account_id: str = field(default_factory=lambda: os.getenv("CLOUDFLARE_ACCOUNT_ID", ""))
    access_key_id: str = field(default_factory=lambda: os.getenv("R2_ACCESS_KEY_ID", ""))
    secret_access_key: str = field(default_factory=lambda: os.getenv("R2_SECRET_ACCESS_KEY", ""))
    bucket_name: str = field(
        default_factory=lambda: os.getenv("R2_BUCKET_NAME", "audio-separation")
    )


@dataclass
class Config:
    input_limits: InputLimits = field(default_factory=InputLimits)
    processing: ProcessingSettings = field(default_factory=ProcessingSettings)
    rate_limits: RateLimits = field(default_factory=RateLimits)
    server: ServerSettings = field(default_factory=ServerSettings)
    webhooks: WebhookSettings = field(default_factory=WebhookSettings)
    r2_storage: R2StorageSettings = field(default_factory=R2StorageSettings)

    @property
    def R2_STORAGE(self) -> bool:
        return bool(
            self.r2_storage.account_id
            and self.r2_storage.access_key_id
            and self.r2_storage.secret_access_key
        )

    def validate(self):
        """Validate configuration values"""
        if not self.R2_STORAGE:
            raise ValueError("R2 storage must be configured - filesystem storage disabled")

        if self.webhooks.url and self.webhooks.secret and len(self.webhooks.secret) < 32:
            raise ValueError("WEBHOOK_SECRET must be at least 32 characters for security")


@contextmanager
def timeout_context(timeout_seconds: int):
    """Context manager for timeout handling with proper cleanup"""
    timer = None

    def timeout_handler():
        raise TimeoutError(f"Operation timed out after {timeout_seconds} seconds")

    try:
        timer = threading.Timer(timeout_seconds, timeout_handler)
        timer.start()
        yield
    finally:
        if timer:
            timer.cancel()


config = Config()
config.validate()

app = Flask(__name__)

_secret_key = os.getenv("SECRET_KEY")
if not _secret_key:
    raise RuntimeError("SECRET_KEY environment variable is required")
app.config["SECRET_KEY"] = _secret_key
app.config["MAX_CONTENT_LENGTH"] = config.input_limits.max_file_size_mb * 1024 * 1024

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


@app.after_request
def after_request(response):
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

    if hasattr(g, "correlation_id"):
        response.headers["X-Correlation-ID"] = g.correlation_id

    return response


_handlers: list[logging.Handler] = [logging.StreamHandler()]
if not config.server.debug:
    try:
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        rotating_handler = logging.handlers.RotatingFileHandler(
            logs_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5
        )
        _handlers.append(rotating_handler)
    except OSError as e:
        print(f"Warning: Could not create log file, using console only: {e}")

logging.basicConfig(
    level=logging.INFO if not config.server.debug else logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=_handlers,
)
logger = logging.getLogger(__name__)


if not config.server.debug:
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)

limiter = Limiter(
    app=app, key_func=get_remote_address, default_limits=[config.rate_limits.requests]
)


class CloudflareR2:
    def __init__(self, account_id: str, access_key: str, secret_key: str, bucket_name: str):
        self.bucket_name = bucket_name

        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )

    def upload_file(self, file_path: Path, key: str) -> bool:
        if not self.client:
            return False
        try:
            self.client.upload_file(
                str(file_path), self.bucket_name, key, ExtraArgs={"ContentType": "audio/mpeg"}
            )
            logger.info("Uploaded %s to R2 as %s", file_path, key)
            return True
        except (NoCredentialsError, ClientError, OSError, FileNotFoundError) as e:
            logger.error("R2 upload error: %s", str(e))
            return False

    def file_exists(self, key: str) -> bool:
        if not self.client:
            return False
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError:
            return False

    def delete_file(self, key: str) -> bool:
        if not self.client:
            return False
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=key)
            return True
        except (NoCredentialsError, ClientError) as e:
            logger.error("R2 delete error: %s", str(e))
            return False

    def get_download_url(self, key: str, expires_in: int = 3600) -> Optional[str]:
        if not self.client:
            return None
        try:
            url = self.client.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket_name, "Key": key}, ExpiresIn=expires_in
            )
            return url
        except (NoCredentialsError, ClientError) as e:
            logger.error("R2 presigned URL error: %s", str(e))
            return None


class WebhookManager:
    def __init__(self, webhook_url: str, webhook_secret: str):
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        self.enabled = bool(webhook_url)

    def create_signature(self, payload: str) -> str:
        if not self.webhook_secret:
            return ""

        signature = hmac.new(
            self.webhook_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"

    def send_webhook(self, event_type: str, data: Dict[str, Any]) -> None:
        if not self.enabled:
            return

        payload = {"event": event_type, "timestamp": time.time(), "data": data}
        threading.Thread(
            target=self._send_webhook_with_retry, args=(event_type, payload), daemon=True
        ).start()

    def _send_webhook_with_retry(self, event_type: str, payload: Dict[str, Any]) -> bool:
        payload_str = json.dumps(payload, sort_keys=True)
        signature = self.create_signature(payload_str)

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
            "User-Agent": "AudioSeparator/1.0",
        }

        for attempt in range(config.webhooks.max_retries):
            try:
                response = requests.post(
                    self.webhook_url,
                    data=payload_str,
                    headers=headers,
                    timeout=config.webhooks.timeout,
                )

                if 200 <= response.status_code < 300:
                    logger.info("Webhook sent successfully: %s", event_type)
                    return True

                logger.warning(
                    "Webhook failed with status %d: %s", response.status_code, response.text
                )

            except requests.exceptions.RequestException as e:
                logger.error("Webhook attempt %d failed: %s", attempt + 1, str(e))

            if attempt < config.webhooks.max_retries - 1:
                time.sleep(config.webhooks.retry_delay * (attempt + 1))

        logger.error("Webhook failed after %d attempts", config.webhooks.max_retries)
        return False

    def notify_job_completed(self, track_id: str, result: Dict[str, Any]):
        self.send_webhook(
            "job.completed", {"track_id": track_id, "status": "completed", "result": result}
        )

    def notify_job_failed(self, track_id: str, error: str):
        self.send_webhook("job.failed", {"track_id": track_id, "status": "failed", "error": error})


r2_client = CloudflareR2(
    config.r2_storage.account_id,
    config.r2_storage.access_key_id,
    config.r2_storage.secret_access_key,
    config.r2_storage.bucket_name,
)

webhook_manager = WebhookManager(config.webhooks.url, config.webhooks.secret)


@dataclass
class ProcessingJob:
    track_id: str
    status: JobStatus
    progress: int = 0
    created_at: float = 0
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    completed_at: Optional[float] = None


def error_handler(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ValueError as e:
            logger.warning("Validation error in %s: %s", func.__name__, str(e))
            return jsonify({"error": str(e)}), 400
        except (RuntimeError, OSError, KeyError) as e:
            logger.error("Error in %s: %s", func.__name__, str(e))
            return jsonify({"error": "Internal server error"}), 500

    return wrapper


class TaskManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, ProcessingJob] = {}
        self.separator_lock = threading.Lock()
        self._separator: Optional[Separator] = None
        self._executor: Optional[ThreadPoolExecutor] = None

    def get_separator(self) -> Separator:
        if self._separator is not None:
            return self._separator

        with self.separator_lock:
            if self._separator is None:
                try:
                    logger.info("Initializing separator model")
                    self._separator = Separator(
                        output_format="mp3",
                        normalization_threshold=0.9,
                        amplification_threshold=0.9,
                        mdx_params={
                            "hop_length": 512,
                            "segment_size": 128,
                            "overlap": 0.25,
                            "batch_size": 1,
                            "enable_denoise": False,
                        },
                    )
                    self._separator.load_model(model_filename="UVR-MDX-NET-Voc_FT.onnx")
                    logger.info("Separator model initialized successfully")
                except (ImportError, RuntimeError, OSError) as e:
                    logger.error("Failed to initialize separator model: %s", str(e))
                    self._separator = None
                    raise RuntimeError(f"Cannot initialize audio separator: {str(e)}") from e
            return self._separator

    def get_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=config.processing.max_workers)
                logger.info(
                    "Thread executor initialized with %d workers", config.processing.max_workers
                )
            return self._executor

    def submit_job(self, func, *args, **kwargs):
        executor = self.get_executor()
        return executor.submit(func, *args, **kwargs)

    def cleanup_resources(self):
        with self._lock:
            if self._executor is not None:
                try:
                    logger.info("Shutting down thread executor...")
                    self._executor.shutdown(wait=True)
                    self._executor = None
                    logger.info("Thread executor shut down successfully")
                except (RuntimeError, OSError) as e:
                    logger.error("Error shutting down executor: %s", str(e))

            if self._separator is not None:
                try:
                    self._separator = None
                    logger.info("Separator resources cleaned up")
                except (AttributeError, RuntimeError) as e:
                    logger.error("Error cleaning up separator: %s", str(e))

    def update_job(
        self,
        track_id: str,
        status: JobStatus,
        *,
        progress: int = 0,
        error: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ):
        with self._lock:
            if track_id not in self._jobs:
                self._jobs[track_id] = ProcessingJob(
                    track_id=track_id, status=status, created_at=time.time()
                )

            job = self._jobs[track_id]
            job.status = status
            job.progress = progress
            if error:
                job.error = error
            if result:
                job.result = result
            if status in [JobStatus.COMPLETED, JobStatus.FAILED]:
                job.completed_at = time.time()

    def get_job(self, track_id: str) -> Optional[ProcessingJob]:
        with self._lock:
            return self._jobs.get(track_id)

    def count_active_jobs(self) -> int:
        with self._lock:
            return len([j for j in self._jobs.values() if j.status == JobStatus.PROCESSING])

    def cleanup_expired(self):
        current_time = time.time()
        with self._lock:
            expired_jobs = [
                tid
                for tid, job in self._jobs.items()
                if (
                    current_time - job.created_at > config.processing.cleanup_interval
                    and job.status in [JobStatus.COMPLETED, JobStatus.FAILED]
                )
            ]

            if expired_jobs:
                logger.info("Cleaning up %d expired jobs", len(expired_jobs))

            for track_id in expired_jobs:
                try:
                    threading.Thread(
                        target=FileManager.cleanup_track_files, args=(track_id,), daemon=True
                    ).start()
                    del self._jobs[track_id]
                except (KeyError, RuntimeError, OSError, ValueError) as e:
                    logger.error("Error cleaning up job %s: %s", track_id, str(e))

    def get_all_jobs(self) -> Dict[str, ProcessingJob]:
        with self._lock:
            return self._jobs.copy()


task_manager = TaskManager()


class FileManager:
    TRACK_PATTERNS = {
        TrackType.VOCALS: ["vocals", "(Vocals)"],
        TrackType.INSTRUMENTAL: ["instrumental", "(Instrumental)", "no_vocals"],
    }

    @staticmethod
    def cleanup_track_files(track_id: str):
        try:
            for track_type in TrackType:
                r2_key = f"{track_id}/{track_type.value}.mp3"
                try:
                    if r2_client.delete_file(r2_key):
                        logger.debug("Cleaned up R2 file: %s", r2_key)
                except (ClientError, NoCredentialsError, OSError) as e:
                    logger.warning("Failed to clean up R2 file %s: %s", r2_key, e)
        except (TypeError, ValueError, AttributeError) as e:
            logger.error("Unexpected error during cleanup for %s: %s", track_id, e)

    @staticmethod
    def detect_separated_files(output_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
        vocals_file = None
        instrumental_file = None

        for file_path in output_dir.iterdir():
            if file_path.is_file():
                filename = file_path.name.lower()
                for track_type, patterns in FileManager.TRACK_PATTERNS.items():
                    if any(pattern.lower() in filename for pattern in patterns):
                        if track_type == TrackType.VOCALS:
                            vocals_file = file_path
                        else:
                            instrumental_file = file_path
                        break

        return vocals_file, instrumental_file

    @staticmethod
    def upload_to_r2(track_id: str, vocals_file: Path, instrumental_file: Path) -> bool:
        try:
            vocals_key = f"{track_id}/vocals.mp3"
            instrumental_key = f"{track_id}/instrumental.mp3"

            vocals_uploaded = r2_client.upload_file(vocals_file, vocals_key)
            instrumental_uploaded = r2_client.upload_file(instrumental_file, instrumental_key)

            return vocals_uploaded and instrumental_uploaded
        except (OSError, ClientError, NoCredentialsError) as e:
            logger.error("Failed to upload to R2: %s", e)
            return False


def sanitize_input(text: str, max_length: int) -> str:
    """Sanitize and validate user input using validators library"""
    if not validators.length(text, min=1, max=max_length):
        raise ValueError(f"Input must be 1-{max_length} characters long")

    # Remove potentially dangerous characters
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F\x7F-\x9F]', "", text.strip())

    if any(len(word) > 50 for word in sanitized.split()):
        raise ValueError("Input contains words that are too long")

    if not sanitized:
        raise ValueError("Input cannot be empty after sanitization")

    return sanitized


def validate_track_id(track_id: str) -> bool:
    """Validate that track_id is a valid UUID"""
    return validators.uuid(track_id) is True


def build_response(
    track_id: str,
    status: JobStatus,
    message: str,
    status_code: int = 200,
):
    response = {
        "track_id": track_id,
        "status": status.value,
        "message": message,
        "correlation_id": getattr(g, "correlation_id", "unknown"),
    }
    return jsonify(response), status_code


@app.route("/")
def read_root():
    return {
        "status": "healthy",
        "service": "audio_separator",
        "features": {"r2_storage": True, "webhooks": True},
    }


def validate_separation_request() -> Optional[Tuple[Any, int]]:
    """Validate separation request and return error response if invalid"""
    error_msg = None
    status_code = 400

    if not request.is_json:
        error_msg = "Content-Type must be application/json"
    elif request.content_length and request.content_length > 1024:
        error_msg = "Request payload too large"
        status_code = 413
    else:
        try:
            data = request.get_json()
        except (ValueError, TypeError, UnicodeDecodeError):
            error_msg = "Invalid JSON"
        else:
            if not data or not isinstance(data, dict):
                error_msg = "Request body must be a JSON object"
            elif "search_query" not in data:
                error_msg = "Missing 'search_query' field"
            else:
                raw_query = data.get("search_query", "")
                if not isinstance(raw_query, str):
                    error_msg = "search_query must be a string"
                else:
                    try:
                        sanitize_input(raw_query, config.input_limits.max_search_query_length)
                    except ValueError as e:
                        error_msg = f"Input validation failed: {str(e)}"
                    else:
                        if task_manager.count_active_jobs() >= config.processing.max_active_jobs:
                            error_msg = "Server busy. Please try again later."
                            status_code = 429

    if error_msg:
        return jsonify({"error": error_msg}), status_code
    return None


@app.route("/separate", methods=["POST"])
@limiter.limit(config.rate_limits.separation)
@error_handler
def separate_audio():
    """Start audio separation job - returns immediately with job ID"""
    validation_error = validate_separation_request()
    if validation_error:
        return validation_error

    data = request.get_json()
    raw_query = data["search_query"]
    search_query = sanitize_input(raw_query, config.input_limits.max_search_query_length)

    track_id = str(uuid.uuid4())
    task_manager.update_job(track_id, JobStatus.PROCESSING, progress=0)

    task_manager.submit_job(process_audio_background, track_id, search_query)

    correlation_id = getattr(g, "correlation_id", "unknown")
    logger.info(
        "Accepted job %s (correlation: %s) for search query: %s",
        track_id,
        correlation_id,
        search_query[:50] + ("..." if len(search_query) > 50 else ""),
    )
    return build_response(track_id, JobStatus.PROCESSING, "Audio separation started", 202)


def process_audio_background(track_id: str, search_query: str):
    """Background audio processing function with webhook notifications - cloud storage only"""
    temp_dir = None

    try:
        temp_dir = Path(tempfile.mkdtemp())

        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=10)
        logger.info("Downloading audio: %s", track_id)
        downloaded_file, original_title = download_audio(temp_dir, search_query)

        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=40)
        logger.info("Separating audio: %s", track_id)
        vocals_file, instrumental_file = separate_audio_tracks(temp_dir, downloaded_file, track_id)

        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=80)
        logger.info("Uploading to R2: %s", track_id)

        if not FileManager.upload_to_r2(track_id, vocals_file, instrumental_file):
            raise RuntimeError("Failed to upload files to R2 storage")

        result = create_result_dict(track_id, original_title)
        task_manager.update_job(track_id, JobStatus.COMPLETED, progress=100, result=result)

        webhook_manager.notify_job_completed(track_id, result)
        logger.info("Job %s completed successfully", track_id)

    except (RuntimeError, OSError, FileNotFoundError, ValueError) as e:
        error_msg = str(e)
        logger.error("Error processing %s: %s", track_id, error_msg)
        task_manager.update_job(track_id, JobStatus.FAILED, progress=0, error=error_msg)

        webhook_manager.notify_job_failed(track_id, error_msg)

        FileManager.cleanup_track_files(track_id)

    finally:
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def create_result_dict(track_id: str, original_title: Optional[str]) -> Dict[str, str]:
    result = {
        "message": "Audio separation completed successfully",
        "original_title": original_title or "Unknown Title",
        "track_id": track_id,
    }

    vocals_url = r2_client.get_download_url(f"{track_id}/vocals.mp3", expires_in=86400)
    instrumental_url = r2_client.get_download_url(f"{track_id}/instrumental.mp3", expires_in=86400)

    if vocals_url and instrumental_url:
        result["vocals_url"] = vocals_url
        result["instrumental_url"] = instrumental_url
        result["storage"] = "r2"
    else:
        result["error"] = "Failed to generate download URLs from R2 storage"
        result["storage"] = "r2_failed"

    return result


def download_audio(temp_dir: Path, search_query: str) -> Tuple[Path, Optional[str]]:
    extract_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
    }

    try:
        with yt_dlp.YoutubeDL(extract_opts) as ydl:
            search_results = ydl.extract_info(f"ytsearch1:{search_query}", download=False)

            if (
                not search_results
                or "entries" not in search_results
                or not search_results["entries"]
            ):
                raise RuntimeError("No search results found")

            first_entry = search_results["entries"][0]

            if "filesize" in first_entry and first_entry["filesize"]:
                file_size_mb = first_entry["filesize"] / (1024 * 1024)
                if file_size_mb > config.input_limits.max_file_size_mb:
                    raise ValueError(
                        f"File too large: {file_size_mb:.1f}MB "
                        f"(limit: {config.input_limits.max_file_size_mb}MB)"
                    )
            elif "filesize_approx" in first_entry and first_entry["filesize_approx"]:
                file_size_mb = first_entry["filesize_approx"] / (1024 * 1024)
                if file_size_mb > config.input_limits.max_file_size_mb:
                    raise ValueError(
                        f"File too large (approx): {file_size_mb:.1f}MB "
                        f"(limit: {config.input_limits.max_file_size_mb}MB)"
                    )

            ydl_opts = {
                "format": "bestaudio/best",
                "extractaudio": True,
                "audioformat": "mp3",
                "outtmpl": str(temp_dir / "original.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_results = ydl.extract_info(f"ytsearch1:{search_query}", download=True)

            if (
                not search_results
                or "entries" not in search_results
                or not search_results["entries"]
            ):
                raise RuntimeError("No search results found")

            for file_path in temp_dir.iterdir():
                if file_path.name.startswith("original."):
                    file_size_mb = file_path.stat().st_size / (1024 * 1024)
                    if file_size_mb > config.input_limits.max_file_size_mb:
                        raise ValueError(f"File too large: {file_size_mb:.1f}MB")

                    first_entry = search_results["entries"][0]
                    title_value = (
                        first_entry.get("title") if isinstance(first_entry, dict) else None
                    )
                    original_title = (
                        str(title_value) if isinstance(title_value, str) else "Unknown Title"
                    )

                    return file_path, original_title

            raise RuntimeError("Downloaded file not found")

    except (DownloadError, ExtractorError) as e:
        raise RuntimeError(f"YouTube download failed: {str(e)}") from e
    except (OSError, IOError) as e:
        raise RuntimeError(f"File system error during download: {str(e)}") from e
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"Invalid response data from YouTube: {str(e)}") from e


def separate_audio_tracks(
    temp_dir: Path, audio_file: Path, track_id: Optional[str] = None
) -> Tuple[Path, Path]:
    output_dir = temp_dir / "separated"
    output_dir.mkdir(exist_ok=True, parents=True)

    try:
        separator = task_manager.get_separator()

        with task_manager.separator_lock:
            separator.output_dir = str(output_dir)

        if track_id:
            task_manager.update_job(track_id, JobStatus.PROCESSING, progress=50)

        with timeout_context(config.processing.timeout):
            separator.separate(str(audio_file))

        if track_id:
            task_manager.update_job(track_id, JobStatus.PROCESSING, progress=70)

    except (RuntimeError, OSError, TimeoutError) as e:
        raise RuntimeError(f"Audio separation failed: {str(e)}") from e

    vocals_file, instrumental_file = FileManager.detect_separated_files(output_dir)

    if not vocals_file or not instrumental_file:
        available_files = [f.name for f in output_dir.iterdir() if f.is_file()]
        raise FileNotFoundError(f"Could not find separated tracks. Found files: {available_files}")

    return vocals_file, instrumental_file


@app.route("/status/<track_id>", methods=["GET"])
@limiter.limit("60 per minute")
def get_job_status(track_id: str):
    if not validate_track_id(track_id):
        return jsonify({"error": "Invalid track ID format"}), 400

    job = task_manager.get_job(track_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    response = {
        "track_id": track_id,
        "status": job.status.value,
        "progress": job.progress,
        "created_at": job.created_at,
    }

    if job.completed_at:
        response["completed_at"] = job.completed_at
        response["processing_time"] = job.completed_at - job.created_at

    if job.error:
        response["error"] = job.error
    if job.result:
        response["result"] = job.result

    return jsonify(response)


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "services": {
            "separator": True,
            "r2": True,
            "webhooks": True,
        },
        "metrics": {
            "active_jobs": task_manager.count_active_jobs(),
            "max_jobs": config.processing.max_active_jobs,
        },
    }

    if r2_client.client:
        try:
            r2_client.client.head_bucket(Bucket=r2_client.bucket_name)
            health_status["services"]["r2_operational"] = True
        except (ClientError, NoCredentialsError) as e:
            health_status["services"]["r2_operational"] = False
            logger.error("R2 health check failed: %s", e)

    return jsonify(health_status)


class CleanupManager:
    def __init__(self):
        self.last_cleanup_time = 0

    def should_cleanup(self) -> bool:
        current_time = time.time()
        time_limit_seconds = 60 * 5
        if current_time - self.last_cleanup_time > time_limit_seconds:
            self.last_cleanup_time = current_time
            return True
        return False


cleanup_manager = CleanupManager()


@app.before_request
def before_request():
    g.correlation_id = secrets.token_hex(8)

    if cleanup_manager.should_cleanup():
        threading.Thread(target=task_manager.cleanup_expired, daemon=True).start()


def cleanup_resources():
    logger.info("Shutting down...")
    logger.info("Cleaning up task manager resources...")
    try:
        task_manager.cleanup_resources()
    except Exception as e:
        logger.error("Error cleaning up task manager: %s", str(e))

    logger.info("Cleaning up cloud storage files...")
    try:
        for job in task_manager.get_all_jobs().values():
            if job.status == JobStatus.PROCESSING:
                FileManager.cleanup_track_files(job.track_id)
    except Exception as e:
        logger.error("Error during cleanup: %s", str(e))


def signal_handler(signum, _):
    logger.info("Received shutdown signal %s", signum)
    try:
        cleanup_resources()
    except Exception as e:
        logger.error("Error during shutdown: %s", str(e))
    finally:
        sys.exit(0)


def initialize_separator():
    max_retries = 5
    for attempt in range(max_retries):
        try:
            task_manager.get_separator()
            logger.info("Pre-loading separator model...")
            logger.info("Pre-loaded separator model successfully")
            return
        except (RuntimeError, OSError, ImportError, ValueError, FileNotFoundError) as e:
            logger.warning(
                "Failed to pre-load separator model (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                str(e),
            )
            if attempt == max_retries - 1:
                logger.error(
                    "Failed to initialize separator after %d attempts. "
                    "Service may not function properly.",
                    max_retries,
                )
                raise RuntimeError("Cannot start service without separator model") from e
            time.sleep(2**attempt)


def validate_environment():
    warnings = []
    errors = []

    if not config.R2_STORAGE:
        errors.append("R2 storage is required but not configured")
    else:
        required_r2_vars = ["CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]
        missing_r2_vars = [var for var in required_r2_vars if not os.getenv(var)]
        if missing_r2_vars:
            errors.append(
                f"Missing required R2 environment variables: {', '.join(missing_r2_vars)}"
            )

    if not os.getenv("WEBHOOK_URL"):
        warnings.append("WEBHOOK_URL not set - webhooks will not work")

    for w in warnings:
        logger.warning(w)

    for error in errors:
        logger.error(error)

    if errors:
        raise RuntimeError(f"Configuration errors: {'; '.join(errors)}")

    return warnings


if __name__ == "__main__":
    logger.info("Starting service")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    config_warnings = validate_environment()
    if config_warnings:
        logger.warning("Configuration issues detected:")
        for warning in config_warnings:
            logger.warning("  - %s", warning)

    initialize_separator()

    if r2_client.client:
        try:
            r2_client.client.head_bucket(Bucket=r2_client.bucket_name)
            logger.info("R2 connectivity test: passed")
        except (ClientError, NoCredentialsError, OSError) as e:
            logger.error("R2 connectivity test failed: %s", str(e))

    logger.info("Webhook manager initialized and ready")

    try:
        app.run(debug=config.server.debug, host=config.server.host, port=config.server.port)
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    finally:
        cleanup_resources()
