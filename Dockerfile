# Container image for the main FastAPI backend (`app/`) ONLY - not
# `livekit_agent/`, which is a deliberately separate deployable (its own
# requirements.txt, its own settings, no import of `app.*` - see that
# package's own docstrings/README). It's meant to run as its own process
# (typically on LiveKit Cloud's own agent hosting), not bundled in here.
#
# Build/run from the backend repository root:
#   docker build -t tamakkan-backend:"$(git rev-parse HEAD)" .
#   docker run --env-file .env -p 4652:4652 tamakkan-backend:"$(git rev-parse HEAD)"
# Only ever tag this image tamakkan-backend:<commit-sha> - never a shared or
# `latest` tag (see docker-compose.yml).

# ---- builder: resolve dependencies into a venv ----------------------------
FROM python:3.12-slim-bookworm AS builder

# bcrypt/pydantic-core etc ship manylinux wheels for this image, but keep a
# compiler around in the builder stage in case pip ever has to build one from
# source for this platform - it never reaches the final image either way.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---- final: slim runtime image ---------------------------------------------
FROM python:3.12-slim-bookworm

# ffmpeg (+ffprobe, same package) - required on PATH for the transcript
# pipeline's chunk-cutting (see app/core/video_processing.py).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Only `app/` - no tests, docs, .env, or the separate livekit_agent/ package
# (see .dockerignore for the full exclusion list).
COPY app ./app
RUN chown -R appuser:appuser /app

USER appuser
EXPOSE 4652

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4652/api/v1/health', timeout=3)" || exit 1

# The fixed, no-reload entry point (see app/main.py's own docstring) -
# `uvicorn --reload` is for local dev only.
CMD ["python", "-m", "app.main"]
