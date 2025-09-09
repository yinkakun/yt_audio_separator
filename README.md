# YouTube Audio Separator

Downloads YouTube audio and separates it into vocals and instrumental tracks.

## Installation

```bash
uv sync
```

## Usage

**1. Set environment variables:**

```bash
export CLOUDFLARE_ACCOUNT_ID=your_account_id
export R2_ACCESS_KEY_ID=your_access_key  
export R2_SECRET_ACCESS_KEY=your_secret_key
export R2_PUBLIC_DOMAIN=your_domain
```

**2. Start server:**

```bash
uv run python main.py
```

**3. Separate audio:**

```bash
curl -X POST http://localhost:5500/separate-audio \
  -H "Content-Type: application/json" \
  -d '{"search_query": "Song Title Artist"}'
```

**4. Check status:**

```bash
curl http://localhost:5500/status/<track_id>
```

## API Routes

### POST /separate-audio

Start audio separation job.

**Request:**

```json
{
  "search_query": "Song Title Artist"
}
```

**Response (202):**

```json
{
  "track_id": "uuid",
  "status": "processing",
}
```

### GET /status/{track_id}

Get job status and results.

**Response:**

```json
{
  "track_id": "uuid",
  "status": "completed|processing|failed",
  "progress": 100,
  "created_at": 1234567890.0,
  "completed_at": 1234567890.0,
  "processing_time": 30.5,
  "error": "Error message (if failed)",
  "result": {
    "vocals_url": "https://domain.com/vocals.mp3",
    "instrumental_url": "https://domain.com/instrumental.mp3",
    "track_id": "uuid"
  }
}
```

### GET /health

Health check endpoint.

**Response:**

```json
{
  "status": "healthy",
  "timestamp": 1234567890.0,
  "metrics": {
    "active_jobs": 2,
    "max_active_jobs": 5
  },
  "services": {
    "r2_operational": true,
    "r2_configured": true
  }
}
```

### GET /

Root endpoint returning basic status.

**Response:**

```json
{
  "status": "healthy"
}
```

## Webhook Events

When `WEBHOOK_URL` is configured, the following events are sent:

### job.completed

Sent when audio separation completes successfully.

**Payload:**

```json
{
  "event": "job.completed",
  "timestamp": 1234567890.0,
  "data": {
    "track_id": "uuid",
    "status": "completed",
    "result": {
      "vocals_url": "https://domain.com/vocals.mp3",
      "instrumental_url": "https://domain.com/instrumental.mp3",
      "track_id": "uuid"
    }
  }
}
```

### job.failed

Sent when audio separation fails.

**Payload:**

```json
{
  "event": "job.failed",
  "timestamp": 1234567890.0,
  "data": {
    "track_id": "uuid",
    "status": "failed",
    "error": "Error message"
  }
}
```

**Webhook Headers:**

- `Content-Type: application/json`
- `X-Webhook-Signature: sha256=<signature>` (HMAC-SHA256 of payload)
- `User-Agent: AudioSeparator/1.0`

## Configuration

**Required:**

- `CLOUDFLARE_ACCOUNT_ID` - Cloudflare account ID  
- `R2_ACCESS_KEY_ID` - R2 access key
- `R2_SECRET_ACCESS_KEY` - R2 secret key
- `R2_PUBLIC_DOMAIN` - R2 bucket public domain

**Optional:**

- `PORT` - Server port (default: 5500)
- `MAX_FILE_SIZE_MB` - Max file size (default: 50MB)
- `WEBHOOK_URL` - Job notification webhook URL
- `WEBHOOK_SECRET` - Secret for webhook signature verification

## Development

**Format code:**

```bash
uv run black .
uv run isort .
```

**Lint:**

```bash
uv run pylint .
```
