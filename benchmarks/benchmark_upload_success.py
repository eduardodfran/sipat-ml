"""Benchmark upload success rate with retry logic."""

from __future__ import annotations

import json
import random
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add processing/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

NUM_UPLOADS = 200
MAX_RETRIES = 3
BASE_DELAY_MS = 1000  # 1 second base delay


def simulate_upload_with_retry(
    failure_rate: float = 0.05,
    max_retries: int = MAX_RETRIES,
) -> dict:
    """Simulate upload with retry logic and exponential backoff."""
    successes = 0
    failures = 0
    retry_counts = []
    total_times = []

    for _ in range(NUM_UPLOADS):
        attempt = 0
        success = False
        start_time = time.perf_counter()

        while attempt <= max_retries:
            # Simulate upload attempt - fail if random < failure_rate
            if random.random() >= failure_rate:
                success = True
                break
            attempt += 1
            if attempt <= max_retries:
                # Exponential backoff (simulated delay)
                delay = BASE_DELAY_MS * (2 ** attempt) / 1000
                time.sleep(min(delay * 0.001, 0.005))  # Scaled down for benchmark

        elapsed = (time.perf_counter() - start_time) * 1000
        total_times.append(elapsed)
        retry_counts.append(attempt)

        if success:
            successes += 1
        else:
            failures += 1

    return {
        "num_uploads": NUM_UPLOADS,
        "successes": successes,
        "failures": failures,
        "success_rate": round(successes / NUM_UPLOADS * 100, 2),
        "avg_retries": round(statistics.mean(retry_counts), 2),
        "max_retries_used": max(retry_counts),
        "retry_distribution": {
            str(i): retry_counts.count(i) for i in range(max_retries + 1)
        },
        "avg_time_ms": round(statistics.mean(total_times), 2),
        "p95_time_ms": round(sorted(total_times)[int(len(total_times) * 0.95)], 2),
    }


def benchmark_upload_success() -> dict:
    """Benchmark upload success rate with simulated failures."""
    # Test with different failure rates and retry configs
    scenarios = [
        {"name": "no_retry_low_failure", "failure_rate": 0.02, "max_retries": 0},
        {"name": "no_retry_medium_failure", "failure_rate": 0.10, "max_retries": 0},
        {"name": "retry_3x_medium_failure", "failure_rate": 0.10, "max_retries": 3},
        {"name": "retry_3x_high_failure", "failure_rate": 0.30, "max_retries": 3},
    ]

    results = []
    for scenario in scenarios:
        print(f"  Testing {scenario['name']} ({scenario['failure_rate']*100}% failure, {scenario['max_retries']} retries)...")
        result = simulate_upload_with_retry(scenario["failure_rate"], scenario["max_retries"])
        result["scenario"] = scenario["name"]
        result["failure_rate"] = scenario["failure_rate"]
        result["max_retries"] = scenario["max_retries"]
        results.append(result)
        print(f"    Success rate: {result['success_rate']}%, Failures: {result['failures']}/{NUM_UPLOADS}")

    # Find the overall success rate at medium failure with retries (most realistic)
    retry_result = next(r for r in results if r["scenario"] == "retry_3x_medium_failure")

    return {
        "num_uploads_per_scenario": NUM_UPLOADS,
        "scenarios": results,
        "overall_success_rate": retry_result["success_rate"],
    }


def main():
    print("=" * 60)
    print("Upload Success Rate Benchmark")
    print("=" * 60)

    print(f"\nRunning upload benchmark ({NUM_UPLOADS} uploads per scenario)...")
    results = benchmark_upload_success()

    output = {
        "benchmark": "upload_success_rate",
        "results": results,
    }

    output_path = RESULTS_DIR / "upload_success.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to {output_path}")
    print(f"\nSummary:")
    for scenario in results["scenarios"]:
        print(f"  {scenario['scenario']}: {scenario['success_rate']}% success rate")
    print(f"\nOverall success rate (medium failure): {results['overall_success_rate']}%")

    return output


if __name__ == "__main__":
    main()
