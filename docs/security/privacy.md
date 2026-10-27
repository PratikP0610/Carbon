# Carbon Privacy Policy & Data Handling

## 1. Local-First Privacy Guarantee

Carbon is designed from first principles as an on-device personal assistant. Your data, prompts, voice recordings, and system interactions belong entirely to you and reside exclusively on your machine.

---

## 2. Core Privacy Principles

1. **No Silent Cloud Transmissions:**
   - Carbon does not send prompts, conversation logs, or telemetry to external cloud servers.
   - All AI inference executes locally via Ollama on Apple Silicon unified memory.

2. **Zero Default Permanent Retention:**
   - Carbon does not blindly record every casual conversation into permanent memory.
   - Only explicitly identified semantic facts, user preferences, and structured procedures are persisted in long-term memory.

3. **User-Directed Forgetting:**
   - You have unconditional authority to inspect, export, or delete any record stored in Carbon's database.
   - Forgetting commands (e.g., "Forget what I told you about my medical appointment", "Delete all episodic logs from this morning") are executed immediately and permanently.

4. **Cryptographic Keychain for Credentials:**
   - Carbon never stores passwords, API tokens, or secrets in plaintext configuration files or SQLite tables.
   - All sensitive credentials must use the macOS Keychain Services API.

5. **Loopback Network Isolation:**
   - The backend API binds strictly to `127.0.0.1` and does not accept remote network connections.
