FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/app/data/openlease.db
EXPOSE 8000
# data/ is a mounted volume (see docker-compose) so listings + cache survive restarts
CMD ["sh", "-c", "mkdir -p /app/data && uvicorn app.app:app --host 0.0.0.0 --port 8000"]
