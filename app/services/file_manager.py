import logging
from pathlib import Path
from typing import Optional, Tuple

from botocore.exceptions import ClientError, NoCredentialsError

from app.models.job import TrackType

logger = logging.getLogger(__name__)


class FileManager:
    TRACK_PATTERNS = {
        TrackType.VOCALS: ["vocals", "(Vocals)"],
        TrackType.INSTRUMENTAL: ["instrumental", "(Instrumental)", "no_vocals"],
    }

    @staticmethod
    def cleanup_track_files(track_id: str, r2_client=None):
        if not r2_client:
            return

        try:
            for track_type in TrackType:
                r2_key = f"{track_id}/{track_type.value}.mp3"
                try:
                    if r2_client.delete_file(r2_key):
                        logger.debug("Cleaned up R2 file: %s", r2_key)
                except (ClientError, NoCredentialsError, OSError) as e:
                    logger.warning("Failed to clean up R2 file %s: %s", r2_key, e)
        except (TypeError, ValueError, AttributeError) as e:
            logger.error("Unexpected error during cleanup for %s: %s", track_id, e)

    @staticmethod
    def detect_separated_files(output_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
        vocals_file = None
        instrumental_file = None

        for file_path in output_dir.iterdir():
            if not file_path.is_file():
                continue

            filename = file_path.name.lower()
            for track_type, patterns in FileManager.TRACK_PATTERNS.items():
                if not any(pattern.lower() in filename for pattern in patterns):
                    continue

                if track_type == TrackType.VOCALS:
                    vocals_file = file_path
                else:
                    instrumental_file = file_path
                break

        return vocals_file, instrumental_file

    @staticmethod
    def upload_to_r2(track_id: str, vocals_file: Path, instrumental_file: Path, r2_client) -> bool:
        try:
            vocals_key = f"{track_id}/vocals.mp3"
            instrumental_key = f"{track_id}/instrumental.mp3"

            vocals_uploaded = r2_client.upload_file(vocals_file, vocals_key)
            instrumental_uploaded = r2_client.upload_file(instrumental_file, instrumental_key)

            return vocals_uploaded and instrumental_uploaded
        except (OSError, ClientError, NoCredentialsError) as e:
            logger.error("Failed to upload to R2: %s", e)
            return False

    @staticmethod
    async def upload_to_r2_async(
        track_id: str, vocals_file: Path, instrumental_file: Path, r2_client
    ) -> bool:
        try:
            vocals_key = f"{track_id}/vocals.mp3"
            instrumental_key = f"{track_id}/instrumental.mp3"

            file_uploads = [(vocals_file, vocals_key), (instrumental_file, instrumental_key)]

            upload_results = await r2_client.upload_files_parallel(file_uploads)
            return all(upload_results)

        except (OSError, ClientError, NoCredentialsError) as e:
            logger.error("Failed to upload to R2 (async): %s", e)
            return False
