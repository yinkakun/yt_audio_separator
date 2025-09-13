# YouTube Audio Separator

FastAPI service that downloads YouTube audio and separates it into vocals and instrumental tracks using ML models. Features intelligent caching, distributed rate limiting, job queues with Redis, and webhook notifications.

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
   
   # Required - For job queue (Redis)
   export REDIS_URL=rediss://username:password@your-upstash-redis.com:6379
   ```

3. **Start the services:**

   ```bash
   # Option 1: Using Docker Compose (recommended)
   docker-compose up --scale worker=2
   
   # Option 2: Manual start
   # Terminal 1 - Start web server
   uv run python main.py
   
   # Terminal 2 - Start worker(s)
   uv run python worker.py
   ```

4. **Process audio:**

   ```bash
   # Start separation job
   curl -X POST http://localhost:5500/separate-audio \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_secret_key" \
     -d '{"search_query": "Song Title Artist"}'
   
   # Check job status
   curl -H "X-API-Key: your_api_secret_key" \
     http://localhost:5500/job/your-track-id
   
   # Check service health
   curl http://localhost:5500/health
   ```

## API Reference

| Endpoint | Method | Description |
|----------|---------|-------------|
| `/separate-audio` | POST | Start audio separation job (queued with Redis) |
| `/job/{track_id}` | GET | Get job status and results by track ID |
| `/queue/info` | GET | Get queue statistics (admin) |
| `/health` | GET | Health check with system metrics and service status |

### POST /separate-audio

Initiates audio separation for a YouTube video and queues it for processing via Redis. Features intelligent caching to prevent duplicate processing and reduce costs.

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

### GET /job/{track_id}

Get the current status and results of a processing job by track ID.

**Headers:**

```
X-API-Key: your_api_secret_key
```

**Response (Processing):**

```json
{
  "status": "processing",
  "track_id": "uuid",
  "created_at": 1234567890.0,
  "started_at": 1234567891.0
}
```

**Response (Completed):**

```json
{
  "status": "completed",
  "track_id": "uuid",
  "created_at": 1234567890.0,
  "started_at": 1234567891.0,
  "ended_at": 1234567920.0,
  "result": {
    "vocals_url": "https://domain.com/vocals.mp3",
    "instrumental_url": "https://domain.com/instrumental.mp3",
    "track_id": "uuid"
  },
  "progress": 100
}
```

**Response (Failed):**

```json
{
  "status": "failed",
  "track_id": "uuid",
  "created_at": 1234567890.0,
  "started_at": 1234567891.0,
  "ended_at": 1234567895.0,
  "error": "Error message",
  "progress": 0
}
```

### GET /queue/info

Get queue statistics and information (admin endpoint).

**Headers:**

```
X-API-Key: your_api_secret_key
```

**Response:**

```json
{
  "name": "default",
  "length": 5,
  "job_ids": ["uuid1", "uuid2", "uuid3"]
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
| `REDIS_URL` | Yes | - | Redis connection URL for job queue (e.g., Upstash Redis) |
| `PORT` | No | 5500 | Server port |
| `MAX_FILE_SIZE_MB` | No | 50 | Maximum audio file size |

## Features

### **Redis Job Queue**

- **Multiple concurrent requests** - No more blocking on audio processing
- **Job persistence** - Jobs survive server restarts and failures
- **Horizontal scaling** - Add worker containers to increase throughput
- **Real-time status tracking** - Monitor job progress via REST API
- **Built-in retries** - Automatic retry on transient failures
- **Upstash Redis support** - SSL-ready for managed Redis services

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

## Docker Deployment

### Docker Compose (Recommended)

```bash
# Build and start web server + workers
docker-compose up --build

# Scale workers for higher throughput
docker-compose up --scale worker=4

# Background deployment
docker-compose up -d --scale worker=2
```

### Manual Docker

```bash
# Build the image
docker build -t yt-audio-separator .

# Run web server
docker run -p 5500:5500 --env-file .env \
  --target web yt-audio-separator

# Run workers (separate containers)
docker run --env-file .env \
  --target worker yt-audio-separator
```

### Worker Configuration

Environment variables for workers:

```bash
WORKER_NAME=audio-worker-1          # Unique worker identifier
QUEUE_NAMES=default                 # Comma-separated queue names
WORKER_CONCURRENCY=1               # Jobs per worker
```
