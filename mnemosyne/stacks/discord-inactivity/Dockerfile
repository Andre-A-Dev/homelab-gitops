FROM python:3.12-slim

# tzdata is required, otherwise ZoneInfo("Europe/Berlin") silently fails
# and the daily scan would run on UTC.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/app/config.yaml \
    DB_PATH=/data/activity.db

RUN useradd --system --uid 10001 bot && mkdir -p /data && chown bot /data
USER bot

CMD ["python", "-m", "app.main"]
