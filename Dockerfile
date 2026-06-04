FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY processing/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY processing/ ./processing/
COPY weights/ ./weights/

EXPOSE 8000

CMD ["uvicorn", "processing.main:app", "--host", "0.0.0.0", "--port", "8000"]
