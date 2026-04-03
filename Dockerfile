FROM python:3.11-slim

WORKDIR /app

# Ensure print() output appears immediately in docker logs
ENV PYTHONUNBUFFERED=1

# Install dependencies first for better layer caching
COPY requirements.txt requirements-bot.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-bot.txt

# Copy application code and ensure readable by non-root user
COPY . .
RUN chmod -R a+rX .

# Expose the bot API port
EXPOSE 3006

# Run as a non-root user (security best practice — CWE-250)
RUN adduser --disabled-password --gecos "" botuser
USER botuser

# Health check — hits the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3006/health')"

# Run the FastAPI bot service
# BOT_HOST and BOT_PORT come from environment variables (defaulting to 0.0.0.0:3006)
CMD ["sh", "-c", "uvicorn bot.api.main:app --host ${BOT_HOST:-0.0.0.0} --port ${BOT_PORT:-3006}"]
