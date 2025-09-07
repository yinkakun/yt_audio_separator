import logging
import signal
import time

from botocore.exceptions import ClientError, NoCredentialsError

from app import create_app, validate_environment
from app.config.settings import Config
from app.services.storage import CloudflareR2

logger = logging.getLogger(__name__)


def initialize_separator(task_manager):
    """Initialize the audio separator with retry logic"""
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


def main():
    """Main application entry point"""
    config = Config()
    app = create_app()
    logger.info("Starting service")

    signal.signal(signal.SIGINT, app.config["signal_handler"])
    signal.signal(signal.SIGTERM, app.config["signal_handler"])

    config_warnings = validate_environment(config)
    if config_warnings:
        logger.warning("Configuration issues detected:")
        for warning in config_warnings:
            logger.warning("  - %s", warning)

    task_manager = app.config["task_manager"]
    initialize_separator(task_manager)

    # Test R2 connectivity
    r2_client = CloudflareR2(
        config.r2_storage.account_id,
        config.r2_storage.access_key_id,
        config.r2_storage.secret_access_key,
        config.r2_storage.bucket_name,
        config.r2_storage.public_domain,
    )
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
        app.config["cleanup_resources"]()


if __name__ == "__main__":
    main()
