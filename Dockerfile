FROM python:3.13-slim

RUN apt-get update && apt-get install -y \
    libcairo2 \
    libffi-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
CMD gunicorn app:app --workers 2 --timeout 120 --bind 0.0.0.0:8080
