import logging
import time
import uuid
from functools import wraps
from typing import Any, Optional, Tuple

from flask import Blueprint, g, jsonify, request
from flask_limiter.util import get_remote_address

from app.models.job import JobStatus
from app.utils.validation import sanitize_input, validate_track_id

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)


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


def validate_separation_request(config) -> Optional[Tuple[Any, int]]:
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

    if error_msg:
        return jsonify({"error": error_msg}), status_code
    return None


def create_routes(config, task_manager, audio_processor, r2_client, limiter):
    @api_bp.route("/")
    def read_root():
        return {
            "status": "healthy",
            "service": "audio_separator",
            "features": {"r2_storage": True, "webhooks": True},
        }

    @api_bp.route("/separate", methods=["POST"])
    @limiter.limit(config.rate_limits.separation)
    @error_handler
    def separate_audio():
        """Start audio separation job - returns immediately with job ID"""
        validation_error = validate_separation_request(config)
        if validation_error:
            return validation_error

        if task_manager.count_active_jobs() >= config.processing.max_active_jobs:
            return jsonify({"error": "Server busy. Please try again later."}), 429

        data = request.get_json()
        raw_query = data["search_query"]
        search_query = sanitize_input(raw_query, config.input_limits.max_search_query_length)

        track_id = str(uuid.uuid4())
        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=0)

        task_manager.submit_job(
            audio_processor.process_audio_background,
            track_id,
            search_query,
            config.input_limits.max_file_size_mb,
            config.processing.timeout,
        )

        correlation_id = getattr(g, "correlation_id", "unknown")
        logger.info(
            "Accepted job %s (correlation: %s) for search query: %s",
            track_id,
            correlation_id,
            search_query[:50] + ("..." if len(search_query) > 50 else ""),
        )
        return build_response(track_id, JobStatus.PROCESSING, "Audio separation started", 202)

    @api_bp.route("/status/<track_id>", methods=["GET"])
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

    @api_bp.route("/health", methods=["GET"])
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
            except (OSError, RuntimeError) as e:
                health_status["services"]["r2_operational"] = False
                logger.error("R2 health check failed: %s", e)

        return jsonify(health_status)

    return api_bp
