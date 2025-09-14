import hashlib
import hmac
import json
from typing import Any, Dict


def generate_webhook_signature(payload: Dict[str, Any], secret: str) -> str:
    """
    Generate HMAC-SHA256 signature for webhook payload.

    Args:
        payload: Dictionary payload to sign
        secret: Webhook secret key

    Returns:
        str: Signature in format "sha256=<hex_digest>"

    Example:
        payload = {"status": "completed", "track_id": "123"}
        secret = "your_webhook_secret"
        signature = generate_webhook_signature(payload, secret)
        # Returns: "sha256=abc123..."
    """
    if not secret:
        return ""

    payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    signature = hmac.new(
        secret.encode("utf-8"), payload_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"sha256={signature}"
