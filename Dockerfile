# TARGETARCH and TARGETVARIANT are injected automatically by buildx.
# They must be declared before the first FROM to be usable in FROM instructions.
# For linux/arm/v7:  TARGETARCH=arm, TARGETVARIANT=v7  → selects base-armv7
# For linux/arm/v8:  TARGETARCH=arm, TARGETVARIANT=v8  → selects base-armv8
# For linux/amd64:   TARGETARCH=amd64, TARGETVARIANT=""  → selects base-amd64
# For linux/arm64:   TARGETARCH=arm64, TARGETVARIANT=""  → selects base-arm64
ARG TARGETARCH
ARG TARGETVARIANT

# Base images per platform — arm/v7 and arm/v8 (32-bit ARM, Raspberry Pi) need
# slim-bookworm; amd64 and arm64 use the smaller alpine image.
FROM python:3.12-alpine AS base-amd64
FROM python:3.12-alpine AS base-arm64
FROM python:3.12-slim-bookworm AS base-armv7
FROM python:3.12-slim-bookworm AS base-armv8
FROM base-${TARGETARCH}${TARGETVARIANT}

WORKDIR /app

# Install build dependencies, pip packages, then clean up.
# wget is kept as a runtime dependency for the HEALTHCHECK below.
# Package manager differs by base image (apk on Alpine, apt on Debian).
COPY requirements.txt .
RUN if command -v apk > /dev/null; then \
      apk add --no-cache --virtual .build-deps \
          gcc \
          python3-dev \
          make \
          automake \
          autoconf \
          libtool \
          musl-dev \
          linux-headers && \
      apk add --no-cache wget && \
      pip install --no-cache-dir -r requirements.txt && \
      apk del .build-deps; \
    else \
      apt-get update && \
      apt-get install -y --no-install-recommends \
          gcc \
          python3-dev \
          make \
          automake \
          autoconf \
          libtool \
          libffi-dev \
          wget && \
      pip install --no-cache-dir -r requirements.txt && \
      apt-get purge -y gcc python3-dev make automake autoconf libtool libffi-dev && \
      apt-get autoremove -y && \
      rm -rf /var/lib/apt/lists/*; \
    fi

# Copy application
COPY app/ ./app/

# Expose port
EXPOSE 8675

# Health check - use /health which always returns HTTP 200 (gateway status is in
# the JSON body). This makes it safe as a process-level liveness check.
# start_period gives the server time to establish its first gateway connection
# before Docker starts counting retries.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD wget --spider -q http://localhost:8675/health || exit 1

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8675"]
