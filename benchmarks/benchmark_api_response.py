"""Benchmark API response time for read operations."""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add processing/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

NUM_REQUESTS = 100


def benchmark_health_endpoint() -> dict:
    """Benchmark the health check endpoint."""
    from fastapi.testclient import TestClient
    from processing.main import app

    client = TestClient(app)
    times = []

    for _ in range(NUM_REQUESTS):
        start = time.perf_counter()
        response = client.get("/health")
        elapsed = (time.perf_counter() - start) * 1000  # ms
        times.append(elapsed)

    return {
        "endpoint": "/health",
        "method": "GET",
        "num_requests": len(times),
        "mean_ms": round(statistics.mean(times), 2),
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "p95_ms": round(sorted(times)[int(len(times) * 0.95)], 2),
    }


def benchmark_health_detail_endpoint() -> dict:
    """Benchmark the detailed health check endpoint."""
    from fastapi.testclient import TestClient
    from processing.main import app

    # Mock Supabase service to avoid real DB connection
    with patch("processing.services.supabase_client.get_supabase_service") as mock_svc:
        mock_instance = MagicMock()
        mock_instance.validate_token.return_value = {"user_id": "test"}
        mock_instance.select.return_value = [{"id": "test"}]
        mock_instance.get_circuit_status.return_value = {"state": "closed"}
        mock_svc.return_value = mock_instance

        client = TestClient(app)
        times = []

        for _ in range(NUM_REQUESTS):
            start = time.perf_counter()
            response = client.get("/health/detail")
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)

    return {
        "endpoint": "/health/detail",
        "method": "GET",
        "num_requests": len(times),
        "mean_ms": round(statistics.mean(times), 2),
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "p95_ms": round(sorted(times)[int(len(times) * 0.95)], 2),
    }


def benchmark_rides_endpoint() -> dict:
    """Benchmark the list rides endpoint."""
    from fastapi.testclient import TestClient
    from processing.main import app

    with patch("processing.services.supabase_client.get_supabase_service") as mock_svc:
        mock_instance = MagicMock()
        mock_instance.validate_token.return_value = {"user_id": "test-user", "sub": "test-user"}
        mock_instance.table.return_value.select.return_value.eq.return_value.range.return_value.execute.return_value = MagicMock(
            data=[
                {"id": f"ride-{i}", "status": "completed", "created_at": "2026-01-01T00:00:00Z"}
                for i in range(10)
            ],
            count=10,
        )
        mock_svc.return_value = mock_instance

        client = TestClient(app)
        times = []

        headers = {"Authorization": "Bearer test-token"}
        for _ in range(NUM_REQUESTS):
            start = time.perf_counter()
            response = client.get("/rides", headers=headers)
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)

    return {
        "endpoint": "/rides",
        "method": "GET",
        "num_requests": len(times),
        "mean_ms": round(statistics.mean(times), 2),
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "p95_ms": round(sorted(times)[int(len(times) * 0.95)], 2),
    }


def main():
    print("=" * 60)
    print("API Response Time Benchmark")
    print("=" * 60)

    results = []

    # Benchmark each endpoint
    print(f"\nBenchmarking /health ({NUM_REQUESTS} requests)...")
    results.append(benchmark_health_endpoint())
    print(f"  Mean: {results[-1]['mean_ms']:.1f}ms")

    print(f"\nBenchmarking /health/detail ({NUM_REQUESTS} requests)...")
    results.append(benchmark_health_detail_endpoint())
    print(f"  Mean: {results[-1]['mean_ms']:.1f}ms")

    print(f"\nBenchmarking /rides ({NUM_REQUESTS} requests)...")
    results.append(benchmark_rides_endpoint())
    print(f"  Mean: {results[-1]['mean_ms']:.1f}ms")

    # Calculate overall stats
    all_means = [r["mean_ms"] for r in results]
    overall = {
        "num_endpoints": len(results),
        "num_requests_per_endpoint": NUM_REQUESTS,
        "overall_mean_ms": round(statistics.mean(all_means), 2),
        "overall_median_ms": round(statistics.median(all_means), 2),
        "fastest_endpoint": min(results, key=lambda x: x["mean_ms"])["endpoint"],
        "slowest_endpoint": max(results, key=lambda x: x["mean_ms"])["endpoint"],
    }

    output = {
        "benchmark": "api_response_time",
        "results": results,
        "overall": overall,
    }

    output_path = RESULTS_DIR / "api_response.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to {output_path}")
    print(f"\nSummary:")
    for r in results:
        print(f"  {r['endpoint']}: {r['mean_ms']:.1f}ms mean, {r['p95_ms']:.1f}ms p95")
    print(f"\nOverall mean: {overall['overall_mean_ms']:.1f}ms")

    return output


if __name__ == "__main__":
    main()
