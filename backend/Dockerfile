FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=100

COPY requirements.txt .
# The cache mount keeps downloaded wheels out of the image but reuses them
# across rebuilds, so a new base image does not mean downloading them again.
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

# The code stays root-owned and read-only to the runtime user, whatever the
# file modes were in the build context.
RUN chmod -R a+rX,go-w /app && useradd --create-home --uid 10001 opsforge
USER opsforge

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://localhost:8000/health', timeout=2).status != 200)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
