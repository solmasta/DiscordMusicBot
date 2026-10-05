FROM python:3.12-slim

# FFmpeg + libs required by discord.py[voice] / PyNaCl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libffi-dev libsodium-dev libopus0 libopus-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "bot.py"]
