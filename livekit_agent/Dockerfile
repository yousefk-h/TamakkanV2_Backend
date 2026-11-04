# For deploying this worker to LiveKit Cloud (`lk agent create`/`deploy`,
# run from INSIDE this directory so the build context is just
# `livekit_agent/` - the separate deployable it already is everywhere else
# in this codebase, see agent.py's module docstring).
#
# Copied into /app/livekit_agent/ (not flattened to /app/) specifically so
# this package's existing absolute imports (`from livekit_agent.config
# import ...`) keep working completely unchanged - identical to how it's
# run locally (`python -m livekit_agent.agent start`, from the backend repository root).
#
# LiveKit Cloud injects LIVEKIT_URL/LIVEKIT_API_KEY/LIVEKIT_API_SECRET at
# runtime automatically - never set those as secrets (see README's "Deploying
# to LiveKit Cloud" section).

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---- build stage ------------------------------------------------------
FROM base AS build

RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/livekit_agent
COPY requirements.txt ./

RUN python -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ---- production stage --------------------------------------------------
FROM base

ARG UID=10001
RUN adduser --disabled-password --gecos "" --home "/app" --shell "/sbin/nologin" --uid "${UID}" appuser

WORKDIR /app
COPY --from=build --chown=appuser:appuser /app /app
ENV PATH="/app/.venv/bin:$PATH"
USER appuser

CMD ["python", "-m", "livekit_agent.agent", "start"]
