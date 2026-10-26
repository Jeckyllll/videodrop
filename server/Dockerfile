FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt 'yt-dlp[default]>=2026.8.19' 'imageio-ffmpeg>=0.6.0'
COPY worker.py /app/worker.py
COPY server /app/server
RUN useradd -u 10001 -m -s /usr/sbin/nologin videodrop
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 VIDEODROP_STATE=/data
CMD ["python", "-m", "server.app"]
