import logging
import logging.handlers
from pathlib import Path


def setup_logging(debug: bool = False):
    """Set up application logging configuration"""
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if not debug:
        try:
            logs_dir = Path("logs")
            logs_dir.mkdir(exist_ok=True)
            rotating_handler = logging.handlers.RotatingFileHandler(
                logs_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5
            )
            handlers.append(rotating_handler)
        except OSError as e:
            print(f"Warning: Could not create log file, using console only: {e}")

    logging.basicConfig(
        level=logging.INFO if not debug else logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

    if not debug:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        logging.getLogger("boto3").setLevel(logging.WARNING)
        logging.getLogger("botocore").setLevel(logging.WARNING)
