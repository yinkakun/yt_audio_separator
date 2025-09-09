from fastapi import FastAPI

from api.routes import create_fastapi_app
from config.config import Config
from config.logging_config import get_logger, setup_logging
from services.audio_processor import AudioProcessor
from services.storage import CloudflareR2, R2Storage
from services.task_manager import TaskManager
from services.webhook import WebhookManager

logger = get_logger(__name__)


def validate_environment(config):
    """Validate environment configuration"""
    warnings = []
    errors = []

    if not config.r2_storage_enabled:
        errors.append("R2 storage is required but not configured")
    else:
        missing_r2_configs = []
        if not config.cloudflare_account_id:
            missing_r2_configs.append("CLOUDFLARE_ACCOUNT_ID")
        if not config.r2_access_key_id:
            missing_r2_configs.append("R2_ACCESS_KEY_ID")
        if not config.r2_secret_access_key:
            missing_r2_configs.append("R2_SECRET_ACCESS_KEY")
        if not config.r2_public_domain:
            missing_r2_configs.append("R2_PUBLIC_DOMAIN")

        if missing_r2_configs:
            errors.append(
                f"Missing required R2 environment variables: {', '.join(missing_r2_configs)}"
            )

    if not config.webhook_url:
        warnings.append("WEBHOOK_URL not set - webhooks will not work")

    for w in warnings:
        logger.warning(w)

    for error in errors:
        logger.error(error)

    if errors:
        raise RuntimeError(f"Configuration errors: {'; '.join(errors)}")

    return warnings


def create_app() -> FastAPI:
    config = Config()
    config.validate_for_production()

    setup_logging(config.server.debug)

    r2_client = CloudflareR2(
        config=R2Storage(
            s3_url=f"https://{config.cloudflare_account_id}.r2.cloudflarestorage.com",
            bucket_name=config.r2_storage.bucket_name,
            public_domain=config.r2_storage.public_domain,
            access_key_id=config.r2_storage.access_key_id,
            secret_access_key=config.r2_storage.secret_access_key,
        )
    )

    task_manager = TaskManager(max_workers=config.processing.max_active_jobs)
    webhook_manager = WebhookManager(config.webhooks.url, config.webhooks.secret)
    audio_processor = AudioProcessor(
        r2_client,
        webhook_manager,
        task_manager,
    )

    app = create_fastapi_app(config, task_manager, audio_processor, r2_client, webhook_manager)

    return app
