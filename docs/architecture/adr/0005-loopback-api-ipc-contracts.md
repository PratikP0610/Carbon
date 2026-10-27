# ADR-0005: Loopback REST and WebSocket/SSE IPC Contracts

## Status
Approved

## Context
The native macOS GUI (SwiftUI) must communicate with the Carbon backend (Python 3.12+).
The interface must deliver:
1. Low latency command/response handling.
2. Real-time token streaming for LLM generation.
3. Asynchronous push events (permission approval requests, system status, speech events).
4. Strict local isolation to prevent unauthorized external network access.

## Decision
Carbon's backend exposes an asynchronous local API using **FastAPI** bound exclusively to loopback IPv4 (`127.0.0.1:8765`).

1. **Network Binding:** Binds strictly to `127.0.0.1` (never `0.0.0.0`). Requests from any non-loopback IP are dropped immediately.
2. **REST Endpoints:** Synchronous operations, health queries, model listings, task lifecycle control (`POST /api/v1/tasks`, `GET /api/v1/health`, `GET /api/v1/models`).
3. **WebSocket Stream:** Bi-directional real-time stream (`/ws/chat` and `/ws/events`) for streaming inference tokens, voice buffers, cancellation signals, and authorization prompts.
4. **Local Auth Token:** During backend startup, an ephemeral high-entropy bearer token is generated and stored in a secure local runtime file (`data/.carbon_token`, mode `0600`) or passed via environment variable, ensuring only the local client can communicate with the backend.

## Consequences
### Positive:
- **Maximum security:** Impossible for LAN or public internet actors to access Carbon API.
- **High throughput & responsiveness:** WebSockets eliminate HTTP polling overhead for token streaming.
- **Clean platform separation:** SwiftUI client remains lightweight and decoupled from Python internals.

### Negative / Trade-offs:
- Requires managing WebSocket reconnects and heartbeat pings between frontend and backend.
