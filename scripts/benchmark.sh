#!/usr/bin/env bash
set -euo pipefail

echo "=================================================="
echo "          CARBON PERFORMANCE BENCHMARK            "
echo "=================================================="

# Check virtual environment
if [ -d ".venv" ]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="python3"
fi

echo "Using Python: $($PYTHON --version) ($PYTHON)"

# Ensure pytest is installed
if ! $PYTHON -m pytest --version >/dev/null 2>&1; then
    echo "Installing benchmark and test dependencies..."
    $PYTHON -m pip install -q -e ".[dev]"
fi

# Run performance benchmark suite
echo "Running performance benchmarks..."
$PYTHON -m pytest tests/performance -v -s || true

echo "=================================================="
echo "Benchmark run finished."
