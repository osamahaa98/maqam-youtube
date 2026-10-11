FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --upgrade --pre 'yt-dlp[default]' && /opt/venv/bin/python -m yt_dlp --version
ENV PATH="/opt/venv/bin:$PATH" PORT=8080
WORKDIR /app
COPY server.py .
USER node
EXPOSE 8080
CMD ["python", "server.py"]
