"""Custom exceptions for better error handling patterns"""


class AudioSeparationError(Exception):
    """Base exception for audio separation errors"""


class ValidationError(AudioSeparationError):
    """Exception for input validation errors"""


class ProcessingError(AudioSeparationError):
    """Exception for audio processing errors"""


class StorageError(AudioSeparationError):
    """Exception for storage-related errors"""


class CacheError(AudioSeparationError):
    """Exception for cache-related errors"""


class ServiceUnavailableError(AudioSeparationError):
    """Exception for service unavailability"""


class RateLimitExceededError(AudioSeparationError):
    """Exception for rate limit exceeded"""

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after
