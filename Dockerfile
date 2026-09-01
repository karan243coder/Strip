FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    SC_DOWNLOAD_DIR=/tmp/sc_downloads

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /tmp/sc_downloads

EXPOSE 8080
CMD ["python", "-u", "bot.py"]
