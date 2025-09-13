from typing import Any, Dict, Optional
from urllib.parse import urlparse

import redis
from rq import Queue

from config.logging_config import get_logger
from workers.audio_jobs import process_audio_job

logger = get_logger(__name__)


class QueueManager:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._connection = None
        self._queue = None

    def get_redis_connection(self) -> redis.Redis:
        if self._connection is None:
            parsed_url = urlparse(self.redis_url)

            connection_kwargs = {
                "socket_timeout": 30,
                "decode_responses": False,
                "socket_connect_timeout": 30,
            }

            if parsed_url.scheme == "rediss":
                connection_kwargs.update(
                    {
                        "ssl_cert_reqs": None,
                        "ssl_check_hostname": False,
                    }
                )

            try:
                self._connection = redis.from_url(self.redis_url, **connection_kwargs)
                self._connection.ping()
                logger.info("Redis connection established successfully")
            except redis.RedisError as e:
                logger.error(f"Failed to connect to Redis: {e}")
                raise

        return self._connection

    def get_queue(self, queue_name: str = "default") -> Queue:
        """Get RQ queue instance"""
        if self._queue is None:
            connection = self.get_redis_connection()
            self._queue = Queue(queue_name, connection=connection)
            logger.info(f"Queue '{queue_name}' initialized")
        return self._queue

    def enqueue_audio_job(
        self,
        track_id: str,
        search_query: str,
        max_file_size_mb: int,
        processing_timeout: Optional[int],
        webhook_url: str,
        webhook_secret: str,
        cache_key: str,
        storage_config: Dict[str, Any],
        cache_manager_config: Optional[Dict[str, Any]] = None,
        job_timeout: int = 60 * 10,  # 10 minutes
    ):
        """Enqueue audio processing job"""

        queue = self.get_queue()

        job = queue.enqueue(
            process_audio_job,
            track_id,
            search_query,
            max_file_size_mb,
            processing_timeout,
            webhook_url,
            webhook_secret,
            cache_key,
            storage_config,
            cache_manager_config,
            job_id=track_id,
            job_timeout=job_timeout,
        )

        logger.info("Enqueued audio processing job", job_id=job.id, track_id=track_id)
        return job

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Get job status and result"""
        queue = self.get_queue()
        job = queue.fetch_job(job_id)

        if job is None:
            return {"status": "not_found", "track_id": job_id, "message": "Job not found"}

        status_mapping = {
            "queued": "processing",
            "started": "processing",
            "finished": "completed",
            "failed": "failed",
            "deferred": "processing",
            "canceled": "failed",
        }

        status = status_mapping.get(job.get_status(), "unknown")

        result = {
            "status": status,
            "track_id": job_id,
            "created_at": job.created_at.timestamp() if job.created_at else None,
            "started_at": job.started_at.timestamp() if job.started_at else None,
            "ended_at": job.ended_at.timestamp() if job.ended_at else None,
        }

        if job.is_finished and job.result:
            result.update(job.result)
        elif job.is_failed:
            result["error"] = str(job.exc_info) if job.exc_info else "Job failed"

        return result

    def get_queue_info(self) -> Dict[str, Any]:
        queue = self.get_queue()
        return {
            "name": queue.name,
            "length": len(queue),
            "job_ids": queue.job_ids,
        }

    def close(self):
        if self._connection:
            try:
                self._connection.close()
                logger.info("Redis connection closed")
            except redis.RedisError as e:
                logger.error(f"Error closing Redis connection: {e}")
            finally:
                self._connection = None
                self._queue = None
