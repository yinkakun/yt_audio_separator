from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class TrackType(Enum):
    VOCALS = "vocals"
    INSTRUMENTAL = "instrumental"


class JobStatus(Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ProcessingJob:
    track_id: str
    status: JobStatus
    progress: int = 0
    created_at: float = 0
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    completed_at: Optional[float] = None
