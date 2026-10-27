# Carbon Development Setup Guide

## 1. Prerequisites

- **Hardware:** Apple Silicon Mac (M-series, baseline 16 GB unified memory).
- **macOS:** macOS 14 Sonoma or higher (macOS 15/26/27 supported).
- **Python:** Python 3.12+ (installed via Homebrew or pyenv).
- **Xcode:** Xcode 16+ or Apple Command Line Tools (`xcode-select --install`).
- **Ollama:** Installed and configured with `qwen3.5:9b`.

---

## 2. Setting Up Ollama & Primary Model

1. Ensure Ollama is installed:
   ```bash
   brew install ollama
   ```
2. Start the Ollama background service:
   ```bash
   brew services start ollama
   ```
3. Pull the approved primary model:
   ```bash
   ollama pull qwen3.5:9b
   ```
4. Verify the model manifest is healthy:
   ```bash
   curl -s http://127.0.0.1:11434/api/tags | grep qwen3.5:9b
   ```

---

## 3. Python Backend Environment

1. Clone or navigate to the Carbon repository:
   ```bash
   cd ~/Carbon
   ```
2. Create and activate a Python 3.12 virtual environment:
   ```bash
   /opt/homebrew/bin/python3.12 -m venv .venv
   source .venv/bin/activate
   ```
3. Upgrade build tools and install dependencies in editable mode with development extras:
   ```bash
   pip install --upgrade pip
   pip install -e ".[dev]"
   ```
4. Copy the environment configuration template:
   ```bash
   cp .env.example .env
   ```

---

## 4. Environment Verification

Run the diagnostics script:
```bash
chmod +x scripts/diagnostics.sh
./scripts/diagnostics.sh
```

Execute the test suite:
```bash
pytest
```
