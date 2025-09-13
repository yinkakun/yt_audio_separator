import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Dict, Optional, Protocol, Tuple

import yt_dlp
from audio_separator.separator import Separator
from yt_dlp.utils import DownloadError, ExtractorError

from config.logging_config import get_logger

logger = get_logger(__name__)

MODEL_FILENAME = "UVR-MDX-NET-Voc_FT.onnx"

mdx_params = {
    "hop_length": 1024,
    "segment_size": 256,
    "overlap": 0.25,
    "batch_size": 1,
    "enable_denoise": False,
}


class SeparatorProvider(Protocol):
    def get_separator(self) -> Separator: ...

    def initialize_model(self) -> None: ...

    @property
    def separator_lock(self) -> threading.Lock: ...


class DefaultSeparatorProvider:
    def __init__(self, models_dir: Optional[str] = None):
        self._separator: Optional[Separator] = None
        self._separator_lock = threading.Lock()
        self.model_load_timeout = 60 * 15  # 15 minutes
        self.working_dir = Path("audio_workspace")
        self.models_dir = models_dir or os.getenv("MODELS_DIR", "/tmp/audio-separator-models")

    def initialize_model(self) -> None:
        with self._separator_lock:
            if self._separator is not None:
                return

            try:
                self._separator = self._create_separator()
                self._separator.load_model(model_filename=MODEL_FILENAME)
                logger.info("Separator model preloaded successfully", model=MODEL_FILENAME)
            except (ImportError, RuntimeError, OSError) as e:
                logger.error(
                    "Failed to preload separator model on startup",
                    error=str(e),
                    model=MODEL_FILENAME,
                    models_dir=self.models_dir,
                )
                self._separator = None
                raise RuntimeError(f"Cannot preload audio separator: {str(e)}") from e

    def _create_separator(self) -> Separator:
        models_path = Path(self.models_dir)
        models_path.mkdir(parents=True, exist_ok=True)

        separator_config = {
            "output_dir": str(self.working_dir.resolve()),
            "output_format": "WAV",
            "output_bitrate": "320k",
            "normalization_threshold": 0.9,
            "amplification_threshold": 0.9,
            "mdx_params": mdx_params,
            "model_file_dir": self.models_dir,
        }

        try:
            separator = Separator(**separator_config)
            logger.info("Separator created successfully with config", config=separator_config)
            return separator
        except Exception as e:
            logger.error("Failed to create separator", error=str(e), config=separator_config)
            raise

    def get_separator(self) -> Separator:
        if self._separator is not None:
            return self._separator

        with self._separator_lock:
            if self._separator is not None:
                return self._separator

            try:
                self._separator = self._create_separator()
                self._separator.load_model(model_filename=MODEL_FILENAME)
                return self._separator
            except (ImportError, RuntimeError, OSError) as e:
                self._separator = None
                raise RuntimeError(f"Cannot initialize audio separator: {str(e)}") from e

    @property
    def separator_lock(self) -> threading.Lock:
        return self._separator_lock


def run_with_timeout(fn, *args, timeout: int = 300):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn, *args)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout as exc:
            future.cancel()
            raise TimeoutError(f"Operation timed out after {timeout} seconds") from exc


def retry_with_backoff(func, max_retries=3, base_delay=1, backoff_factor=2, max_delay=60):
    """Retry function with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return func()
        except (OSError, RuntimeError) as e:
            if attempt == max_retries - 1:
                raise e

            delay = min(base_delay * (backoff_factor**attempt), max_delay)
            logger.warning(
                "Attempt failed, retrying",
                attempt=attempt + 1,
                max_retries=max_retries,
                delay=delay,
                error=str(e),
            )
            time.sleep(delay)


class AudioProcessor:

    def __init__(
        self,
        storage,
        models_dir: Optional[str] = None,
        working_dir: Optional[str] = None,
    ):
        self.storage = storage
        working_dir_path = working_dir or os.getenv("AUDIO_WORKSPACE_DIR", "audio_workspace")
        self.working_dir = Path(working_dir_path)
        self.separator_provider = DefaultSeparatorProvider(models_dir=models_dir)
        self._ensure_working_dir()

    def _ensure_working_dir(self) -> None:
        """Ensure the working directory exists."""
        self.working_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Working directory initialized", path=str(self.working_dir))

    def cleanup_working_dir(self) -> None:
        """Clean up the entire working directory."""
        if self.working_dir.exists():
            shutil.rmtree(self.working_dir, ignore_errors=True)
            logger.info("Working directory cleaned up", path=str(self.working_dir))
            self._ensure_working_dir()

    def initialize_model(self) -> None:
        """Initialize the separator model on startup to download it before first request."""
        self.separator_provider.initialize_model()

    def _validate_file_size(self, size_bytes: int, max_file_size_mb: int = 100) -> None:
        """Validate file size against maximum allowed size."""
        size_mb = int(size_bytes) / (1024 * 1024)
        if size_mb > max_file_size_mb:
            raise ValueError(f"File too large: {size_mb:.1f}MB (limit: {max_file_size_mb}MB)")

    def _validate_file_size_approx(self, size_bytes: int, max_file_size_mb: int = 100) -> None:
        """Validate approximate file size against maximum allowed size."""
        size_mb = int(size_bytes) / (1024 * 1024)
        if size_mb > max_file_size_mb:
            raise ValueError(
                f"File too large (approx): {size_mb:.1f}MB (limit: {max_file_size_mb}MB)"
            )

    def _validate_audio_file(self, audio_file: Path) -> None:
        """Validate audio file before processing."""
        if not audio_file.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_file}")

        if audio_file.stat().st_size == 0:
            raise ValueError(f"Audio file is empty: {audio_file}")

        # Check file extension
        allowed_extensions = {".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg"}
        if audio_file.suffix.lower() not in allowed_extensions:
            raise ValueError(f"Unsupported audio format: {audio_file.suffix}")

    def _validate_separated_files(self, output_dir: Path, track_id: str = "") -> Tuple[Path, Path]:
        vocals_file, instrumental_file = self.storage.detect_separated_files(output_dir)

        if not vocals_file or not instrumental_file:
            available_files = [f.name for f in output_dir.iterdir() if f.is_file()]
            if track_id:
                logger.error(
                    "Could not find separated tracks after separation",
                    track_id=track_id,
                    available_files=available_files,
                    output_dir=str(output_dir),
                )
            raise FileNotFoundError(
                f"Could not find separated tracks. Found files: {available_files}"
            )

        return vocals_file, instrumental_file

    def process_audio(
        self,
        track_id: str,
        search_query: str,
        max_file_size_mb: int,
        processing_timeout: Optional[int] = None,
    ) -> Dict[str, str]:
        job_dir: Optional[Path] = None
        timeout_value = processing_timeout

        try:
            job_dir = self.working_dir / track_id
            job_dir.mkdir(parents=True, exist_ok=True)

            logger.info(
                "Downloading audio",
                track_id=track_id,
                query=search_query,
            )
            downloaded_file, original_title = self.download_audio(
                job_dir, search_query, max_file_size_mb
            )

            logger.info("Separating audio", track_id=track_id, timeout=timeout_value)
            vocals_file, instrumental_file = self.separate_audio_tracks(
                job_dir,
                downloaded_file,
                track_id,
            )

            logger.info("Uploading separated tracks to storage", track_id=track_id)
            self.storage.upload_track_files(track_id, vocals_file, instrumental_file)

            result = self.create_result_dict(track_id, original_title)
            logger.info(
                "Job completed successfully", track_id=track_id, result_keys=list(result.keys())
            )
            return result

        except (RuntimeError, OSError, FileNotFoundError, ValueError, TimeoutError) as e:
            error_msg = str(e)
            logger.error(
                "Audio processing failed", track_id=track_id, error=error_msg, query=search_query
            )
            logger.error("Job failed", track_id=track_id, error=error_msg)
            self.storage.cleanup_track_files(track_id)
            raise

        finally:
            if job_dir and job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
                logger.info("Cleaned up job directory", track_id=track_id, path=str(job_dir))

    def create_result_dict(self, track_id: str, original_title: Optional[str]) -> Dict[str, str]:
        result = {
            "original_title": original_title or "Unknown Title",
            "track_id": track_id,
        }

        vocals_url = self.storage.get_download_url(
            f"{track_id}/vocals.mp3", self.storage.public_domain
        )
        instrumental_url = self.storage.get_download_url(
            f"{track_id}/instrumental.mp3", self.storage.public_domain
        )

        if not vocals_url or not instrumental_url:
            result["error"] = "Failed to generate download URLs from R2 storage"
            result["storage"] = "r2_failed"
            return result

        result["vocals_url"] = vocals_url
        result["instrumental_url"] = instrumental_url
        result["storage"] = "r2"
        return result

    def _get_base_ydl_opts(self) -> dict:
        return {
            "quiet": True,
            "no_warnings": True,
        }

    def _validate_search_results(self, search_results: dict) -> dict:
        if not search_results or "entries" not in search_results or not search_results["entries"]:
            raise RuntimeError("No search results found")
        return search_results["entries"][0]

    def _validate_entry_file_size(self, entry: dict, max_file_size_mb: int) -> None:
        if "filesize" in entry and entry["filesize"]:
            self._validate_file_size(entry["filesize"], max_file_size_mb)
        elif "filesize_approx" in entry and entry["filesize_approx"]:
            self._validate_file_size_approx(entry["filesize_approx"], max_file_size_mb)

    def _extract_title_from_entry(self, entry: dict) -> str:
        title_value = entry.get("title") if isinstance(entry, dict) else None
        return str(title_value) if isinstance(title_value, str) else "Unknown Title"

    def download_audio(
        self, job_dir: Path, search_query: str, max_file_size_mb: int
    ) -> Tuple[Path, Optional[str]]:
        def _download():
            search_term = f"ytsearch1:{search_query}"
            extract_opts = {**self._get_base_ydl_opts(), "format": "bestaudio/best"}
            with yt_dlp.YoutubeDL(extract_opts) as ydl:
                search_results = ydl.extract_info(search_term, download=False)
                if search_results is None:
                    raise RuntimeError("No search results found")
                first_entry = self._validate_search_results(search_results)
                self._validate_entry_file_size(first_entry, max_file_size_mb)

            download_opts = {
                **self._get_base_ydl_opts(),
                "format": "bestaudio[ext=m4a]/bestaudio",
                "outtmpl": str(job_dir / "original.%(ext)s"),
            }
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                search_results = ydl.extract_info(search_term, download=True)
                if search_results is None:
                    raise RuntimeError("No search results found")
                first_entry = self._validate_search_results(search_results)

                for file_path in job_dir.iterdir():
                    if file_path.name.startswith("original."):
                        self._validate_file_size(file_path.stat().st_size, max_file_size_mb)
                        original_title = self._extract_title_from_entry(first_entry)
                        return file_path, original_title

                raise RuntimeError("Downloaded file not found")

        try:
            return run_with_timeout(_download, timeout=300)
        except (DownloadError, ExtractorError) as e:
            raise RuntimeError(f"YouTube download failed: {str(e)}") from e
        except (OSError, IOError) as e:
            raise RuntimeError(f"File system error during download: {str(e)}") from e
        except (KeyError, TypeError, ValueError) as e:
            raise RuntimeError(f"Invalid response data from YouTube: {str(e)}") from e

    def separate_audio_tracks(
        self,
        job_dir: Path,
        audio_file: Path,
        track_id: str,
        processing_timeout: int = 300,
    ) -> Tuple[Path, Path]:
        output_dir = job_dir / "separated"
        output_dir.mkdir(exist_ok=True, parents=True)

        try:
            self._validate_audio_file(audio_file)
            with self.separator_provider.separator_lock:
                models_path = Path(self.separator_provider.models_dir)
                models_path.mkdir(parents=True, exist_ok=True)

                temp_separator = Separator(
                    output_dir=str(output_dir),
                    output_format="WAV",
                    normalization_threshold=0.9,
                    amplification_threshold=0.9,
                    mdx_params=mdx_params,
                    model_file_dir=self.separator_provider.models_dir,
                )

                temp_separator.load_model(model_filename=MODEL_FILENAME)

                logger.debug(
                    "Separator configuration",
                    track_id=track_id,
                    output_dir=output_dir,
                    model=MODEL_FILENAME,
                    models_dir=self.separator_provider.models_dir,
                )

                try:
                    output_files = run_with_timeout(
                        temp_separator.separate,
                        str(audio_file),
                        timeout=processing_timeout,
                    )
                    logger.info(
                        "Separation completed", track_id=track_id, output_files=output_files
                    )
                except Exception as sep_error:
                    logger.error(
                        "Separation process failed",
                        track_id=track_id,
                        error=str(sep_error),
                        error_type=type(sep_error).__name__,
                    )
                    raise

        except (RuntimeError, OSError, TimeoutError, ValueError, FileNotFoundError) as e:
            logger.error(
                "Failed to process file",
                track_id=track_id,
                audio_file_path=str(audio_file),
                error=str(e),
                error_type=type(e).__name__,
            )
            raise RuntimeError(f"Audio separation failed: {str(e)}") from e

        return self._validate_separated_files(output_dir, track_id)

    def separate_audio(self, audio_file: Path) -> Tuple[Path, Path]:
        output_dir = audio_file.parent / "separated"
        output_dir.mkdir(exist_ok=True, parents=True)

        try:
            self._validate_audio_file(audio_file)
            models_path = Path(self.separator_provider.models_dir)
            models_path.mkdir(parents=True, exist_ok=True)

            separator = Separator(
                output_dir=str(output_dir),
                output_format="WAV",
                normalization_threshold=0.9,
                amplification_threshold=0.9,
                mdx_params=mdx_params,
                model_file_dir=self.separator_provider.models_dir,
            )

            separator.load_model(model_filename=MODEL_FILENAME)
            separator.separate(str(audio_file))

            return self.storage.detect_separated_files(output_dir)

        except Exception as e:
            raise RuntimeError(f"Audio separation failed: {str(e)}") from e
