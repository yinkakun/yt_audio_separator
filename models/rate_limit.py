from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitInfo:
    limit: int
    remaining: int
    reset_time: int
    retry_after: int
