# CARBON 1.0.0
## Product Requirements Document (PRD) and Technical Development Specification

**Product:** Carbon  
**Version:** 1.0.0  
**Document type:** Product Requirements Document + Technical Development Brief  
**Initial platform:** macOS  
**Future platform:** Android  
**Primary development agent:** Google Antigravity  
**Status:** Approved for architecture and implementation planning

---

# 1. Product Introduction

Carbon is a personal AI assistant designed to operate primarily on the user's own devices.

It is not intended to be just another chatbot. Its purpose is to become an intelligent, persistent, voice-enabled and controlled personal computing assistant.

Carbon should be able to:

- Understand natural-language instructions.
- Reason and respond intelligently.
- Remember useful information across conversations.
- Automatically select the most appropriate AI model.
- Switch between approved models when required.
- Interact with the macOS environment with user authorization.
- Understand voice commands and respond through speech.
- Execute approved tasks through controlled tools.
- Work locally without depending unnecessarily on cloud services.
- Eventually communicate with a connected Android device.

Carbon must prioritize speed, reliability, privacy, security and efficient resource utilization.

## 1.1 Product Vision

Build a local-first AI assistant that acts as an intelligent layer between the user and their devices.

The long-term objective is for Carbon to understand the user's intent, plan appropriate actions, use approved tools, learn useful preferences and complete tasks while remaining under the user's control.

## 1.2 Core Design Principles

1. Local-first: Prefer local inference and local data processing.
2. Low latency: Minimize unnecessary processing and model-loading delays.
3. Adaptive intelligence: Use appropriate reasoning depth for each task.
4. Controlled autonomy: Never allow an AI model unrestricted system access.
5. Persistent memory: Preserve useful context across sessions.
6. Modular architecture: Each subsystem must have clearly defined responsibilities.
7. Resource awareness: Operate efficiently on a Mac with 16 GB unified memory.
8. Transparency: Record important actions and explain failures.
9. Extensibility: Support future providers, models and connected devices.
10. User authority: The user retains control over permissions and consequential actions.

---

# 2. Target Platform and Hardware

## 2.1 Primary development environment

Carbon 1.0.0 must initially target:

- Apple MacBook Air M5
- 16 GB unified memory
- macOS
- Apple Silicon ARM64 architecture

The application must be optimized for Apple Silicon.

## 2.2 Existing AI environment

The initial model environment is:

- Runtime: Ollama
- Primary model: Qwen 3.5 9B

The implementation must inspect the actual installed model identifier and runtime availability rather than assuming a particular tag or model configuration.

Llama 3.1 8B is not part of the active model configuration.

## 2.3 Future device integration

The Samsung Galaxy S23 FE will be considered a connected Android node in a later development phase.

It must not delay or complicate the initial Mac implementation.

---

# 3. Product Scope

## 3.1 Included in Carbon 1.0.0

- Native macOS application interface.
- Local AI inference integration.
- Carbon Core orchestration.
- Carbon Model Supervisor (CMS).
- Automatic model selection and switching.
- Model registry and model governance.
- Adaptive reasoning.
- Persistent local memory.
- Text-based interaction.
- Voice input and speech output.
- Controlled macOS application interaction.
- Permission and approval system.
- Tool registry and execution framework.
- Logging, diagnostics and performance benchmarks.
- Basic controlled autonomy.

## 3.2 Excluded from the initial release

- Full Android application.
- Unrestricted computer control.
- Unrestricted terminal execution.
- Autonomous financial transactions.
- Silent file deletion or modification.
- Automatic installation of arbitrary models.
- Mandatory cloud AI dependency.
- Multi-model concurrent chat inference.

These exclusions may be reconsidered through future architecture decisions.

---

# 4. Functional Requirements

## FR-001: Natural-Language Interaction

Carbon must accept user instructions through a text interface.

It must:
- Understand ordinary conversational requests.
- Maintain conversation context.
- Provide responses in a readable format.
- Handle errors without crashing.
- Support cancellation of active requests.

## FR-002: Intelligence Engine

Carbon must have an Intelligence Engine responsible for:

- Understanding task intent.
- Estimating task complexity.
- Determining required reasoning depth.
- Preparing structured task requests.
- Validating model outputs.
- Returning results to Carbon Core.

Reasoning levels:

**Instant:** Simple factual or conversational tasks.

**Standard:** Explanations, comparisons and ordinary planning.

**Deep:** Complex analysis, debugging and multi-step problem solving.

The reasoning level and model selection must remain separate decisions.

## FR-003: Model Control Plane

CMS is the model-management authority.

Confirmed decisions:

- Model switching: Automatic.
- Model authority: CMS.
- Model concurrency: Single active chat LLM.

CMS must manage:

- Model registration.
- Model availability.
- Capability metadata.
- Model selection.
- Model switching.
- Model health.
- Resource requirements.
- Fallback selection.
- Model governance.
- Model lifecycle.

Individual models must not be permitted to change their own authority or execute system tools directly.

## FR-004: Persistent Memory

Carbon must store useful information locally.

Memory categories:

1. Working memory: Active task context.
2. Episodic memory: Relevant past interactions and events.
3. Semantic memory: Stable facts and knowledge.
4. Procedural memory: Reusable instructions and workflows.

Initial storage architecture:

- SQLite for structured persistence.
- A pluggable retrieval interface for semantic search.
- Optional embedding-based retrieval in a later implementation stage.

Memory must support:
- Adding information.
- Retrieving relevant information.
- Updating information.
- User-directed forgetting.
- Source and timestamp tracking.
- Memory access controls.

The system must not automatically store every conversation as permanent memory.

## FR-005: Voice Engine

Carbon should support:

- Microphone input.
- Speech-to-text.
- Text-to-speech.
- Interruptible speech output.
- Voice activity handling.
- Conversation cancellation.

Initial technologies to evaluate:

- Whisper.cpp for local speech recognition.
- Kokoro for local speech synthesis.

These are proposed dependencies, not mandatory installation instructions. Antigravity must benchmark and verify compatibility before adopting them.

Voice must be implemented as an independent subsystem.

## FR-006: Controlled Computer Access

Carbon must interact with macOS through approved tools.

Initial capabilities may include:

- Detecting available applications.
- Opening approved applications.
- Reading explicitly approved files.
- Retrieving approved system information.
- Performing reversible operations.

Every tool must have:
- A unique identifier.
- A description.
- A validated input schema.
- A permission classification.
- An execution handler.
- An audit record.
- A timeout and error-handling policy.

The LLM must request actions. Carbon Governance must authorize them. Carbon Core must control their execution.

## FR-007: Governance and Permissions

Permission levels:

- P0: Basic conversational operations.
- P1: Read approved information.
- P2: Reversible actions, including approved application launching.
- P3: Important modifications or external submissions; explicit confirmation required.
- P4: Sensitive, destructive or high-impact operations; strong confirmation or prohibition.

Autonomy levels:

- A0: Observe.
- A1: Assist and recommend.
- A2: Execute approved actions.
- A3: Bounded autonomy within explicitly defined constraints.

Default behavior must be conservative.

## FR-008: Device Management

The Device Engine must provide a future-compatible interface for connected devices.

Version 1.0.0 will implement the Mac device layer first.

Future Android integration must support authenticated pairing, encrypted communication, explicit capabilities and permission management.

---

# 5. Technical Architecture

Carbon must begin as a modular monolith.

Do not introduce microservices in version 1.0.0 without a demonstrated technical requirement.

## 5.1 Architecture layers

**Layer 1 — User Interface**
- Native macOS interface.
- Conversation display.
- Input controls.
- Permission prompts.
- System status.

**Layer 2 — Carbon Core**
- Orchestration.
- Task management.
- Lifecycle.
- Event handling.
- Execution coordination.

**Layer 3 — Intelligence and Model Control**
- Intelligence Engine.
- Complexity assessment.
- CMS.
- Model registry.
- Router.
- Switcher.
- Resource manager.
- Model governance.

**Layer 4 — Functional Engines**
- Memory Engine.
- Voice Engine.
- Governance Engine.
- Device Engine.

**Layer 5 — Infrastructure**
- Ollama provider.
- SQLite.
- Approved system integrations.
- Logging.
- Diagnostics.

## 5.2 Authority model

Carbon Core retains final system authority.

CMS controls model selection and model lifecycle within the policies established by Core.

Individual LLMs are workers only.

An LLM must never:
- Directly execute shell commands.
- Modify Carbon's security policy.
- Grant itself permissions.
- Register arbitrary models.
- Access unrestricted user files.
- Bypass user approval requirements.

## 5.3 Model switching workflow

1. Receive task from Carbon Core.
2. Determine required capabilities.
3. Ask CMS to select an eligible model.
4. Check model health and resource requirements.
5. Prepare a controlled transition.
6. Release the previous chat model when required.
7. Load and verify the selected model.
8. Execute inference.
9. Return the result to Core.
10. Record the operation.

Only one chat LLM inference may execute at a time.

STT, TTS and embedding workloads must be managed separately and must still respect overall resource budgets.

---

# 6. Recommended Full Technology Stack

## 6.1 Native macOS application

**Swift + SwiftUI**

Responsibilities:
- Native user interface.
- macOS application lifecycle.
- Microphone and audio permissions.
- Native notifications.
- Application menus.
- macOS-specific integration.

The interface communicates with the Carbon backend through a documented local interface.

## 6.2 Backend and orchestration

**Python 3.12+**

Responsibilities:
- Carbon Core.
- Intelligence Engine.
- CMS.
- Memory Engine.
- Governance.
- Voice orchestration.
- Tool execution.
- Device abstractions.

Use type annotations, structured validation, asynchronous I/O where useful and clear interface boundaries.

## 6.3 Local application API

**FastAPI + Pydantic**

Responsibilities:
- Local communication between SwiftUI and Python.
- Request and response validation.
- Streaming responses.
- Health endpoints.
- Controlled event delivery.

The API must bind to loopback only by default, use authenticated local communication where appropriate, and must not be exposed to the public internet.

WebSocket or Server-Sent Events may be used for streaming where justified.

## 6.4 AI model runtime

**Ollama**

Responsibilities:
- Local model inference.
- Model availability checks.
- Model loading and unloading through supported interfaces.
- Provider-level health checks.

Use a provider abstraction so Carbon is not permanently tied to Ollama.

## 6.5 Primary model

**Qwen 3.5 9B**

The initial approved primary model.

Do not assume that the model can perform every task equally well. Capabilities must be tested.

## 6.6 Database and persistence

**SQLite**

Responsibilities:
- Conversation metadata.
- Memory records.
- Model registry metadata.
- User preferences.
- Audit logs.
- Task state.

Use schema migrations and database versioning.

## 6.7 Memory retrieval

Initial implementation:
- SQLite-backed retrieval interface.

Future evaluation:
- Local embedding model.
- Vector search implementation.
- Hybrid keyword and semantic retrieval.

Avoid introducing a large database service unnecessarily.

## 6.8 Voice

Evaluate:
- Whisper.cpp for STT.
- Kokoro for TTS.

Use independent interfaces so these engines can be replaced.

## 6.9 Security and validation

Evaluate:
- Pydantic for input validation.
- macOS Keychain for secrets.
- macOS permission APIs.
- Sandboxed execution for approved operations.
- Structured audit logging.

Do not store secrets in source files or ordinary configuration files.

## 6.10 Testing and quality assurance

- pytest for Python tests.
- Swift Testing or XCTest for native application tests.
- Ruff for Python linting and formatting.
- Pyright or an equivalent type checker.
- Integration tests for subsystem communication.
- Security tests for permissions and tool execution.
- Performance tests for inference, routing, switching and memory.

## 6.11 Development and version control

- Git.
- GitHub.
- Antigravity as the primary development environment.
- Markdown architecture documentation.
- Automated test and lint workflows.

Dependency versions must be pinned and documented after compatibility verification.

---

# 7. Performance Requirements

Performance is a first-class product requirement.

Initial engineering targets, subject to measurement:

| Metric | Initial target |
|---|---:|
| Simple local request first-token latency | P95 ≤ 2 seconds |
| Standard request first-token latency | P95 ≤ 4 seconds |
| Cached memory retrieval | P95 ≤ 100 ms |
| Core orchestration overhead | P95 ≤ 50 ms |
| Stability test | 60 minutes |

These targets apply under documented test conditions and are not guaranteed before benchmarking.

Track separately:
- Time to first token.
- Total response time.
- Model switching time.
- Model loading time.
- RAM usage.
- CPU utilization.
- Memory pressure.
- Failure rate.

Carbon must not run unnecessary LLM reasoning for simple tasks.

---

# 8. Proposed Repository Structure

carbon/
├── README.md
├── LICENSE
├── pyproject.toml
├── .gitignore
├── .env.example
│
├── docs/
│   ├── product/
│   │   └── PRD.md
│   ├── architecture/
│   │   ├── overview.md
│   │   ├── system-design.md
│   │   ├── component-contracts.md
│   │   └── data-flow.md
│   ├── security/
│   │   ├── threat-model.md
│   │   ├── permissions.md
│   │   └── privacy.md
│   ├── development/
│   │   ├── setup.md
│   │   ├── testing.md
│   │   └── contributing.md
│   └── benchmarks/
│       └── methodology.md
│
├── apps/
│   └── macos/
│       └── Carbon/
│
├── src/
│   └── carbon/
│       ├── main.py
│       ├── config/
│       ├── core/
│       ├── intelligence/
│       ├── model_control/
│       ├── memory/
│       ├── voice/
│       ├── governance/
│       ├── devices/
│       ├── tools/
│       └── interfaces/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── security/
│   └── performance/
│
├── benchmarks/
│   ├── inference/
│   ├── voice/
│   ├── memory/
│   └── orchestration/
│
├── scripts/
│   ├── diagnostics.sh
│   └── benchmark.sh
│
└── data/
    └── .gitkeep

---

# 9. Development Roadmap

## Phase 0 — Product and Architecture Foundation
- Review this PRD.
- Validate technical feasibility.
- Write system architecture.
- Define component interfaces.
- Write security and privacy policies.
- Establish performance methodology.
- Record architecture decisions.

## Phase 1 — Carbon Core
- Establish the application structure.
- Implement backend startup and shutdown.
- Implement task management.
- Establish the local API.
- Build a basic native Mac interface.
- Add diagnostics and structured logging.

## Phase 2 — Model Control
- Implement CMS.
- Implement model registry.
- Implement Ollama provider.
- Integrate Qwen 3.5 9B.
- Implement automatic model selection.
- Implement single-model concurrency.
- Implement switching and fallback.
- Add model governance.

## Phase 3 — Intelligence Engine
- Intent classification.
- Complexity assessment.
- Adaptive reasoning.
- Structured task planning.
- Output validation.

## Phase 4 — Memory Engine
- SQLite persistence.
- Memory categories.
- Retrieval.
- Memory updates.
- User-controlled forgetting.

## Phase 5 — Voice Engine
- STT.
- TTS.
- Audio pipeline.
- Interruptions.
- Voice performance testing.

## Phase 6 — Governance and Computer Access
- Permission engine.
- Tool registry.
- Approved macOS integrations.
- Confirmation system.
- Audit logging.

## Phase 7 — Controlled Autonomy
- Task planning.
- Multi-step execution.
- Checkpointing.
- Failure recovery.
- Bounded autonomous workflows.

## Phase 8 — Optimization
- Latency optimization.
- Memory optimization.
- Model switching optimization.
- Reliability testing.
- Security review.

## Phase 9 — Android Preparation
- Device protocol.
- Authentication.
- Encryption.
- Device capability model.
- Android node integration planning.

## Phase 10 — Release Preparation
- Functional testing.
- Security testing.
- Performance testing.
- Documentation.
- Installation and recovery procedures.
- Carbon 1.0.0 release candidate.

---

# 10. Definition of Done

Carbon 1.0.0 is ready for release only when:

1. The native Mac application launches reliably.
2. The local backend starts and shuts down cleanly.
3. Qwen 3.5 9B works through the approved provider.
4. CMS controls model selection and switching.
5. Single-chat-LLM concurrency is enforced.
6. Model failures have controlled recovery.
7. Persistent memory works across restarts.
8. Voice input and output work reliably.
9. Approved computer actions are permission-controlled.
10. No model can bypass governance.
11. Audit logging works.
12. Performance is measured and documented.
13. Security tests pass.
14. Documentation matches the implementation.

---

# 11. Responsibilities of Antigravity

Antigravity is the primary implementation agent.

It is responsible for:

- Inspecting the development environment.
- Establishing the repository.
- Implementing the architecture.
- Writing and maintaining source code.
- Creating tests.
- Running tests.
- Debugging failures.
- Maintaining technical documentation.
- Measuring performance.
- Reporting progress and blockers.
- Identifying architectural problems before they become expensive.

Antigravity must not silently change approved architecture decisions.

Any significant change must be documented as a proposed Architecture Decision Record (ADR) and presented for approval.

The user retains final product and architecture authority.

---

# 12. Non-Negotiable Development Rules

1. Do not rush into implementation before validating the architecture.
2. Do not install dependencies without explaining their purpose and compatibility.
3. Do not introduce unnecessary frameworks or services.
4. Do not create a microservice architecture for the initial release.
5. Do not give an LLM unrestricted shell or filesystem access.
6. Do not expose local APIs publicly.
7. Do not silently send private data to cloud providers.
8. Do not assume model loading or unloading behaviour; measure it.
9. Do not claim a feature is complete without tests.
10. Do not overwrite user files or existing configurations without approval.
11. Keep all significant decisions documented.
12. Optimize for the actual Mac hardware rather than theoretical performance.
