# Multi-arch (builds natively on the Pi 5's arm64).
FROM python:3.11-slim-bookworm

# System libs matplotlib/mplfinance need at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libfreetype6 libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Leverage the piwheels index on ARM so numpy/pandas/matplotlib ship as
# prebuilt wheels instead of compiling from source (minutes vs an hour).
COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir \
        --extra-index-url https://www.piwheels.org/simple \
        -r requirements.txt

# Optional AI coach (google-genai + anthropic SDKs) — off by default so the image stays slim.
#   docker compose build --build-arg AI_ENABLED=1
ARG AI_ENABLED=0
RUN if [ "$AI_ENABLED" = "1" ]; then \
        pip install --no-cache-dir --extra-index-url https://www.piwheels.org/simple \
            -r requirements-ai.txt; \
    fi

COPY src/ ./src/
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    BREAK_SIGNAL_CONFIG=/app/config.yaml \
    MPLBACKEND=Agg

# config.yaml, the state db, the journal db, screenshots and backups all live
# on the mounted ./data volume (see docker-compose.yml), never in the image.
CMD ["python", "-m", "break_signal"]
