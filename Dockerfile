FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir 'yt-dlp[default]==2026.10.21' && /opt/venv/bin/python -m yt_dlp --version
ENV PATH="/opt/venv/bin:$PATH" PORT=8080 MAQAM_DIRECT_AUDIO=0 TUNELIO_CREDIT_RESERVE=20
WORKDIR /app
COPY server.py fast_audio.py bench_fast.py benchmark-videos.json test_server.py test_fast_audio.py ./
USER node
EXPOSE 8080
CMD ["python", "server.py"]
