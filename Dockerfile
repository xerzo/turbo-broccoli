# ============================================================
# Single image for all 3 apps: tg-comment, anisearch, mongosync
# Multi-stage build — compile TgCrypto in builder, keep final slim
# ============================================================

# --- Stage 1: Builder (compile C extensions) ---
FROM python:3.12-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# --- Stage 2: Final runtime (no compiler, no dev headers) ---
FROM python:3.12-slim-bookworm

WORKDIR /app

# Install pre-built wheels from builder stage
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# Copy all app folders
COPY tg-comment/ /app/tg-comment/
COPY anisearch/ /app/anisearch/
COPY mongosync/ /app/mongosync/

# Real-time log output
ENV PYTHONUNBUFFERED=1

# Default — each service overrides this in docker-compose
CMD ["python", "--version"]

