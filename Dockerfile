FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN nice -n 19 npm ci --no-audit --no-fund
COPY frontend/ ./
RUN nice -n 19 npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai \
    V2_CONTAINER=1 V2_HOST_PID_CHECK=0 V2_BOARD_DIR=/srv/v2-wiring-board \
    V2_REPO_DIR=/srv/v0-ai V2_MIRROR_DIR=/var/lib/v2-board
RUN nice -n 19 apt-get update && nice -n 19 apt-get install -y --no-install-recommends git openssh-client tzdata ca-certificates && nice -n 19 rm -rf /var/lib/apt/lists/* \
    && nice -n 19 groupadd -g 10001 board && nice -n 19 useradd -u 10001 -g board -m board \
    && nice -n 19 mkdir -p /app/backend /var/lib/v2-board && nice -n 19 chown -R board:board /app /var/lib/v2-board
WORKDIR /app
COPY requirements.txt ./
RUN nice -n 19 pip install --no-cache-dir -r requirements.txt
COPY --chown=board:board backend/ ./backend/
COPY --from=frontend --chown=board:board /build/frontend/dist/ ./frontend/dist/
USER board
WORKDIR /app/backend
EXPOSE 3160
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3160/api/session', timeout=2)"]
CMD ["nice", "-n", "19", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3160"]
