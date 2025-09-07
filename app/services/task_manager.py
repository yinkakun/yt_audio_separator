import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from app.models.job import JobStatus, ProcessingJob
from app.services.file_manager import FileManager

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, max_workers: int = 2, audio_processor=None):
        self._lock = threading.Lock()
        self._async_lock = asyncio.Lock()
        self._jobs: Dict[str, ProcessingJob] = {}
        self._executor: Optional[ThreadPoolExecutor] = None
        self.max_workers = max_workers
        self.audio_processor = audio_processor

    def get_executor(self, max_workers: int = 2) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is not None:
                return self._executor

            self._executor = ThreadPoolExecutor(max_workers=max_workers)
            logger.info("Thread executor initialized with %d workers", max_workers)
            return self._executor

    def submit_job(self, func, *args, **kwargs):
        executor = self.get_executor()
        return executor.submit(func, *args, **kwargs)

    def cleanup_resources(self):
        with self._lock:
            if self._executor is None:
                return

            try:
                logger.info("Shutting down thread executor...")
                self._executor.shutdown(wait=True)
                self._executor = None
                logger.info("Thread executor shut down successfully")
            except (RuntimeError, OSError) as e:
                logger.error("Error shutting down executor: %s", str(e))

    def _update_job_data(
        self,
        track_id: str,
        status: JobStatus,
        *,
        progress: int = 0,
        error: Optional[str] = None,
        result: Optional[Dict] = None,
    ):
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

    def update_job(
        self,
        track_id: str,
        status: JobStatus,
        *,
        progress: int = 0,
        error: Optional[str] = None,
        result: Optional[Dict] = None,
    ):
        with self._lock:
            self._update_job_data(track_id, status, progress=progress, error=error, result=result)

    def get_job(self, track_id: str) -> Optional[ProcessingJob]:
        with self._lock:
            return self._jobs.get(track_id)

    def count_active_jobs(self) -> int:
        with self._lock:
            return len([j for j in self._jobs.values() if j.status == JobStatus.PROCESSING])

    def cleanup_expired(self, cleanup_interval: int = 3600):
        current_time = time.time()
        with self._lock:
            expired_jobs = [
                tid
                for tid, job in self._jobs.items()
                if (
                    current_time - job.created_at > cleanup_interval
                    and job.status in [JobStatus.COMPLETED, JobStatus.FAILED]
                )
            ]

            if not expired_jobs:
                return

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

    async def update_job_async(
        self,
        track_id: str,
        status: JobStatus,
        *,
        progress: int = 0,
        error: Optional[str] = None,
        result: Optional[Dict] = None,
    ):
        async with self._async_lock:
            self._update_job_data(track_id, status, progress=progress, error=error, result=result)

    async def get_job_async(self, track_id: str) -> Optional[ProcessingJob]:
        async with self._async_lock:
            return self._jobs.get(track_id)

    async def count_active_jobs_async(self) -> int:
        async with self._async_lock:
            return len([j for j in self._jobs.values() if j.status == JobStatus.PROCESSING])

    def cleanup(self):
        self.cleanup_resources()
