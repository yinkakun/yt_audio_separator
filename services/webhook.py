import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Dict

import aiohttp
import requests

logger = logging.getLogger(__name__)


class WebhookManager:
    def __init__(self, webhook_url: str, webhook_secret: str, max_retries: int = 3):
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        self.enabled = bool(webhook_url)
        self.max_retries = max_retries

    def create_signature(self, payload: str) -> str:
        if not self.webhook_secret:
            return ""

        signature = hmac.new(
            self.webhook_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"

    def send_webhook(self, event_type: str, data: Dict[str, Any]) -> None:
        if not self.enabled:
            return

        payload = {"event": event_type, "timestamp": time.time(), "data": data}
        threading.Thread(
            target=self._send_webhook_with_retry, args=(event_type, payload), daemon=True
        ).start()

    def _send_webhook_with_retry(
        self, event_type: str, payload: Dict[str, Any], max_retries: int = 3, timeout: int = 30
    ) -> bool:
        payload_str = json.dumps(payload, sort_keys=True)
        signature = self.create_signature(payload_str)

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
            "User-Agent": "AudioSeparator/1.0",
        }

        for attempt in range(max_retries):
            try:
                response = requests.post(
                    self.webhook_url,
                    data=payload_str,
                    headers=headers,
                    timeout=timeout,
                )

                if 200 <= response.status_code < 300:
                    logger.info("Webhook sent successfully: %s", event_type)
                    return True

                logger.warning(
                    "Webhook failed with status %d: %s", response.status_code, response.text
                )

            except requests.exceptions.RequestException as e:
                logger.error("Webhook attempt %d failed: %s", attempt + 1, str(e))

            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))

        logger.error("Webhook failed after %d attempts", max_retries)
        return False

    def notify_job_completed(self, track_id: str, result: Dict[str, Any]):
        self.send_webhook(
            "job.completed", {"track_id": track_id, "status": "completed", "result": result}
        )

    def notify_job_failed(self, track_id: str, error: str):
        self.send_webhook("job.failed", {"track_id": track_id, "status": "failed", "error": error})

    async def send_webhook_async(self, event_type: str, data: Dict[str, Any]) -> None:
        if not self.enabled:
            return

        payload = {"event": event_type, "timestamp": time.time(), "data": data}
        asyncio.create_task(self._send_webhook_with_retry_async(event_type, payload))

    async def _send_webhook_with_retry_async(
        self, event_type: str, payload: Dict[str, Any], timeout: int = 30
    ) -> bool:
        payload_str = json.dumps(payload, sort_keys=True)
        signature = self.create_signature(payload_str)

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
            "User-Agent": "AudioSeparator/1.0",
        }

        timeout_config = aiohttp.ClientTimeout(total=timeout)

        for attempt in range(self.max_retries):
            try:
                async with aiohttp.ClientSession(timeout=timeout_config) as session:
                    async with session.post(
                        self.webhook_url,
                        data=payload_str,
                        headers=headers,
                    ) as response:
                        if 200 <= response.status < 300:
                            logger.info("Webhook sent successfully (async): %s", event_type)
                            return True

                        response_text = await response.text()
                        logger.warning(
                            "Webhook failed with status %d: %s", response.status, response_text
                        )

            except aiohttp.ClientError as e:
                logger.error("Webhook attempt %d failed (async): %s", attempt + 1, str(e))

            if attempt < self.max_retries - 1:
                await asyncio.sleep(5 * (attempt + 1))

        logger.error("Webhook failed after %d attempts (async)", self.max_retries)
        return False

    async def notify_job_completed_async(self, track_id: str, result: Dict[str, Any]):
        await self.send_webhook_async(
            "job.completed", {"track_id": track_id, "status": "completed", "result": result}
        )

    async def notify_job_failed_async(self, track_id: str, error: str):
        await self.send_webhook_async(
            "job.failed", {"track_id": track_id, "status": "failed", "error": error}
        )
