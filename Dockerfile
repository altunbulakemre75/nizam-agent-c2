# NIZAM COP — Dockerfile
# Multi-stage: builder installs deps, runtime image is slim.

# ── Stage 1: Builder ──────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ──────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN useradd -m -u 1001 nizam
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY . .

# Leaflet static files must exist (downloaded during dev — committed to repo)
# Ensure the images dir is present
RUN mkdir -p cop/static/images

# Switch to non-root
USER nizam

# Default: COP server
ENV COP_PORT=8100
EXPOSE 8100

CMD ["python", "-m", "uvicorn", "cop.server:app", \
     "--host", "0.0.0.0", "--port", "8100", \
     "--log-level", "info"]
