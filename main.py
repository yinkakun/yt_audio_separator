import hashlib
import logging
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yt_dlp
from audio_separator.separator import Separator
from flask import Flask, jsonify, request, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


class TrackType(Enum):
    VOCALS = "vocals"
    INSTRUMENTAL = "instrumental"


class JobStatus(Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Config:
    OUTPUT_DIR: Path = Path("separated_tracks")
    MAX_TITLE_LENGTH: int = 100
    MAX_ARTIST_LENGTH: int = 100
    MAX_SEARCH_QUERY_LENGTH: int = 200
    MAX_FILE_SIZE_MB: int = 50
    PROCESSING_TIMEOUT: int = 300
    CACHE_EXPIRY_HOURS: int = 24
    MAX_WORKERS: int = 2
    MAX_ACTIVE_JOBS: int = 2


config = Config()

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

try:
    config.OUTPUT_DIR.mkdir(exist_ok=True, parents=True)
    logger.info("Output directory created/verified: %s", config.OUTPUT_DIR)
except OSError as e:
    logger.error("Failed to create output directory %s: %s", config.OUTPUT_DIR, e)
    raise
limiter = Limiter(
    app=app, key_func=get_remote_address, default_limits=["100 per hour", "10 per minute"]
)


@dataclass
class ProcessingJob:
    track_id: str
    status: JobStatus
    progress: int = 0
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: float = 0
    completed_at: Optional[float] = None


def error_handler(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ValueError as e:
            logger.warning("Validation error in %s: %s", func.__name__, str(e))
            return jsonify({"error": str(e)}), 400
        except (RuntimeError, OSError, KeyError) as e:
            logger.error("Error in %s: %s", func.__name__, str(e))
            return jsonify({"error": "Internal server error"}), 500

    return wrapper


executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)


class TaskManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, ProcessingJob] = {}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._separator: Optional[Separator] = None

    def get_separator(self) -> Separator:
        with self._lock:
            if self._separator is None:
                logger.info("Initializing separator model")
                self._separator = Separator(
                    output_format="wav",
                    normalization_threshold=0.9,
                    amplification_threshold=0.9,
                    mdx_params={
                        "hop_length": 1024,
                        "segment_size": 256,
                        "overlap": 0.5,
                        "batch_size": 2,
                        "enable_denoise": True,
                    },
                )
                self._separator.load_model(model_filename="UVR-MDX-NET-Inst_HQ_3.onnx")
                logger.info("Separator model initialized")
            return self._separator

    def update_job(
        self,
        track_id: str,
        status: JobStatus,
        *,
        progress: int = 0,
        error: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
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

    def get_cached_result(self, query: str) -> Optional[Dict[str, Any]]:
        cache_key = hashlib.md5(query.encode()).hexdigest()
        with self._lock:
            if cache_key in self._cache:
                cached_data = self._cache[cache_key]
                if (
                    time.time() - cached_data.get("created_at", 0)
                    < config.CACHE_EXPIRY_HOURS * 3600
                ):
                    return cached_data["result"]
                del self._cache[cache_key]
            return None

    def cache_result(self, query: str, result: Dict[str, Any], track_id: str):
        cache_key = hashlib.md5(query.encode()).hexdigest()
        with self._lock:
            self._cache[cache_key] = {
                "result": result,
                "track_id": track_id,
                "created_at": time.time(),
            }

    def count_active_jobs(self) -> int:
        with self._lock:
            return len([j for j in self._jobs.values() if j.status == JobStatus.PROCESSING])

    def cleanup_expired(self):
        current_time = time.time()
        with self._lock:
            expired_jobs = [
                tid for tid, job in self._jobs.items() if current_time - job.created_at > 3600
            ]
            for track_id in expired_jobs:
                del self._jobs[track_id]
                FileManager.cleanup_track_files(track_id)

            expired_cache = [
                key
                for key, data in self._cache.items()
                if current_time - data.get("created_at", 0) > config.CACHE_EXPIRY_HOURS * 3600
            ]
            for cache_key in expired_cache:
                cached_data = self._cache[cache_key]
                if "track_id" in cached_data:
                    FileManager.cleanup_track_files(cached_data["track_id"])
                del self._cache[cache_key]


task_manager = TaskManager()


class FileManager:
    TRACK_PATTERNS = {
        TrackType.VOCALS: ["vocals", "(Vocals)"],
        TrackType.INSTRUMENTAL: ["instrumental", "(Instrumental)", "no_vocals"],
    }

    @staticmethod
    def get_track_directory(track_id: str) -> Path:
        return config.OUTPUT_DIR / track_id

    @staticmethod
    def cleanup_track_files(track_id: str):
        track_dir = FileManager.get_track_directory(track_id)
        if track_dir.exists():
            shutil.rmtree(track_dir, ignore_errors=True)

    @staticmethod
    def copy_cached_files(source_track_id: str, dest_track_id: str) -> bool:
        try:
            source_dir = FileManager.get_track_directory(source_track_id)
            dest_dir = FileManager.get_track_directory(dest_track_id)

            if not source_dir.exists():
                return False

            dest_dir.mkdir(exist_ok=True, parents=True)
            for track_type in TrackType:
                src_file = source_dir / f"{track_type.value}.wav"
                if src_file.exists():
                    shutil.copy2(src_file, dest_dir / f"{track_type.value}.wav")
                else:
                    return False
            return True
        except (OSError, shutil.Error) as e:
            logger.error("Failed to copy cached files: %s", str(e))
            return False

    @staticmethod
    def detect_separated_files(output_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
        vocals_file = None
        instrumental_file = None

        for file_path in output_dir.iterdir():
            if file_path.is_file():
                filename = file_path.name.lower()
                for track_type, patterns in FileManager.TRACK_PATTERNS.items():
                    if any(pattern.lower() in filename for pattern in patterns):
                        if track_type == TrackType.VOCALS:
                            vocals_file = file_path
                        else:
                            instrumental_file = file_path
                        break

        return vocals_file, instrumental_file


def sanitize_input(text: str, max_length: int) -> str:
    if not text:
        raise ValueError("Input must be a non-empty string")

    sanitized = re.sub(r'[<>:"/\|?*\x00-\x1f]', "", text.strip())

    if len(sanitized) > max_length:
        raise ValueError(f"Input too long. Maximum {max_length} characters")
    if not sanitized:
        raise ValueError("Input cannot be empty after sanitization")

    return sanitized


def build_response(
    track_id: str,
    status: JobStatus,
    message: str,
    result: Optional[Dict[str, Any]] = None,
    status_code: int = 200,
):
    response: Dict[str, Any] = {"track_id": track_id, "status": status.value, "message": message}
    if result:
        response["result"] = result
    if status == JobStatus.PROCESSING:
        response["status_url"] = f"/jobs/{track_id}"

    return jsonify(response), status_code


def validate_track_type(track_type: str) -> TrackType:
    try:
        return TrackType(track_type)
    except ValueError as exc:
        valid_types = ", ".join([t.value for t in TrackType])
        raise ValueError(f"Invalid track type. Use: {valid_types}") from exc


@app.route("/")
def read_root():
    return {"service": "audio_separator", "status": "healthy"}


@app.route("/separate-audio", methods=["POST"])
@limiter.limit("5 per minute")
@error_handler
def separate_audio():
    """Start audio separation job - returns immediately with job ID"""
    if request.content_length and request.content_length > 1024:
        return jsonify({"error": "Request too large"}), 413

    data = request.get_json()
    if not data or "title" not in data or "artist" not in data:
        return jsonify({"error": "Missing title or artist"}), 400

    title = sanitize_input(data["title"], config.MAX_TITLE_LENGTH)
    artist = sanitize_input(data["artist"], config.MAX_ARTIST_LENGTH)

    search_query = f"{artist} - {title}"
    if len(search_query) > config.MAX_SEARCH_QUERY_LENGTH:
        return jsonify({"error": "Combined search query too long"}), 400

    cached_result = task_manager.get_cached_result(search_query)
    if cached_result:
        track_id = str(uuid.uuid4())
        cached_track_id = cached_result.get("track_id_source")
        if cached_track_id and FileManager.copy_cached_files(cached_track_id, track_id):
            result = cached_result.copy()
            result["vocals_url"] = f"/download/{track_id}/vocals"
            result["instrumental_url"] = f"/download/{track_id}/instrumental"

            task_manager.update_job(track_id, JobStatus.COMPLETED, progress=100, result=result)
            logger.info("Returned cached result for: %s - %s", artist, title)

            return build_response(
                track_id, JobStatus.COMPLETED, "Audio separation completed (cached)", result
            )

    if task_manager.count_active_jobs() >= config.MAX_ACTIVE_JOBS:
        return jsonify({"error": "Server busy. Please try again later."}), 429

    track_id = str(uuid.uuid4())
    task_manager.update_job(track_id, JobStatus.PROCESSING, progress=0)
    executor.submit(process_audio_background, track_id, search_query)

    logger.info("Started processing job: %s for %s - %s", track_id, artist, title)
    return build_response(track_id, JobStatus.PROCESSING, "Audio separation started", None, 202)


def process_audio_background(track_id: str, search_query: str):
    """Background audio processing function"""
    temp_dir = None
    output_track_dir = FileManager.get_track_directory(track_id)

    try:
        output_track_dir.mkdir(exist_ok=True, parents=True)
        temp_dir = Path(tempfile.mkdtemp())

        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=10)
        logger.info("Processing %s: Downloading audio", track_id)
        downloaded_file, original_title = download_audio(temp_dir, search_query)

        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=40)
        logger.info("Processing %s: Separating audio", track_id)
        vocals_file, instrumental_file = separate_audio_tracks(temp_dir, downloaded_file, track_id)

        task_manager.update_job(track_id, JobStatus.PROCESSING, progress=80)
        logger.info("Processing %s: Saving results", track_id)
        save_separated_tracks(vocals_file, instrumental_file, output_track_dir)

        result = create_result_dict(track_id, original_title)
        task_manager.update_job(track_id, JobStatus.COMPLETED, progress=100, result=result)
        task_manager.cache_result(search_query, result, track_id)

        logger.info("Completed processing job: %s", track_id)

    except (RuntimeError, OSError, FileNotFoundError, ValueError) as e:
        logger.error("Error processing %s: %s", track_id, str(e))
        task_manager.update_job(track_id, JobStatus.FAILED, progress=0, error=str(e))
        FileManager.cleanup_track_files(track_id)

    finally:
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def save_separated_tracks(vocals_file: Path, instrumental_file: Path, output_dir: Path):
    """Save separated audio tracks to output directory"""
    shutil.move(str(vocals_file), str(output_dir / "vocals.wav"))
    shutil.move(str(instrumental_file), str(output_dir / "instrumental.wav"))


def create_result_dict(track_id: str, original_title: Optional[str]) -> Dict[str, str]:
    """Create standardized result dictionary"""
    return {
        "message": "audio_separator tracks created successfully",
        "original_title": original_title or "Unknown Title",
        "vocals_url": f"/download/{track_id}/vocals",
        "instrumental_url": f"/download/{track_id}/instrumental",
        "track_id_source": track_id,
    }


def download_audio(temp_dir: Path, search_query: str) -> Tuple[Path, Optional[str]]:
    """Download audio from YouTube"""
    ydl_opts = {
        "format": "bestaudio/best",
        "extractaudio": True,
        "audioformat": "wav",
        "outtmpl": str(temp_dir / "original.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }

    try:
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
                    if file_size_mb > config.MAX_FILE_SIZE_MB:
                        raise ValueError(f"File too large: {str(file_size_mb)}" % file_size_mb)

                    first_entry = search_results["entries"][0]
                    title_value = (
                        first_entry.get("title") if isinstance(first_entry, dict) else None
                    )
                    original_title = (
                        str(title_value) if isinstance(title_value, str) else "Unknown Title"
                    )

                    return file_path, original_title

            raise RuntimeError("Downloaded file not found")

    except Exception as e:
        raise RuntimeError(f"Download failed: {str(e)}") from e


def separate_audio_tracks(
    temp_dir: Path, audio_file: Path, track_id: Optional[str] = None
) -> Tuple[Path, Path]:
    """Separate audio into vocals and instrumental using cached separator"""
    output_dir = temp_dir / "separated"
    output_dir.mkdir(exist_ok=True, parents=True)

    try:
        separator = Separator(
            output_dir=str(output_dir),
            output_format="wav",
            normalization_threshold=0.9,
            amplification_threshold=0.9,
            mdx_params={
                "hop_length": 1024,
                "segment_size": 512,
                "overlap": 0.25,
                "batch_size": 4,
                "enable_denoise": True,
            },
        )
        separator.load_model(model_filename="UVR-MDX-NET-Voc_FT.onnx")

        if track_id:
            task_manager.update_job(track_id, JobStatus.PROCESSING, progress=50)

        separator.separate(str(audio_file))

        if track_id:
            task_manager.update_job(track_id, JobStatus.PROCESSING, progress=70)

    except Exception as e:
        raise RuntimeError(f"Audio separation failed: {str(e)}") from e

    vocals_file, instrumental_file = FileManager.detect_separated_files(output_dir)

    if not vocals_file or not instrumental_file:
        available_files = [f.name for f in output_dir.iterdir() if f.is_file()]
        raise FileNotFoundError(f"Could not find separated tracks. Found files: {available_files}")

    return vocals_file, instrumental_file


@app.route("/jobs/<track_id>", methods=["GET"])
def get_job_status(track_id: str):
    """Get job status"""
    job = task_manager.get_job(track_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    response = {"track_id": track_id, "status": job.status.value, "progress": job.progress}

    if job.error:
        response["error"] = job.error
    if job.result:
        response["result"] = job.result

    return jsonify(response)


@app.route("/download/<track_id>/<track_type>", methods=["GET"])
def download_track(track_id: str, track_type: str):
    """Download separated track"""
    try:
        track_type_enum = validate_track_type(track_type)
        track_dir = FileManager.get_track_directory(track_id)
        file_path = track_dir / f"{track_type_enum.value}.wav"

        if not file_path.exists():
            return jsonify({"error": "Track not found"}), 404

        return send_file(
            str(file_path),
            as_attachment=True,
            download_name=f"{track_id}_{track_type_enum.value}.wav",
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.before_request
def before_request():
    """Clean up old jobs before processing requests"""
    task_manager.cleanup_expired()


def initialize_separator():
    try:
        task_manager.get_separator()
        logger.info("Pre-loaded separator model successfully")
    except (RuntimeError, OSError, ImportError, ValueError) as e:
        logger.error("Failed to pre-load separator model: %s", str(e))


if __name__ == "__main__":
    logger.info("Starting audio_separator service")
    initialize_separator()
    app.run(debug=False, host="0.0.0.0", port=5500)
