import json
import time
from typing import Any, Dict, Optional

from cloudflare import Cloudflare
from fastapi import HTTPException, Request

from config.logging_config import get_logger

logger = get_logger(__name__)


def parse_rate_limit(rate_limit: str) -> tuple[int, int]:
    """
    Parse rate limit string into requests and window seconds.

    Args:
        rate_limit: Rate limit string (e.g., "100 per hour", "5 per minute")

    Returns:
        tuple: (limit_requests, window_seconds)

    Raises:
        ValueError: If rate limit format is invalid
    """
    parts = rate_limit.lower().split()
    if len(parts) != 3 or parts[1] != "per":
        raise ValueError(f"Invalid rate limit format: {rate_limit}")

    limit_requests = int(parts[0])
    time_unit = parts[2]

    window_seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}.get(
        time_unit.rstrip("s"), 60
    )

    return limit_requests, window_seconds


class CloudflareKVLimiter:
    def __init__(self, account_id: str, namespace_id: str, api_token: str):
        self.account_id = account_id
        self.namespace_id = namespace_id
        self.client = Cloudflare(api_token=api_token)

    async def close(self) -> None:
        # The Cloudflare client handles its own cleanup
        pass

    async def get_key(self, key: str) -> Optional[dict]:
        try:
            response = self.client.kv.namespaces.values.get(
                namespace_id=self.namespace_id, key_name=key, account_id=self.account_id
            )
            content = response.read()
            return json.loads(content)
        except Exception as e:
            # Handle 404 and other errors
            if "404" in str(e) or "not found" in str(e).lower():
                return None
            logger.warning(f"KV get failed for key {key}: {e}")
            return None

    async def put_key(self, key: str, value: Dict[str, Any], ttl: Optional[int] = None) -> bool:
        try:
            if ttl:
                self.client.kv.namespaces.values.update(
                    key_name=key,
                    account_id=self.account_id,
                    namespace_id=self.namespace_id,
                    metadata="",
                    value=json.dumps(value),
                    expiration_ttl=int(ttl),
                )
            else:
                self.client.kv.namespaces.values.update(
                    key_name=key,
                    account_id=self.account_id,
                    namespace_id=self.namespace_id,
                    metadata="",
                    value=json.dumps(value),
                )
            return True
        except Exception as e:
            logger.warning(f"KV put failed for key {key}: {e}")
            return False

    async def is_allowed(
        self, identifier: str, limit_requests: int, window_seconds: int
    ) -> tuple[bool, dict]:
        """
        Check if request is allowed based on rate limit
        Returns (is_allowed, info_dict)
        """
        current_time = int(time.time())
        key = f"rate_limit:{identifier}:{current_time // window_seconds}"

        data = await self.get_key(key)
        if data is None:
            data = {"count": 0, "reset_time": current_time + window_seconds}

        count = data.get("count", 0)
        reset_time = data.get("reset_time", current_time + window_seconds)

        if count >= limit_requests:
            return False, {
                "limit": limit_requests,
                "remaining": 0,
                "reset_time": reset_time,
                "retry_after": reset_time - current_time,
            }

        new_count = count + 1
        await self.put_key(
            key, {"count": new_count, "reset_time": reset_time}, ttl=window_seconds + 60
        )  # Extra buffer time

        return True, {
            "limit": limit_requests,
            "remaining": max(0, limit_requests - new_count),
            "reset_time": reset_time,
            "retry_after": 0,
        }

    def limit(self, rate_limit: str):
        """Decorator for rate limiting endpoints"""

        def decorator(func):
            async def wrapper(request: Request, *args, **kwargs):
                try:
                    limit_requests, window_seconds = parse_rate_limit(rate_limit)
                except ValueError as e:
                    logger.warning(str(e))
                    return await func(request, *args, **kwargs)

                client_ip = request.client.host if request.client else "unknown"

                allowed, info = await self.is_allowed(client_ip, limit_requests, window_seconds)

                if not allowed:
                    logger.warning(f"Rate limit exceeded for {client_ip}: {rate_limit}")
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "error": "Rate limit exceeded",
                            "limit": info["limit"],
                            "remaining": info["remaining"],
                            "reset_time": info["reset_time"],
                            "retry_after": info["retry_after"],
                        },
                        headers={"Retry-After": str(info["retry_after"])},
                    )

                response = await func(request, *args, **kwargs)
                if hasattr(response, "headers"):
                    response.headers["X-RateLimit-Limit"] = str(info["limit"])
                    response.headers["X-RateLimit-Remaining"] = str(info["remaining"])
                    response.headers["X-RateLimit-Reset"] = str(info["reset_time"])
                return response

            return wrapper

        return decorator
