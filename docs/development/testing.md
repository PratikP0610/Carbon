# Carbon Testing & Quality Assurance

## 1. Testing Philosophy

In accordance with Carbon Rule 9 (*"Do not claim a feature is complete without tests"*), every subsystem must be accompanied by unit, integration, and security verification tests.

Test suites are categorized into four tiers:
1. `tests/unit/`: Subsystem logic, models, schema validation, and state machines in isolation.
2. `tests/integration/`: End-to-end communication between Core, CMS, Ollama provider, and SQLite.
3. `tests/security/`: Permission gate enforcement, injection defense, and loopback socket restrictions.
4. `tests/performance/`: Latency, throughput, and memory footprint benchmarks.

---

## 2. Running Tests

### Run all tests
```bash
pytest
```

### Run with verbose output and failure stopping
```bash
pytest -v -x
```

### Run specific test suites
```bash
pytest tests/unit
pytest tests/integration
pytest tests/security
pytest tests/performance
```

---

## 3. Linting and Formatting

Carbon enforces strict code formatting and typing with `ruff`:

```bash
# Check formatting and lint rules
ruff check .

# Automatically apply safe fixes
ruff check --fix .

# Format code
ruff format .
```

---

## 4. Continuous Integration Checklist

Before marking any phase or task complete:
- [ ] All unit and integration tests pass.
- [ ] Ruff checks pass with zero warnings.
- [ ] Diagnostics script executes cleanly with zero failures.
- [ ] No hardcoded secrets or arbitrary shell execution paths exist.
