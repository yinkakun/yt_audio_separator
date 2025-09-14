from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config.logger import get_logger

logger = get_logger(__name__)


class AudioType(Enum):
    VOCALS = "vocals"
    INSTRUMENTAL = "instrumental"


class AudioClassifier:
    TRACK_PATTERNS: Dict[AudioType, List[str]] = {
        AudioType.VOCALS: ["vocals", "(Vocals)"],
        AudioType.INSTRUMENTAL: ["instrumental", "(Instrumental)", "no_vocals"],
    }

    @classmethod
    def detect_separated_files(cls, output_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
        """Detect vocals and instrumental files in the output directory."""
        vocals_file = None
        instrumental_file = None

        all_files = list(output_dir.iterdir())

        for file_path in all_files:
            if not file_path.is_file():
                continue

            filename = file_path.name.lower()

            for track_type, patterns in cls.TRACK_PATTERNS.items():
                matching_patterns = [pattern for pattern in patterns if pattern.lower() in filename]
                if not matching_patterns:
                    continue

                if track_type == AudioType.VOCALS:
                    vocals_file = file_path
                else:
                    instrumental_file = file_path
                break

        return vocals_file, instrumental_file

    @classmethod
    def get_track_filename(cls, track_id: str, track_type: AudioType) -> str:
        return f"{track_id}/{track_type.value}.mp3"
