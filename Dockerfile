FROM python:3.12-slim

# ffmpeg joins video + audio and converts to MP3.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp needs a JavaScript runtime to solve YouTube's player challenges.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
# yt-dlp[default] includes yt-dlp-ejs (the YouTube solver scripts that run in Deno).
# Always pull the newest yt-dlp at build time, since YouTube changes often.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U "yt-dlp[default]" \
    && deno --version && yt-dlp --version

COPY . .

ENV PORT=10000 PYTHONUNBUFFERED=1
EXPOSE 10000

# One worker so every request sees the same in-memory download jobs; threads handle concurrency.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 16 --timeout 120 --access-logfile -
