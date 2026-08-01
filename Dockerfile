FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py subsonic.py oauth.py queuestate.py signature.py queue_api.py \
     mdns.py smapi_rest.py catalog_sync.py ./
COPY setup_ui/ setup_ui/

# Baked into the image rather than left on a volume. Amazon refetches these on
# every manifest update, and a skill whose icons 404 fails the update with
# RESOURCE_NOT_FOUND, so "did you remember to put the files on the volume" is
# not a good first-run experience. ICON_DIR still overrides for anyone who
# wants their own artwork without rebuilding.
COPY icons/ icons/

ENV PORT=5056 \
    CAPTURE_DIR=/data/captures \
    ICON_DIR=/app/icons

EXPOSE 5056

# One process, many threads. The caches are in-process, so two workers meant
# two of everything: a queue warmed on one worker was still cold on the other,
# and roughly half of all track transitions paid a 3s station build. Threads
# rather than a single sync worker because /stream proxies audio through this
# same app, and one blocking worker would stall every directive for the length
# of a song. The work is entirely I/O bound, so the GIL costs nothing here.
CMD ["gunicorn", "--bind", "0.0.0.0:5056", "--workers", "1", "--threads", "8", \
     "--access-logfile", "-", "app:app"]
