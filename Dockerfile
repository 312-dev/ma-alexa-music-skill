FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py subsonic.py oauth.py queuestate.py .

ENV PORT=5056 \
    CAPTURE_DIR=/data/captures

EXPOSE 5056

# One process, many threads. The caches are in-process, so two workers meant
# two of everything: a queue warmed on one worker was still cold on the other,
# and roughly half of all track transitions paid a 3s station build. Threads
# rather than a single sync worker because /stream proxies audio through this
# same app, and one blocking worker would stall every directive for the length
# of a song. The work is entirely I/O bound, so the GIL costs nothing here.
CMD ["gunicorn", "--bind", "0.0.0.0:5056", "--workers", "1", "--threads", "8", \
     "--access-logfile", "-", "app:app"]
