# Base stage with common dependencies
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    libsndfile1-dev \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r -g 1000 appuser && \
    useradd -r -u 1000 -g appuser -m appuser

# Set working directory and change ownership
WORKDIR /app
RUN chown appuser:appuser /app

# Install uv as root, then switch to appuser
RUN pip install uv

# Switch to non-root user for everything else
USER appuser

# Set up Python environment in user space
ENV PATH="/home/appuser/.local/bin:$PATH"
ENV PYTHONPATH="/app"

# Copy and install dependencies
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY --chown=appuser:appuser . .

# Create directories in user space
RUN mkdir -p \
    /home/appuser/audio_workspace \
    /home/appuser/models

# Use environment variables to point to user directories
ENV MODELS_DIR=/home/appuser/models
ENV AUDIO_WORKSPACE_DIR=/home/appuser/audio_workspace

# Web server stage
FROM base AS web
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uv", "run", "uvicorn", "__init__:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# Worker stage  
FROM base AS worker
CMD ["uv", "run", "python", "worker.py"]

# Default stage
FROM web AS default