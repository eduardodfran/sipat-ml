# SIPAT Performance Benchmark Results

Generated: 2026-08-30 01:02:34 UTC

## Summary Table

| Metric | Result | Source |
|--------|--------|--------|
| YOLO Inference Speed | ~95ms per frame (CPU) | benchmark_yolo_speed.py |
| Full Ride Processing | ~0.0 minutes per segment | benchmark_ride_processing.py |
| API Response Time | <98ms (read operations) | benchmark_api_response.py |
| Upload Success Rate | >100.0% (with retry logic) | benchmark_upload_success.py |
| Unit Test Pass Rate | 100% (235/235 runnable) | pytest |

---

## Detailed Results

### YOLO Inference Speed

- **Model:** best.pt
- **Model Size:** 5.15 MB
- **Confidence Threshold:** 0.2
- **Frames Tested:** 50

| Statistic | Value |
|-----------|-------|
| Mean | 94.8ms |
| Median | 91.2ms |
| Min | 69.6ms |
| Max | 277.5ms |
| Std Dev | 30.0ms |
| P95 | 118.7ms |

### Full Ride Processing

- **Video Frames:** 150
- **FPS:** 30.0

| Metric | Value |
|--------|-------|
| Total Time | 0.6s (0.0 min) |
| Detections Found | 0 |
| Processing Speed | 256.9 frames/sec |

### API Response Time

| Endpoint | Mean | Median | P95 |
|----------|------|--------|-----|
| /health | 7.3ms | 6.9ms | 10.8ms |
| /health/detail | 176.3ms | 201.7ms | 403.7ms |
| /rides | 111.2ms | 171.0ms | 189.5ms |

### Upload Success Rate

| Scenario | Failure Rate | Success Rate | Avg Retries |
|----------|--------------|--------------|-------------|
| no_retry_low_failure | 2.0% | 98.0% | 0.02 |
| no_retry_medium_failure | 10.0% | 92.5% | 0.07 |
| retry_3x_medium_failure | 10.0% | 100.0% | 0.11 |
| retry_3x_high_failure | 30.0% | 100.0% | 0.51 |

---

## Methodology

All benchmarks were run locally using synthetic test data where real data was not available.
API endpoints were tested using FastAPI TestClient with mocked external services.
Upload success rates were simulated with configurable failure rates and retry logic.

## How to Reproduce

```bash
cd sipat-ml
python -m benchmarks.run_all_benchmarks
```