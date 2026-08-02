# Adroid Runtime Dockerfile
# Multi-stage build: build wheels in a builder stage, copy to slim runtime image.
#
# Usage:
#   docker build -t adroid:latest .
#   docker run -p 7654:7654 -v $(pwd)/data:/data adroid:latest
#
# The image runs `adroid start --bridge adb --port 7654` by default.
# Override CMD to use a different bridge (mock for testing without a device).

# ---------------------------------------------------------------------------
# Builder stage — install build deps + compile wheels
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Build deps for cryptography + pydantic
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only pyproject.toml first for better layer caching
COPY pyproject.toml README.md ./
COPY adroid/ ./adroid/

# Build wheels for all extras
RUN pip install --no-cache-dir --upgrade pip wheel && \
    pip wheel --no-cache-dir --wheel-dir /wheels ".[web,mcp]"

# ---------------------------------------------------------------------------
# Runtime stage — slim image with only runtime deps
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Install adb (for ADB bridge) + tini (proper signal handling)
# adb is needed inside the container so the runtime can shell out to it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    adb \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd --create-home --uid 1000 --shell /bin/bash adroid
WORKDIR /home/adroid

# Copy wheels from builder + install
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir /wheels/*.whl && \
    rm -rf /wheels

# Copy source (so editable install picks up templates + static)
COPY --chown=adroid:adroid . /home/adroid/app
WORKDIR /home/adroid/app
RUN pip install --no-cache-dir --no-deps -e .

# Create data directory for audit log + blobs
RUN mkdir -p /data && chown -R adroid:adroid /data
VOLUME /data

# Switch to non-root user
USER adroid

# Expose the default port
EXPOSE 7654

# Use tini for proper SIGTERM handling (clean shutdown)
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default: boot runtime with ADB bridge
# Override with `docker run adroid:latest adroid start --bridge mock` for testing
CMD ["adroid", "start", \
     "--bridge", "adb", \
     "--port", "7654", \
     "--host", "0.0.0.0", \
     "--audit-log", "/data/adroid.auditlog", \
     "--blob-store-dir", "/data/blobs"]

# Healthcheck — hit the tools endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7654/api/tools', timeout=3)" || exit 1
