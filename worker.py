import os
import signal
import sys

from rq import Queue, Worker

from config.config import Config
from config.logging_config import get_logger, setup_logging
from workers.queue_manager import QueueManager

logger = get_logger(__name__)


def signal_handler(signum, _frame):
    logger.info(f"Received signal {signum}, shutting down worker...")
    sys.exit(0)


def main():
    """Main worker process"""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    config = Config()
    setup_logging(config.server.debug)

    if not config.redis_enabled:
        logger.error("Redis URL not configured. Set REDIS_URL environment variable.")
        sys.exit(1)

    worker_name = os.getenv("WORKER_NAME")
    queue_names = os.getenv("QUEUE_NAMES", "default").split(",")
    worker_concurrency = int(os.getenv("WORKER_CONCURRENCY", "1"))

    logger.info(
        "Starting RQ worker",
        worker_name=worker_name,
        queue_names=queue_names,
        concurrency=worker_concurrency,
        redis_url=config.redis_url[:50] + "...",
    )

    try:
        queue_manager = QueueManager(config.redis_url)
        redis_connection = queue_manager.get_redis_connection()

        queues = [Queue(name, connection=redis_connection) for name in queue_names]

        worker = Worker(
            queues=queues,
            name=worker_name,
            connection=redis_connection,
        )

        logger.info("Worker started successfully", worker_name=worker.name, worker_pid=os.getpid())
        worker.work(with_scheduler=True)
    except (ConnectionError, RuntimeError) as e:
        logger.error(f"Worker failed to start: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Worker shutdown complete")


if __name__ == "__main__":
    main()
