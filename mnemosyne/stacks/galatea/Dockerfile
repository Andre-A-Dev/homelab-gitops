FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

# Exactly one worker process: the interlock's check-then-switch lock and the
# Prometheus counters live in process memory. More workers would silently
# break both. Threads cover the concurrency a single-user UI needs.
CMD ["gunicorn", "--workers", "1", "--threads", "4", \
     "--bind", "0.0.0.0:8000", "--access-logfile", "-", \
     "app.main:app"]
