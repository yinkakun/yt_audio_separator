# YouTube Audio Separator

A Flask API service that downloads audio from YouTube and separates it into vocals and instrumental tracks using AI-powered audio separation. The service integrates with Cloudflare R2 for cloud storage and supports webhook notifications for job status updates.

## Requirements

- Python 3.12+
- Cloudflare R2 storage account
- Environment variables (see Configuration section)

## Installation

 Clone the repository:

```bash
git clone <repository-url>
cd yt_audio_separator
```

 Install dependencies using uv:

```bash
uv sync
```

Set up environment variables (see Configuration section)

Run the application:

```bash
uv run python main.py
```

## Configuration

Create a `.env` file with the following variables:

### Required

```bash
SECRET_KEY=your_secret_key_here
CLOUDFLARE_ACCOUNT_ID=your_cloudflare_account_id
R2_ACCESS_KEY_ID=your_r2_access_key
R2_SECRET_ACCESS_KEY=your_r2_secret_key
```

### Optional

```bash
# Server settings
HOST=0.0.0.0
PORT=5500
DEBUG=false

# Processing limits
MAX_SEARCH_QUERY_LENGTH=200
MAX_FILE_SIZE_MB=50
PROCESSING_TIMEOUT=60
MAX_WORKERS=2
MAX_ACTIVE_JOBS=2
CLEANUP_INTERVAL=3600

# Rate limiting
RATE_LIMIT_REQUESTS=100 per hour
RATE_LIMIT_SEPARATION=5 per minute

# Storage
R2_BUCKET_NAME=audio-separation

# Webhooks (optional)
WEBHOOK_URL=https://your-webhook-endpoint.com/webhook
WEBHOOK_SECRET=your_webhook_secret_min_32_chars
WEBHOOK_TIMEOUT=30
WEBHOOK_MAX_RETRIES=3
WEBHOOK_RETRY_DELAY=5
```

## API Endpoints

### Start Audio Separation

```bash
POST /separate
```

**Request body:**

```json
{
  "search_query": "Song Title Artist Name"
}
```

**Response:**

```json
{
  "status": "processing",
  "track_id": "uuid-here",
  "correlation_id": "correlation-id"
  "message": "Audio separation started",
}
```

### Check Job Status

```bash
GET /status/<track_id>
```

**Response (processing):**

```json
{
  "progress": 50,
  "status": "processing",
  "track_id": "uuid-here",
  "created_at": 1234567890
}
```

**Response (completed):**

```json
{
  "track_id": "uuid-here",
  "status": "completed",
  "progress": 100,
  "created_at": 1234567890,
  "completed_at": 1234567920,
  "processing_time": 30,
  "result": {
    "track_id": "uuid-here",
    "original_title": "Song Title",
    "message": "Audio separation completed successfully",
    "vocals_url": "https://presigned-url-for-vocals.mp3",
    "instrumental_url": "https://presigned-url-for-instrumental.mp3",
  }
}
```

### Health Check

```bash
GET /health
```

**Response:**

```json
{
  "status": "healthy",
  "timestamp": 1234567890,
  "services": {
    "separator": true,
    "r2": true,
    "webhooks": true,
    "r2_operational": true
  },
  "metrics": {
    "active_jobs": 2,
    "max_jobs": 2
  }
}
```

## Usage Examples

### Basic separation

```bash
curl -X POST http://localhost:5500/separate \
  -H "Content-Type: application/json" \
  -d '{"search_query": "Bohemian Rhapsody Queen"}'
```

### Check status

```bash
curl http://localhost:5500/status/<track_id>
```

### Health check

```bash
curl http://localhost:5500/health
```

## Webhooks

When configured, the service sends webhook notifications for:

- **job.completed** - When separation completes successfully
- **job.failed** - When separation fails

Webhook payload example:

```json
{
  "event": "job.completed",
  "timestamp": 1234567890,
  "data": {
    "track_id": "uuid-here",
    "status": "completed",
    "result": {
      "message": "Audio separation completed successfully",
      "original_title": "Song Title",
      "vocals_url": "https://presigned-url",
      "instrumental_url": "https://presigned-url"
    }
  }
}
```

## Development

### Code formatting and linting

```bash
uv run black .
uv run isort .
uv run pylint main.py
```
