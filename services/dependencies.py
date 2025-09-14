from typing import Optional

from fastapi import Depends

from services.audio_cache import AudioCache
from utils.rate_limiter import RateLimiter
from utils.redis_cache import RedisCache
from workers.job_queue import JobQueue


class Services:
    def __init__(self):
        self.rate_limiter: Optional[RateLimiter] = None
        self.redis_cache: Optional[RedisCache] = None
        self.cache_manager: Optional[AudioCache] = None
        self.queue_manager: Optional[JobQueue] = None

    async def close(self):
        """Close all services"""
        if self.queue_manager:
            self.queue_manager.close()
        if self.rate_limiter:
            await self.rate_limiter.close()
        if self.redis_cache:
            await self.redis_cache.close()


services = Services()


RateLimiterDep = Depends(lambda: services.rate_limiter)
CacheManagerDep = Depends(lambda: services.cache_manager)
QueueManagerDep = Depends(lambda: services.queue_manager)
