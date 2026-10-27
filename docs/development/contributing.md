# Contributing to Carbon

## 1. Development Process

Carbon follows a rigorous architecture-first development process governed by the PRD.

1. **Architecture Decisions:** Any modification to subsystem boundaries, concurrency models, or security policies requires an Architecture Decision Record (ADR) in `docs/architecture/adr/`.
2. **Interface Preservation:** Do not break existing contracts in `src/carbon/interfaces/` without prior design alignment.
3. **Local-First & Safety Invariants:** All code must respect the 12 non-negotiable development rules specified in PRD Section 12.

---

## 2. Commit & Code Standards

- Python code must use Python 3.12+ features, type annotations, and Pydantic models for structured I/O.
- Keep commits focused and atomic with descriptive commit messages.
- Always include corresponding unit/integration tests alongside new features.
