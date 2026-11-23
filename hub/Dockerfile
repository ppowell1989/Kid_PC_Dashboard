FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV HUB_CONFIG=/data/config.yaml HUB_DB=/data/hub.db
EXPOSE 8080
# Client IPs for the remote_access gate come from X-Real-IP, which Caddy sets (see deploy/Caddyfile).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
