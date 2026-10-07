FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    AUTO_PUBLISH=false

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && useradd --uid 10001 --create-home appuser \
    && mkdir -p /app/data/media \
    && chown -R appuser:appuser /app/data

# The Phase 1 image is independent of Telegram, Buffer and image-hosting SaaS.
COPY config.py db.py ./
COPY services/__init__.py services/binance.py ./services/
COPY src/ ./src/
COPY prompts/ ./prompts/

USER appuser
EXPOSE 8080
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
CMD ["python", "-m", "src.app"]
