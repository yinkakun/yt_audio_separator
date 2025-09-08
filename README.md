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

## Configuration

**Required:**

- `CLOUDFLARE_ACCOUNT_ID` - Cloudflare account ID  
- `R2_ACCESS_KEY_ID` - R2 access key
- `R2_SECRET_ACCESS_KEY` - R2 secret key
- `R2_PUBLIC_DOMAIN` - R2 bucket public domain

**Optional:**

- `PORT` - Server port (default: 5500)
- `MAX_FILE_SIZE_MB` - Max file size (default: 50MB)
- `WEBHOOK_URL` - Job notification webhook

## Development

**Format code:**

```bash
uv run black .
uv run isort .
```

**Lint:**

```bash
uv run pylint src/
```
