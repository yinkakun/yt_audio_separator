# Youtube Audio Separator

Flask API service that downloads audio from YouTube and separates it into vocals and instrumental tracks.

## Usage

Start the server:
```bash
uv run python main.py
```

Separate audio:
```bash
curl -X POST http://localhost:5500/separate-audio \
  -H "Content-Type: application/json" \
  -d '{"title": "Song Title", "artist": "Artist Name"}'
```

## API Endpoints

- `POST /separate-audio` - Start audio separation job
