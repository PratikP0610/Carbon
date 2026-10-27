#!/usr/bin/env bash
set -euo pipefail

echo "=================================================="
echo "          CARBON SYSTEM DIAGNOSTICS               "
echo "=================================================="

FAILED=0

# 1. Operating System & Architecture
echo -n "[1/7] Checking Operating System & Architecture... "
OS=$(uname -s)
ARCH=$(uname -m)
if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    echo "OK (macOS Apple Silicon $ARCH)"
else
    echo "WARNING: Expected macOS arm64, found $OS $ARCH"
fi

# 2. Unified Memory Check
echo -n "[2/7] Checking Unified Memory... "
MEM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo "0")
MEM_GB=$(( MEM_BYTES / 1024 / 1024 / 1024 ))
if [ "$MEM_GB" -ge 16 ]; then
    echo "OK (${MEM_GB} GB Unified Memory)"
else
    echo "WARNING: ${MEM_GB} GB detected (16 GB baseline recommended)"
fi

# 3. Python Environment
echo -n "[3/7] Checking Python Environment... "
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
    echo "OK (Python $PY_VER at $(which python3))"
else
    echo "FAILED: python3 not found"
    FAILED=1
fi

# 4. Ollama Service Status
echo -n "[4/7] Checking Ollama Service... "
if curl -s -f http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    OLLAMA_VER=$(curl -s http://127.0.0.1:11434/api/version | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
    echo "OK (Ollama $OLLAMA_VER running on 127.0.0.1:11434)"
else
    echo "FAILED: Ollama not reachable on http://127.0.0.1:11434"
    echo "        Run 'brew services start ollama' or 'ollama serve'"
    FAILED=1
fi

# 5. Primary Model Verification (qwen3.5:9b)
echo -n "[5/7] Checking Primary Model (qwen3.5:9b)... "
if curl -s http://127.0.0.1:11434/api/tags 2>/dev/null | grep -q "qwen3.5:9b"; then
    echo "OK (qwen3.5:9b verified present)"
else
    echo "FAILED: qwen3.5:9b not found in Ollama library"
    echo "        Run 'ollama pull qwen3.5:9b'"
    FAILED=1
fi

# 6. Swift & Developer Tools
echo -n "[6/7] Checking Swift Compiler... "
if command -v swift >/dev/null 2>&1; then
    SWIFT_VER=$(swift --version | head -n 1)
    echo "OK ($SWIFT_VER)"
else
    echo "WARNING: Swift command not found (needed for native macOS GUI)"
fi

# 7. SQLite & Storage Directory
echo -n "[7/7] Checking Storage and Permissions... "
mkdir -p data
if [ -w data ]; then
    echo "OK (data/ directory writable)"
else
    echo "FAILED: data/ directory is not writable"
    FAILED=1
fi

echo "=================================================="
if [ "$FAILED" -eq 0 ]; then
    echo "🎉 ALL SYSTEM CHECKS PASSED. Ready for Carbon."
    exit 0
else
    echo "❌ DIAGNOSTICS DETECTED ISSUES. Review messages above."
    exit 1
fi
