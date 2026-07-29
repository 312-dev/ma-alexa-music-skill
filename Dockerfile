FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py subsonic.py oauth.py .

ENV PORT=5056 \
    CAPTURE_DIR=/data/captures

EXPOSE 5056

CMD ["gunicorn", "--bind", "0.0.0.0:5056", "--workers", "2", "--access-logfile", "-", "app:app"]
