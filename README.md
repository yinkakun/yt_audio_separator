# YouTube Audio Separator

FastAPI service that separates YouTube audio into vocals and instrumental tracks. Features Redis job queues, intelligent caching, rate limiting, and webhooks.

## Setup

```bash
# Install dependencies
uv sync

# Set required environment variables
export CLOUDFLARE_ACCOUNT_ID=your_account_id
export R2_ACCESS_KEY_ID=your_access_key
export R2_SECRET_ACCESS_KEY=your_secret_key
export R2_PUBLIC_DOMAIN=your_domain
export API_SECRET_KEY=your_api_secret_key
export REDIS_URL=rediss://username:password@your-upstash-redis.com:6379

# Start services
docker-compose up --scale worker=2
# OR manually: uv run python main.py && uv run python worker.py
```

## Usage

```bash
# Start job
curl -X POST http://localhost:5500/separate-audio \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_secret_key" \
  -d '{"search_query": "Song Title Artist"}'

# Check status
curl -H "X-API-Key: your_api_secret_key" \
  http://localhost:5500/job/your-track-id
```

## API

| Endpoint | Description |
|----------|-------------|
| `POST /separate-audio` | Start audio separation job |
| `GET /job/{track_id}` | Get job status and results |
| `GET /health` | Health check |

### Request

```bash
curl -X POST http://localhost:5500/separate-audio \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_secret_key" \
  -d '{"search_query": "Song Title Artist"}'
```

### Response

```json
{
  "track_id": "uuid",
  "status": "processing|completed|failed"
}
```

## Configuration

Required environment variables:

| Variable | Description |
|----------|-------------|
| `API_SECRET_KEY` | API authentication key |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 storage access key |
| `R2_SECRET_ACCESS_KEY` | R2 storage secret key |
| `R2_PUBLIC_DOMAIN` | R2 bucket domain |
| `REDIS_URL` | Redis connection URL |

Optional:
- `WEBHOOK_URL` - Job completion notifications
- `CLOUDFLARE_API_TOKEN` - Rate limiting
- `PORT` - Server port (default: 5500)

## Deployment

### Docker

```bash
docker-compose up --scale worker=2
```

### Fly.io

```bash
fly secrets set API_SECRET_KEY="your-key"
fly deploy
```
