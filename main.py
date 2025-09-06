import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yt_dlp
from audio_separator.separator import Separator
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

limiter = Limiter(
    app=app, key_func=get_remote_address, default_limits=["100 per hour", "10 per minute"]
)

OUTPUT_DIR = Path("audio_separator_output")
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_TITLE_LENGTH = 100
MAX_ARTIST_LENGTH = 100
MAX_SEARCH_QUERY_LENGTH = 200
MAX_FILE_SIZE_MB = 50
PROCESSING_TIMEOUT = 300  # 5 minutes


@dataclass
class ProcessingJob:
    track_id: str
    status: str  # 'processing', 'completed', 'failed'
    progress: int  # 0-100
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: float = 0
    completed_at: Optional[float] = None


executor = ThreadPoolExecutor(max_workers=3)

job_status: Dict[str, ProcessingJob] = {}
job_lock = threading.Lock()


def sanitize_input(text: str, max_length: int) -> str:
    """Sanitize and validate input text"""
    if not text or not isinstance(text, str):
        raise ValueError("Input must be a non-empty string")

    sanitized = re.sub(r'[<>:"/\|?*\x00-\x1f]', "", text.strip())

    if len(sanitized) > max_length:
        raise ValueError(f"Input too long. Maximum {max_length} characters")

    if not sanitized:
        raise ValueError("Input cannot be empty after sanitization")

    return sanitized


def update_job_status(
    track_id: str, status: str, progress: int = 0, error: str = None, result: Dict[str, Any] = None
):
    with job_lock:
        if track_id not in job_status:
            job_status[track_id] = ProcessingJob(
                track_id=track_id, status="processing", progress=0, created_at=time.time()
            )

        job = job_status[track_id]
        job.status = status
        job.progress = progress

        if error:
            job.error = error
        if result:
            job.result = result
        if status in ["completed", "failed"]:
            job.completed_at = time.time()


def cleanup_old_jobs():
    """Clean up jobs older than 1 hour"""
    current_time = time.time()
    with job_lock:
        expired_jobs = [
            track_id for track_id, job in job_status.items() if current_time - job.created_at > 3600
        ]
        for track_id in expired_jobs:
            del job_status[track_id]
            cleanup_track_files(track_id)


def cleanup_track_files(track_id: str):
    """Clean up files for a specific track"""
    track_dir = OUTPUT_DIR / track_id
    if track_dir.exists():
        shutil.rmtree(track_dir, ignore_errors=True)


def error_response(message: str, status_code: int = 400):
    """Standardized error response"""
    return jsonify({"error": message}), status_code


def get_track_directory(track_id: str) -> Path:
    return OUTPUT_DIR / track_id


VALID_TRACK_TYPES = {"vocals", "instrumental"}


def validate_track_type(track_type: str) -> str:
    """Validate track type"""
    if track_type not in VALID_TRACK_TYPES:
        raise ValueError(f"Invalid track type. Use: {', '.join(VALID_TRACK_TYPES)}")
    return track_type


@app.route("/")
def read_root():
    return {"service": "audio_separator", "status": "healthy"}


@app.route("/separate-audio", methods=["POST"])
@limiter.limit("5 per minute")
def separate_audio():
    """Start audio separation job - returns immediately with job ID"""
    try:
        if request.content_length and request.content_length > 1024:  # 1KB max
            return error_response("Request too large", 413)

        data = request.get_json()
        if not data or "title" not in data or "artist" not in data:
            return error_response("Missing title or artist")

        title = sanitize_input(data["title"], MAX_TITLE_LENGTH)
        artist = sanitize_input(data["artist"], MAX_ARTIST_LENGTH)

        search_query = f"{artist} - {title}"
        if len(search_query) > MAX_SEARCH_QUERY_LENGTH:
            return error_response("Combined search query too long")

        active_jobs = len([j for j in job_status.values() if j.status == "processing"])
        if active_jobs >= 3:
            return error_response("Server busy. Please try again later.", 429)

        track_id = str(uuid.uuid4())

        update_job_status(track_id, "processing", 0)

        executor.submit(process_audio_background, track_id, search_query)

        logger.info("Started processing job: %s for %s - %s", track_id, artist, title)

        return (
            jsonify(
                {
                    "track_id": track_id,
                    "status": "processing",
                    "message": "Audio separation started",
                    "status_url": f"/jobs/{track_id}",
                }
            ),
            202,
        )

    except ValueError as e:
        logger.warning("Validation error in separate_audio: %s", str(e))
        return error_response(str(e))
    except (RuntimeError, OSError, KeyError) as e:
        logger.error("Error starting audio separation: %s", str(e))
        return error_response("Internal server error", 500)


def process_audio_background(track_id: str, search_query: str):
    """Background audio processing function"""
    temp_dir = None
    output_track_dir = OUTPUT_DIR / track_id

    try:
        output_track_dir.mkdir(exist_ok=True)
        temp_dir = tempfile.mkdtemp()

        logger.info("Processing %s: Downloading audio", track_id)
        update_job_status(track_id, "processing", 10)

        downloaded_file, original_title = download_audio(temp_dir, search_query)
        if not downloaded_file:
            raise RuntimeError("Failed to download audio")

        update_job_status(track_id, "processing", 40)
        logger.info("Processing %s: Separating audio", track_id)

        vocals_file, instrumental_file = separate_audio_tracks(temp_dir, downloaded_file, track_id)

        update_job_status(track_id, "processing", 80)
        logger.info("Processing %s: Saving results", track_id)

        vocals_dest = output_track_dir / "vocals.wav"
        instrumental_dest = output_track_dir / "instrumental.wav"

        shutil.move(vocals_file, vocals_dest)
        shutil.move(instrumental_file, instrumental_dest)

        result = {
            "message": "audio_separator tracks created successfully",
            "original_title": original_title,
            "vocals_url": f"/download/{track_id}/vocals",
            "instrumental_url": f"/download/{track_id}/instrumental",
        }

        update_job_status(track_id, "completed", 100, result=result)
        logger.info("Completed processing job: %s", track_id)

    except (RuntimeError, OSError, FileNotFoundError, ValueError) as e:
        logger.error("Error processing %s: %s", track_id, str(e))
        update_job_status(track_id, "failed", 0, error=str(e))

        cleanup_track_files(track_id)

    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def download_audio(temp_dir: str, search_query: str) -> tuple[Optional[str], Optional[str]]:
    """Download audio from YouTube"""
    ydl_opts = {
        "format": "bestaudio/best",
        "extractaudio": True,
        "audioformat": "wav",
        "outtmpl": os.path.join(temp_dir, "original.%(ext)s"),
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

            for file in os.listdir(temp_dir):
                if file.startswith("original."):
                    downloaded_file = os.path.join(temp_dir, file)

                    file_size_mb = os.path.getsize(downloaded_file) / (1024 * 1024)
                    if file_size_mb > MAX_FILE_SIZE_MB:
                        raise ValueError(f"File too large: {file_size_mb:.1f}MB")

                    original_title = search_results["entries"][0]["title"]
                    return downloaded_file, original_title

            raise RuntimeError("Downloaded file not found")

    except Exception as e:
        raise RuntimeError(f"Download failed: {str(e)}") from e


def separate_audio_tracks(temp_dir: str, audio_file: str, track_id: str = None) -> tuple[str, str]:
    """Separate audio into vocals and instrumental using audio-separator"""

    output_dir = os.path.join(temp_dir, "separated")
    os.makedirs(output_dir, exist_ok=True)

    try:
        separator = Separator(
            output_dir=output_dir,
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
        if track_id:
            update_job_status(track_id, "processing", 45)

        separator.load_model(model_filename="UVR-MDX-NET-Voc_FT.onnx")
        if track_id:
            update_job_status(track_id, "processing", 50)

        separator.separate(audio_file)

        if track_id:
            update_job_status(track_id, "processing", 70)

    except Exception as e:
        raise RuntimeError(f"Audio separation failed: {str(e)}") from e
    vocals_file = None
    instrumental_file = None
    for file in os.listdir(output_dir):
        file_path = os.path.join(output_dir, file)
        if os.path.isfile(file_path):
            if "vocals" in file.lower() or "(Vocals)" in file:
                vocals_file = file_path
            elif (
                "instrumental" in file.lower()
                or "(Instrumental)" in file
                or "no_vocals" in file.lower()
            ):
                instrumental_file = file_path
    if not vocals_file or not instrumental_file:
        available_files = os.listdir(output_dir) if os.path.exists(output_dir) else []
        raise FileNotFoundError(f"Could not find separated tracks. Found files: {available_files}")
    return vocals_file, instrumental_file


@app.before_request
def before_request():
    """Clean up old jobs before processing requests"""
    cleanup_old_jobs()


@app.teardown_appcontext
def cleanup(error):
    """Clean up resources"""
    if error:
        logger.error("Request error: %s", error)


if __name__ == "__main__":
    logger.info("Starting audio_separator service")
    app.run(debug=False, host="0.0.0.0", port=5500)
