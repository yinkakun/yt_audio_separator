# YouTube Audio Separator

FastAPI service that downloads YouTube audio and separates it into vocals and instrumental tracks using ML models. Features intelligent caching, distributed rate limiting, and webhook notifications.

## Quick Start

1. **Install dependencies:**

   ```bash
   uv sync
   ```

2. **Set environment variables:**

   ```bash
   # Required
   export CLOUDFLARE_ACCOUNT_ID=your_account_id
   export R2_ACCESS_KEY_ID=your_access_key  
   export R2_SECRET_ACCESS_KEY=your_secret_key
   export R2_PUBLIC_DOMAIN=your_domain
   export API_SECRET_KEY=your_api_secret_key
   
   # Optional - For rate limiting & caching
   export CLOUDFLARE_API_TOKEN=your_api_token
   export CLOUDFLARE_KV_NAMESPACE_ID=your_kv_namespace_id
   
   # Optional - For webhook notifications
   export WEBHOOK_SECRET=your_webhook_secret
   export WEBHOOK_URL=https://your-app.com/webhooks
   ```

3. **Start the server:**

   ```bash
   uv run python main.py
   ```

4. **Process audio:**

   ```bash
   # Start separation job
   curl -X POST http://localhost:5500/separate-audio \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_secret_key" \
     -d '{"search_query": "Song Title Artist"}'
   
   # Check service health
   curl http://localhost:5500/health
   ```

## API Reference

| Endpoint | Method | Description |
|----------|---------|-------------|
| `/separate-audio` | POST | Start audio separation job (async with webhook callback) |
| `/health` | GET | Health check with system metrics and service status |

### POST /separate-audio

Initiates audio separation for a YouTube video. Features intelligent caching to prevent duplicate processing and reduce costs.

**Headers:**

```
Content-Type: application/json
X-API-Key: your_api_secret_key
```

**Request:**

```json
{
  "search_query": "Song Title Artist"
}
```

**Response (202 Accepted):**

```json
{
  "track_id": "uuid",
  "status": "processing",
  "message": "Audio separation started"
}
```

**Response (200 - Cached Result):**

```json
{
  "status": "completed",
  "track_id": "uuid", 
  "result": {
    "vocals_url": "https://domain.com/vocals.mp3",
    "instrumental_url": "https://domain.com/instrumental.mp3",
    "track_id": "uuid"
  },
  "processing_time": 30.5,
  "created_at": 1234567890.0,
  "cached": true
}
```

### GET /health

Returns service health status including storage and rate limiting operational status.

**Response:**

```json
{
  "status": "healthy",
  "timestamp": 1234567890.0,
  "services": {
    "storage_operational": true,
    "storage_configured": true,
    "rate_limiting_operational": true,
    "rate_limiting_configured": true
  }
}
```

## Webhooks (Optional)

Configure `WEBHOOK_URL` environment variable to receive job completion notifications:

**Success Webhook:**

```json
{
  "status": "completed",
  "track_id": "uuid",
  "result": {
    "vocals_url": "https://domain.com/vocals.mp3",
    "instrumental_url": "https://domain.com/instrumental.mp3",
    "track_id": "uuid"
  },
  "progress": 100,
  "created_at": 1234567890.0
}
```

**Failure Webhook:**

```json
{
  "status": "failed",
  "track_id": "uuid", 
  "error": "Error message",
  "progress": 0,
  "created_at": 1234567890.0
}
```

**Headers:**

```
Content-Type: application/json
X-Webhook-Signature: sha256=<hmac_signature>
```

**Signature Verification:**
If `WEBHOOK_SECRET` is configured, webhooks include an HMAC-SHA256 signature for verification:

```python
import hmac
import hashlib
import json

def verify_webhook(payload_str: str, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode('utf-8'),
        payload_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.replace('sha256=', ''))
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `API_SECRET_KEY` | Yes* | - | API authentication key (*required in production) |
| `WEBHOOK_URL` | No | - | Global webhook endpoint URL for completion notifications |
| `WEBHOOK_SECRET` | No | - | Webhook signature verification secret |
| `CLOUDFLARE_ACCOUNT_ID` | Yes | - | Cloudflare account ID for R2 storage |
| `R2_ACCESS_KEY_ID` | Yes | - | R2 storage access key |
| `R2_SECRET_ACCESS_KEY` | Yes | - | R2 storage secret key |  
| `R2_PUBLIC_DOMAIN` | Yes | - | R2 bucket public domain for file URLs |
| `CLOUDFLARE_API_TOKEN` | No | - | API token for KV rate limiting & caching |
| `CLOUDFLARE_KV_NAMESPACE_ID` | No | - | KV namespace ID for distributed features |
| `PORT` | No | 5500 | Server port |
| `MAX_FILE_SIZE_MB` | No | 50 | Maximum audio file size |

## Features

### **Intelligent Caching**

- Prevents duplicate processing of the same audio
- Uses secure SHA-256 cache keys with query normalization
- Automatic cleanup of old cached files
- 30-day TTL for cached results

### **Distributed Rate Limiting**

- Cloudflare KV-based rate limiting for stateless deployments
- Connection pooling for optimal performance
- Automatic fallback if KV is unavailable

### **Security**

- API key authentication required
- HMAC-SHA256 webhook signature verification
- Input validation and sanitization
- Security headers middleware

### **Monitoring**

- Comprehensive health checks
- Service operational status monitoring
- Structured logging with timestamps

## Development

```bash
# Format and lint code
uv run black . && uv run isort . && uv run pylint .

# Run with development settings
uv run python main.py
```

## Docker

```bash
docker build -t yt-audio-separator .
docker run -p 5500:5500 --env-file .env yt-audio-separator
```
