import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from audio_separator.separator import Separator

from app.models.job import JobStatus, ProcessingJob
from app.services.file_manager import FileManager

logger = logging.getLogger(__name__)


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

    def get_executor(self, max_workers: int = 2) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=max_workers)
                logger.info("Thread executor initialized with %d workers", max_workers)
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
        result: Optional[Dict] = None,
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
