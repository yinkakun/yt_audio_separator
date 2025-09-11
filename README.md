# YouTube Audio Separator

FastAPI service that downloads YouTube audio and separates it into vocals and instrumental tracks using machine learning models.

## Quick Start

1. **Install dependencies:**

   ```bash
   uv sync
   ```

2. **Set environment variables:**

   ```bash
   export CLOUDFLARE_ACCOUNT_ID=your_account_id
   export R2_ACCESS_KEY_ID=your_access_key  
   export R2_SECRET_ACCESS_KEY=your_secret_key
   export R2_PUBLIC_DOMAIN=your_domain
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
     -d '{"search_query": "Song Title Artist"}'
   
   # Check job status
   curl http://localhost:5500/status/<track_id>
   ```

## API Reference

| Endpoint | Method | Description |
|----------|---------|-------------|
| `/separate-audio` | POST | Start audio separation job |
| `/status/{track_id}` | GET | Get job status and results |
| `/health` | GET | Health check with system metrics |
| `/` | GET | Basic health status |

### POST /separate-audio

Initiates audio separation for a YouTube video.

**Request:**

```json
{ "search_query": "Song Title Artist" }
```

**Response (202):**

```json
{ "track_id": "uuid", "status": "processing" }
```

### GET /status/{track_id}

Returns job status and download URLs when complete.

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

## Webhooks (Optional)

Configure `WEBHOOK_URL` to receive job completion notifications:

| Event | Description |
|-------|-------------|
| `job.completed` | Audio separation successful |
| `job.failed` | Audio separation failed |

**Example payload:**

```json
{
  "event": "job.completed",
  "timestamp": 1234567890.0,
  "data": {
    "track_id": "uuid",
    "status": "completed",
    "result": {
      "vocals_url": "https://domain.com/vocals.mp3",
      "instrumental_url": "https://domain.com/instrumental.mp3"
    }
  }
}
```

**Headers:** `Content-Type: application/json`, `X-Webhook-Signature: sha256=<signature>`

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CLOUDFLARE_ACCOUNT_ID` | Yes | - | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | Yes | - | R2 access key |
| `R2_SECRET_ACCESS_KEY` | Yes | - | R2 secret key |
| `R2_PUBLIC_DOMAIN` | Yes | - | R2 bucket public domain |
| `PORT` | No | 5500 | Server port |
| `MAX_FILE_SIZE_MB` | No | 50 | Maximum file size |
| `WEBHOOK_URL` | No | - | Job notification webhook URL |
| `WEBHOOK_SECRET` | No | - | Webhook signature verification secret |

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
