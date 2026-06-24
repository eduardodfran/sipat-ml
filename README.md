# SIPAT-ML

Pothole detection and severity classification service for the SIPAT (System for Infrastructure Pothole Assessment Technology) application. Processes road videos using YOLO object detection, GPS telemetry, and Inverse Perspective Mapping (IPM) to identify, classify, and track potholes.

## Architecture

```
sipat-ml/
├── processing/                  # Main application code
│   ├── api/routes/              # FastAPI route handlers
│   │   ├── health.py            # Health check endpoints
│   │   ├── process.py           # Ride processing trigger
│   │   └── rides.py             # Ride CRUD operations
│   ├── config/                  # Settings and constants
│   ├── core/                    # Business logic
│   │   ├── clusterer.py         # DBSCAN pothole clustering
│   │   └── severity.py          # Severity classification (DPWH/FHWA aligned)
│   ├── pipeline/                # Background processing
│   │   └── worker.py            # RideProcessor orchestrator
│   ├── services/                # External service clients
│   │   ├── blob_storage.py      # Azure Blob Storage
│   │   └── supabase_client.py   # Supabase (PostgreSQL + Auth)
│   ├── utils/                   # Computational utilities
│   │   ├── camera_calibration.py
│   │   ├── geo_math.py          # Haversine, bearing calculations
│   │   ├── geo_sync.py          # GPS frame synchronization
│   │   ├── gps_processor.py     # GPS track processing
│   │   └── ipm_transformer.py   # Inverse Perspective Mapping
│   ├── main.py                  # FastAPI app entrypoint
│   ├── upload_api.py            # Upload endpoints
│   └── detection_batch_builder.py  # YOLO detection orchestration
├── tests/                       # Test suite
├── weights/                     # YOLO model weights (not in git)
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## Quick Start

### Prerequisites

- Python 3.12+
- YOLO model weights (`weights/best.pt`)
- Supabase project with service role key
- Azure Storage account for blob storage

### Setup

```bash
# Clone and enter directory
git clone <repo-url>
cd sipat-ml

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r processing/requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your Supabase and Azure credentials
```

### Running Locally

```bash
# Start the API server
python -m uvicorn processing.main:app --host 0.0.0.0 --port 8000 --reload

# Or run the legacy batch worker
python -m processing.batch_worker
```

### Running with Docker

```bash
docker compose up --build
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Basic health check |
| `GET` | `/health/detail` | Detailed health with Supabase status |
| `POST` | `/upload/init` | Initialize upload, get SAS URLs |
| `POST` | `/upload/complete` | Finalize upload, create ride record |
| `POST` | `/upload/abort` | Cancel upload, cleanup blobs |
| `GET` | `/rides` | List user's rides |
| `GET` | `/rides/{id}` | Get ride details |
| `DELETE` | `/rides/{id}` | Delete a ride |
| `POST` | `/process/{id}` | Trigger ride processing |

All endpoints (except `/health`) require `Authorization: Bearer <token>` header.

## Processing Pipeline

1. **Upload**: Client uploads video (MP4) and GPS data (JSON) via SAS URLs
2. **Queue**: Ride record created with `status: queued`
3. **Claim**: Worker claims oldest queued ride, sets `status: processing`
4. **Download**: Video and GPS data downloaded to temp directory
5. **Detection**: YOLO model runs on every 5th frame with IOU deduplication
6. **Severity**: Combined IPM-based area + frame area + confidence capping
7. **Clustering**: DBSCAN merges nearby detections into verified potholes
8. **Sync**: Results merged with existing potholes or inserted as new
9. **Complete**: Ride marked as `status: completed`

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SUPABASE_URL` | Yes | - | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | - | Service role JWT |
| `AZURE_STORAGE_CONNECTION_STRING` | Yes | - | Azure Storage connection |
| `CORS_ORIGINS` | No | `*` | Comma-separated allowed origins |
| `RATE_LIMIT_UPLOAD` | No | `10/hour` | Upload endpoint rate limit |
| `RATE_LIMIT_PROCESS` | No | `5/hour` | Process endpoint rate limit |
| `RATE_LIMIT_READ` | No | `60/minute` | Read endpoint rate limit |

## Testing

```bash
# Run unit tests
pytest tests/test_unit.py -v

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=processing --cov-report=term-missing
```

## Development

### Linting

```bash
ruff check processing/
ruff format processing/
```

### Type Checking

```bash
mypy processing/
```

## Severity Classification

Based on DPWH D.O. No. 120 s. 2019 (adopting FHWA LTPP Distress ID Manual):

| Severity | Area (m²) | Description |
|----------|-----------|-------------|
| Minor | < 0.03 | Small surface defect |
| Moderate | 0.03 - 0.17 | Medium pothole |
| Severe | > 0.17 | Large/deep pothole |

Severity is further adjusted by detection confidence:
- Low confidence (< 0.35): capped at Minor
- Medium confidence (< 0.50): capped at Moderate

## License

Internal use only.
