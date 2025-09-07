import logging
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional, Tuple

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from app.models.job import JobStatus
from app.services.file_manager import FileManager

logger = logging.getLogger(__name__)


@contextmanager
def timeout_context(timeout_seconds: int):
    """Context manager for timeout handling with proper cleanup"""
    timer = None

    def timeout_handler():
        raise TimeoutError(f"Operation timed out after {timeout_seconds} seconds")

    try:
        timer = threading.Timer(timeout_seconds, timeout_handler)
        timer.start()
        yield
    finally:
        if timer:
            timer.cancel()


class AudioProcessor:
    def __init__(self, task_manager, r2_client, webhook_manager):
        self.task_manager = task_manager
        self.r2_client = r2_client
        self.webhook_manager = webhook_manager

    def process_audio_background(
        self, track_id: str, search_query: str, max_file_size_mb: int, processing_timeout: int
    ):
        """Background audio processing function with webhook notifications"""
        temp_dir = None

        try:
            temp_dir = Path(tempfile.mkdtemp())

            self.task_manager.update_job(track_id, JobStatus.PROCESSING, progress=10)
            logger.info("Downloading audio: %s", track_id)
            downloaded_file, original_title = self.download_audio(
                temp_dir, search_query, max_file_size_mb
            )

            self.task_manager.update_job(track_id, JobStatus.PROCESSING, progress=40)
            logger.info("Separating audio: %s", track_id)
            vocals_file, instrumental_file = self.separate_audio_tracks(
                temp_dir, downloaded_file, track_id, processing_timeout
            )

            self.task_manager.update_job(track_id, JobStatus.PROCESSING, progress=80)
            logger.info("Uploading to R2: %s", track_id)

            upload_success = FileManager.upload_to_r2(
                track_id, vocals_file, instrumental_file, self.r2_client
            )
            if not upload_success:
                raise RuntimeError("Failed to upload files to R2 storage")

            result = self.create_result_dict(track_id, original_title)
            self.task_manager.update_job(track_id, JobStatus.COMPLETED, progress=100, result=result)

            self.webhook_manager.notify_job_completed(track_id, result)
            logger.info("Job %s completed successfully", track_id)

        except (RuntimeError, OSError, FileNotFoundError, ValueError) as e:
            error_msg = str(e)
            logger.error("Error processing %s: %s", track_id, error_msg)
            self.task_manager.update_job(track_id, JobStatus.FAILED, progress=0, error=error_msg)

            self.webhook_manager.notify_job_failed(track_id, error_msg)
            FileManager.cleanup_track_files(track_id, self.r2_client)

        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def create_result_dict(self, track_id: str, original_title: Optional[str]) -> Dict[str, str]:
        result = {
            "message": "Audio separation completed successfully",
            "original_title": original_title or "Unknown Title",
            "track_id": track_id,
        }

        vocals_url = self.r2_client.get_download_url(
            f"{track_id}/vocals.mp3", self.r2_client.public_domain
        )
        instrumental_url = self.r2_client.get_download_url(
            f"{track_id}/instrumental.mp3", self.r2_client.public_domain
        )

        if vocals_url and instrumental_url:
            result["vocals_url"] = vocals_url
            result["instrumental_url"] = instrumental_url
            result["storage"] = "r2"
        else:
            result["error"] = "Failed to generate download URLs from R2 storage"
            result["storage"] = "r2_failed"

        return result

    def download_audio(
        self, temp_dir: Path, search_query: str, max_file_size_mb: int
    ) -> Tuple[Path, Optional[str]]:
        extract_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio/best",
        }

        try:
            with yt_dlp.YoutubeDL(extract_opts) as ydl:
                search_results = ydl.extract_info(f"ytsearch1:{search_query}", download=False)

                if (
                    not search_results
                    or "entries" not in search_results
                    or not search_results["entries"]
                ):
                    raise RuntimeError("No search results found")

                first_entry = search_results["entries"][0]

                if "filesize" in first_entry and first_entry["filesize"]:
                    file_size_mb = first_entry["filesize"] / (1024 * 1024)
                    if file_size_mb > max_file_size_mb:
                        raise ValueError(
                            f"File too large: {file_size_mb:.1f}MB "
                            f"(limit: {max_file_size_mb}MB)"
                        )
                elif "filesize_approx" in first_entry and first_entry["filesize_approx"]:
                    file_size_mb = first_entry["filesize_approx"] / (1024 * 1024)
                    if file_size_mb > max_file_size_mb:
                        raise ValueError(
                            f"File too large (approx): {file_size_mb:.1f}MB "
                            f"(limit: {max_file_size_mb}MB)"
                        )

                ydl_opts = {
                    "format": "bestaudio/best",
                    "extractaudio": True,
                    "audioformat": "mp3",
                    "outtmpl": str(temp_dir / "original.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                    "extract_flat": False,
                }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                search_results = ydl.extract_info(f"ytsearch1:{search_query}", download=True)

                if (
                    not search_results
                    or "entries" not in search_results
                    or not search_results["entries"]
                ):
                    raise RuntimeError("No search results found")

                for file_path in temp_dir.iterdir():
                    if file_path.name.startswith("original."):
                        file_size_mb = file_path.stat().st_size / (1024 * 1024)
                        if file_size_mb > max_file_size_mb:
                            raise ValueError(f"File too large: {file_size_mb:.1f}MB")

                        first_entry = search_results["entries"][0]
                        title_value = (
                            first_entry.get("title") if isinstance(first_entry, dict) else None
                        )
                        original_title = (
                            str(title_value) if isinstance(title_value, str) else "Unknown Title"
                        )

                        return file_path, original_title

                raise RuntimeError("Downloaded file not found")

        except (DownloadError, ExtractorError) as e:
            raise RuntimeError(f"YouTube download failed: {str(e)}") from e
        except (OSError, IOError) as e:
            raise RuntimeError(f"File system error during download: {str(e)}") from e
        except (KeyError, TypeError, ValueError) as e:
            raise RuntimeError(f"Invalid response data from YouTube: {str(e)}") from e

    def separate_audio_tracks(
        self,
        temp_dir: Path,
        audio_file: Path,
        track_id: Optional[str] = None,
        processing_timeout: int = 60,
    ) -> Tuple[Path, Path]:
        output_dir = temp_dir / "separated"
        output_dir.mkdir(exist_ok=True, parents=True)

        try:
            separator = self.task_manager.get_separator()

            with self.task_manager.separator_lock:
                separator.output_dir = str(output_dir)

            if track_id:
                self.task_manager.update_job(track_id, JobStatus.PROCESSING, progress=50)

            with timeout_context(processing_timeout):
                separator.separate(str(audio_file))

            if track_id:
                self.task_manager.update_job(track_id, JobStatus.PROCESSING, progress=70)

        except (RuntimeError, OSError, TimeoutError) as e:
            raise RuntimeError(f"Audio separation failed: {str(e)}") from e

        vocals_file, instrumental_file = FileManager.detect_separated_files(output_dir)

        if not vocals_file or not instrumental_file:
            available_files = [f.name for f in output_dir.iterdir() if f.is_file()]
            raise FileNotFoundError(
                f"Could not find separated tracks. Found files: {available_files}"
            )

        return vocals_file, instrumental_file
