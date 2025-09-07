import logging
import os
import secrets
import sys
import threading

from flask import Flask, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from app.api.routes import create_routes
from app.config.settings import Config
from app.services.audio_processor import AudioProcessor
from app.services.storage import CloudflareR2
from app.services.task_manager import TaskManager
from app.services.webhook import WebhookManager
from app.utils.cleanup import CleanupManager
from app.utils.logging_config import setup_logging

from app.models.job import JobStatus
from app.services.file_manager import FileManager

logger = logging.getLogger(__name__)


def validate_environment(config):
    """Validate environment configuration"""
    warnings = []
    errors = []

    if not config.R2_STORAGE:
        errors.append("R2 storage is required but not configured")
    else:
        required_r2_vars = [
            "CLOUDFLARE_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_PUBLIC_DOMAIN",
        ]
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


def create_app():
    """Application factory"""
    config = Config()
    config.validate()

    setup_logging(config.server.debug)

    app = Flask(__name__)

    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        raise RuntimeError("SECRET_KEY environment variable is required")
    app.config["SECRET_KEY"] = secret_key
    app.config["MAX_CONTENT_LENGTH"] = config.input_limits.max_file_size_mb * 1024 * 1024

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    r2_client = CloudflareR2(
        config.r2_storage.account_id,
        config.r2_storage.access_key_id,
        config.r2_storage.secret_access_key,
        config.r2_storage.bucket_name,
        config.r2_storage.public_domain,
    )

    webhook_manager = WebhookManager(config.webhooks.url, config.webhooks.secret)
    task_manager = TaskManager()
    audio_processor = AudioProcessor(task_manager, r2_client, webhook_manager)
    cleanup_manager = CleanupManager()

    limiter = Limiter(
        app=app, key_func=get_remote_address, default_limits=[config.rate_limits.requests]
    )

    api_bp = create_routes(config, task_manager, audio_processor, r2_client, limiter)
    app.register_blueprint(api_bp)

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

    @app.before_request
    def before_request():
        g.correlation_id = secrets.token_hex(8)

        if cleanup_manager.should_cleanup():
            threading.Thread(target=task_manager.cleanup_expired, daemon=True).start()

    def cleanup_resources():
        """Clean up application resources on shutdown"""
        logger.info("Shutting down...")
        logger.info("Cleaning up task manager resources...")
        try:
            task_manager.cleanup_resources()
        except (RuntimeError, OSError) as e:
            logger.error("Error cleaning up task manager: %s", str(e))

        logger.info("Cleaning up cloud storage files...")
        try:

            for job in task_manager.get_all_jobs().values():
                if job.status == JobStatus.PROCESSING:
                    FileManager.cleanup_track_files(job.track_id, r2_client)
        except (RuntimeError, OSError, AttributeError) as e:
            logger.error("Error during cleanup: %s", str(e))

    def signal_handler(signum, _):
        """Handle shutdown signals"""
        logger.info("Received shutdown signal %s", signum)
        try:
            cleanup_resources()
        except (RuntimeError, OSError) as e:
            logger.error("Error during shutdown: %s", str(e))
        finally:
            sys.exit(0)

    app.config["task_manager"] = task_manager
    app.config["cleanup_resources"] = cleanup_resources
    app.config["signal_handler"] = signal_handler

    return app
