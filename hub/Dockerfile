FROM python:3.12-slim
WORKDIR /srv
# ffmpeg turns chat video clips into small MP4s without location data (app/chat.py save_video).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV HUB_CONFIG=/data/config.yaml HUB_DB=/data/hub.db
# The commit this image was built from (deploy/hub-run.sh passes it). /healthz reports it, and the
# self-updater only calls an update good once the new version answers.
ARG HUB_VERSION=dev
ENV HUB_VERSION=$HUB_VERSION
LABEL app=kidspc-hub
EXPOSE 8080
# Client IPs for the remote_access gate come from X-Real-IP, which Caddy sets (see deploy/Caddyfile).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
