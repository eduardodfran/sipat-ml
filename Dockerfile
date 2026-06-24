FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app/processing

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY processing/requirements.txt .

# Install CPU-only torch first to avoid pulling CUDA (14GB+)
RUN pip3 install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu \
    torchvision --index-url https://download.pytorch.org/whl/cpu

RUN pip3 install --no-cache-dir -r requirements.txt

COPY processing/ ./processing/
COPY weights/ ./weights/

EXPOSE 8000

# Production: gunicorn with uvicorn workers (multi-worker)
CMD ["gunicorn", "processing.main:app", \
     "-w", "4", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "120"]

# Development: single uvicorn process (uncomment below, comment out gunicorn CMD)
# CMD ["uvicorn", "processing.main:app", "--host", "0.0.0.0", "--port", "8000"]
