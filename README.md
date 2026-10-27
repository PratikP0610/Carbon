# Carbon 1.0.0

Carbon is a personal AI assistant engineered to run primarily on the user's own devices. Operating as an intelligent, persistent, voice-enabled layer on macOS, Carbon delivers high-performance local AI capabilities under strict user governance.

---

## 🌟 Key Highlights

- **Local-First & Private:** Local inference powered by Ollama on Apple Silicon unified memory. No mandatory cloud AI dependencies.
- **Single Model Authority:** The Carbon Model Supervisor (CMS) controls model lifecycle, automatic selection, and switching. Single active chat LLM concurrency prevents resource exhaustion on 16 GB unified memory Macs.
- **Adaptive Intelligence:** Decoupled reasoning levels (**Instant**, **Standard**, **Deep**) matched to task complexity.
- **Tiered Persistent Memory:** Working, episodic, semantic, and procedural memory backed by local SQLite persistence.
- **Controlled System Access:** Structured permission ladder (P0–P4) and autonomy levels (A0–A3). An LLM is never permitted unrestricted shell, filesystem, or network access.
- **Extensible Architecture:** Designed as a modular monolith in Python 3.12+ with a native SwiftUI macOS desktop interface and future Android device integration.

---

## 💻 System Requirements

- **Operating System:** macOS (macOS 14+ / macOS 15+ / macOS 27+ Apple Silicon ARM64)
- **Hardware:** Apple Silicon (M-series, baseline 16 GB unified memory)
- **Model Runtime:** [Ollama](https://ollama.ai) (verified running on `http://127.0.0.1:11434`)
- **Primary Model:** `qwen3.5:9b` (Q4_K_M GGUF)
- **Backend Environment:** Python 3.12+

---

## 📁 Repository Structure

```
carbon/
├── README.md                          # Project overview and entry point
├── LICENSE                            # MIT License
├── pyproject.toml                     # Python project packaging and QA configuration
├── .gitignore                         # Version control exclusions
├── .env.example                       # Environment configuration template
│
├── docs/                              # Comprehensive documentation suite
│   ├── product/                       # PRD and product specifications
│   ├── architecture/                  # Architecture, system design, contracts, ADRs
│   ├── security/                      # Threat models, permissions, privacy policies
│   ├── development/                   # Setup, testing, and contribution guides
│   └── benchmarks/                    # Benchmark methodology and performance targets
│
├── apps/                              # Client applications
│   └── macos/Carbon/                  # Native SwiftUI macOS desktop client
│
├── src/carbon/                        # Core backend package
│   ├── main.py                        # Backend application entry point
│   ├── config/                        # Configuration and settings management
│   ├── core/                          # Orchestrator, lifecycle, task coordination
│   ├── intelligence/                  # Intent classification, complexity, planner
│   ├── model_control/                 # CMS, model registry, switcher, Ollama provider
│   ├── memory/                        # SQLite persistence, tiered memory engine
│   ├── voice/                         # STT, TTS, audio pipeline interfaces
│   ├── governance/                    # Permissions, autonomy constraints, audit logs
│   ├── devices/                       # Local Mac host and future Android device abstractions
│   ├── tools/                         # Validated tool execution framework
│   └── interfaces/                    # Loopback FastAPI and IPC schemas
│
├── tests/                             # Automated test suite
│   ├── unit/                          # Unit tests for individual subsystems
│   ├── integration/                   # Subsystem communication and API tests
│   ├── security/                      # Permission and governance boundary tests
│   └── performance/                   # Latency and resource utilization benchmarks
│
├── benchmarks/                        # Dedicated benchmark test harnesses
├── scripts/                           # Operational scripts (diagnostics, benchmark)
└── data/                              # Local database and runtime storage (git-ignored)
```

---

## 🚀 Quickstart

### 1. Environment Verification
Verify Ollama is installed and running with `qwen3.5:9b`:
```bash
brew services start ollama
curl -s http://127.0.0.1:11434/api/tags
```

### 2. Python Virtual Environment Setup
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Run Diagnostics
```bash
./scripts/diagnostics.sh
```

### 4. Run Automated Tests
```bash
pytest
```

---

## 📜 Documentation

- [Product Requirements Document (PRD)](docs/product/PRD.md)
- [Architecture Overview](docs/architecture/overview.md)
- [System Design](docs/architecture/system-design.md)
- [Component Contracts](docs/architecture/component-contracts.md)
- [Data Flow & Lifecycle](docs/architecture/data-flow.md)
- [Architecture Decision Records (ADRs)](docs/architecture/adr/)
- [Security Threat Model](docs/security/threat-model.md)
- [Permission Architecture](docs/security/permissions.md)
- [Privacy Policy](docs/security/privacy.md)
- [Benchmark Methodology](docs/benchmarks/methodology.md)

---

## ⚖️ Governance and Non-Negotiable Rules

1. **Controlled Autonomy:** LLMs are strictly workers. They never execute arbitrary shell commands, grant permissions, or alter security policies.
2. **Local Loopback Only:** Carbon's local API binds strictly to `127.0.0.1` and is never exposed to public networks.
3. **Auditability:** Consequential actions and permission decisions are recorded in a tamper-resistant local audit log.
