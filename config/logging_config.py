import logging
import logging.handlers
import sys
from pathlib import Path

import structlog


def setup_logging(debug: bool = False):
    """Set up structured logging configuration"""
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer() if not debug else structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handlers: list[logging.Handler] = []

    console_handler = logging.StreamHandler(sys.stdout)
    handlers.append(console_handler)

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
        format="%(message)s",
        handlers=handlers,
    )

    if not debug:
        logging.getLogger("boto3").setLevel(logging.WARNING)
        logging.getLogger("botocore").setLevel(logging.WARNING)
        logging.getLogger("werkzeug").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance"""
    return structlog.get_logger(name)
