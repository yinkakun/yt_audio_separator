import logging
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional, Tuple, Protocol

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError
from audio_separator.separator import Separator

from src.models.job import JobStatus
from src.services.file_manager import FileManager

logger = logging.getLogger(__name__)


class JobProgressTracker(Protocol):
    def update_job(
        self,
        track_id: str,
        status: JobStatus,
        *,
        progress: int = 0,
        error: Optional[str] = None,
        result: Optional[Dict] = None,
    ) -> None: ...


class SeparatorProvider(Protocol):
    def get_separator(self) -> Separator: ...

    @property
    def separator_lock(self) -> threading.Lock: ...


class DefaultSeparatorProvider:
    def __init__(self):
        self._separator: Optional[Separator] = None
        self._separator_lock = threading.Lock()

    def get_separator(self) -> Separator:
        if self._separator is not None:
            return self._separator

        with self._separator_lock:
            if self._separator is not None:
                return self._separator

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
                return self._separator
            except (ImportError, RuntimeError, OSError) as e:
                logger.error("Failed to initialize separator model: %s", str(e))
                self._separator = None
                raise RuntimeError(f"Cannot initialize audio separator: {str(e)}") from e

    @property
    def separator_lock(self) -> threading.Lock:
        return self._separator_lock


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
    def __init__(
        self,
        r2_client,
        webhook_manager,
        progress_tracker: Optional[JobProgressTracker] = None,
        separator_provider: Optional[SeparatorProvider] = None,
    ):
        self.r2_client = r2_client
        self.webhook_manager = webhook_manager
        self.progress_tracker = progress_tracker
        self.separator_provider = separator_provider or DefaultSeparatorProvider()

    def process_audio(
        self, track_id: str, search_query: str, max_file_size_mb: int, processing_timeout: int
    ):
        temp_dir = None

        try:
            temp_dir = Path(tempfile.mkdtemp())

            if self.progress_tracker:
                self.progress_tracker.update_job(track_id, JobStatus.PROCESSING, progress=10)
            logger.info("Downloading audio: %s", track_id)
            downloaded_file, original_title = self.download_audio(
                temp_dir, search_query, max_file_size_mb
            )

            if self.progress_tracker:
                self.progress_tracker.update_job(track_id, JobStatus.PROCESSING, progress=40)
            logger.info("Separating audio: %s", track_id)
            vocals_file, instrumental_file = self.separate_audio_tracks(
                temp_dir, downloaded_file, track_id, processing_timeout
            )

            if self.progress_tracker:
                self.progress_tracker.update_job(track_id, JobStatus.PROCESSING, progress=80)
            logger.info("Uploading to R2: %s", track_id)

            FileManager.upload_to_r2(track_id, vocals_file, instrumental_file, self.r2_client)
            result = self.create_result_dict(track_id, original_title)
            if self.progress_tracker:
                self.progress_tracker.update_job(
                    track_id, JobStatus.COMPLETED, progress=100, result=result
                )

        except (RuntimeError, OSError, FileNotFoundError, ValueError) as e:
            error_msg = str(e)
            logger.error("Error processing %s: %s", track_id, error_msg)
            if self.progress_tracker:
                self.progress_tracker.update_job(
                    track_id, JobStatus.FAILED, progress=0, error=error_msg
                )

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

        if not vocals_url or not instrumental_url:
            result["error"] = "Failed to generate download URLs from R2 storage"
            result["storage"] = "r2_failed"
            return result

        result["vocals_url"] = vocals_url
        result["instrumental_url"] = instrumental_url
        result["storage"] = "r2"
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

                if "filesize_approx" in first_entry and first_entry["filesize_approx"]:
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
                    if not file_path.name.startswith("original."):
                        continue

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
        processing_timeout: int = 60 * 5,
    ) -> Tuple[Path, Path]:
        output_dir = temp_dir / "separated"
        output_dir.mkdir(exist_ok=True, parents=True)
        try:
            if track_id and self.progress_tracker:
                self.progress_tracker.update_job(track_id, JobStatus.PROCESSING, progress=50)

            separator = self.separator_provider.get_separator()

            with self.separator_provider.separator_lock:
                separator.output_dir = str(output_dir)

                with timeout_context(processing_timeout):
                    separator.separate(str(audio_file))

            if track_id and self.progress_tracker:
                self.progress_tracker.update_job(track_id, JobStatus.PROCESSING, progress=70)

        except (RuntimeError, OSError, TimeoutError) as e:
            raise RuntimeError(f"Audio separation failed: {str(e)}") from e

        vocals_file, instrumental_file = FileManager.detect_separated_files(output_dir)

        if not vocals_file or not instrumental_file:
            available_files = [f.name for f in output_dir.iterdir() if f.is_file()]
            raise FileNotFoundError(
                f"Could not find separated tracks. Found files: {available_files}"
            )

        return vocals_file, instrumental_file

    def download_audio_simple(self, search_query: str, max_file_size_mb: int) -> Path:
        temp_dir = Path(tempfile.mkdtemp())
        try:
            downloaded_file, _ = self.download_audio(temp_dir, search_query, max_file_size_mb)
            return downloaded_file
        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def separate_audio(self, audio_file: Path) -> Tuple[Path, Path]:
        temp_dir = audio_file.parent / "separated"
        temp_dir.mkdir(exist_ok=True, parents=True)

        try:

            separator = Separator()
            separator.output_dir = str(temp_dir)

            separator.separate(str(audio_file))

            vocals_file, instrumental_file = FileManager.detect_separated_files(temp_dir)

            if not vocals_file or not instrumental_file:
                available_files = [f.name for f in temp_dir.iterdir() if f.is_file()]
                raise FileNotFoundError(
                    f"Could not find separated tracks. Found files: {available_files}"
                )

            return vocals_file, instrumental_file

        except Exception as e:
            raise RuntimeError(f"Audio separation failed: {str(e)}") from e
