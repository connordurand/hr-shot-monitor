FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    libbluetooth-dev \
    dbus \
    portaudio19-dev \
    libsndfile1 \
    lsof \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8050

CMD ["python", "-m", "src.dashboard_app"]
