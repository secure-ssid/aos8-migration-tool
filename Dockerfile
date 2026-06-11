FROM python:3.12-slim

# Run as a dedicated unprivileged user — never root (defense in depth, and so
# the per-user credstore home is not /root shared across the container).
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R appuser:appuser /app

# State dir (user registry + per-user encrypted creds) owned by the runtime
# user, so a named volume mounted here initializes with the right ownership.
RUN install -d -o appuser -g appuser -m 700 /home/appuser/.aos8-migration

# Deployment knobs (override per environment — see docker-compose.yml):
#   AOS8_AUTH_MODE=password  one shared gate password (simplest multi-user)
#                  =accounts  per-person @hpe.com self-service login
#   AOS8_APP_PASSWORD=...     the shared password (password mode)
#   AOS8_CREDSTORE_KEY=...    Fernet key enabling encrypted "Remember";
#                             unset in a multi-user mode => persistence is OFF
# The image defaults to single-user 'local' mode so a plain `docker run` works.
ENV AOS8_AUTH_MODE=local \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8501

HEALTHCHECK --start-period=30s --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"

# Production server flags: headless, no dev hot-reload, no file watcher, XSRF on.
CMD ["streamlit", "run", "app.py", \
     "--server.headless=true", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.runOnSave=false", \
     "--server.fileWatcherType=none", \
     "--server.enableXsrfProtection=true", \
     "--browser.gatherUsageStats=false"]
