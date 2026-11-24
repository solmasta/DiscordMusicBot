FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
COPY cogs ./cogs
# Mount a volume at /data so the bot remembers its voice channel across restarts.
ENV STATE_FILE=/data/home.json
CMD ["python", "bot.py"]
