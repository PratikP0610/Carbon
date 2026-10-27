# ADR-0001: Modular Monolith Architecture

## Status
Approved

## Context
Carbon 1.0.0 is an intelligent personal computing assistant running locally on macOS (MacBook Air Apple Silicon, 16 GB unified memory). The architecture must coordinate multiple functional subsystems: UI, Core Orchestration, Model Control, Intelligence, Memory, Governance, Voice, and Tool Execution.

Architectural choices considered:
1. **Microservices architecture:** Decoupling subsystems into separate processes/containers communicating over gRPC or HTTP.
2. **Modular monolith:** A unified backend process organized into strict, decoupled Python modules with well-defined interface contracts, communicating in-process with asynchronous event coordination, paired with a native Swift GUI client over local loopback IPC.

## Decision
We select the **Modular Monolith** architecture for Carbon 1.0.0.

Subsystems (Core, Intelligence, CMS, Memory, Governance, Tools, Devices) reside in the same Python process (`carbon.*`), maintaining isolated interface boundaries (`Protocol` types) and communicating via typed async calls.

## Consequences
### Positive:
- **Low latency:** Zero IPC serialization overhead between Intelligence, Memory, Governance, and CMS.
- **Resource efficiency:** Minimal memory footprint without multiple Python interpreters or container runtimes running concurrently on a 16 GB unified memory Mac.
- **Simplicity:** Single backend process lifecycle, unified error recovery, and simplified debugging.

### Negative / Trade-offs:
- Subsystems share the Python GIL, necessitating asynchronous I/O (`asyncio`) and offloading heavy native compute (like STT/TTS) to C++ runtimes or isolated worker threads.
