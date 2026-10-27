# ADR-0007: Secret Storage Architecture — macOS Keychain Services vs. POSIX 0600 File Storage

## Status
Approved (Architecture Specification & Migration Plan)

## Context
During initial Phase 0/1 prototyping, Carbon implemented an ephemeral bearer token stored in a local filesystem path (`data/.carbon_token`) with POSIX `0600` (`-rw-------`) permissions.

The PRD (Section 8) mandates that production secret storage should integrate with the native **macOS Keychain** (`Security.framework`). The user development directive requires a rigorous technical comparison between POSIX `0600` file storage and macOS Keychain Services, detailing:
- Exact security differences and threat boundaries.
- Implementation implications across the Swift frontend and Python backend.
- Migration appropriateness and timeline.
- Token rotation and restart behavior.

---

## 1. Security Analysis: POSIX 0600 vs. macOS Keychain

| Dimension | POSIX 0600 File (`data/.carbon_token`) | macOS Keychain (`Security.framework`) | Security Assessment |
|---|---|---|---|
| **Threat Boundary** | Isolates against *other* Unix user accounts on the machine. | Isolates against *all unauthorized processes*, even those running under the same user UID. | **Keychain is vastly superior.** POSIX 0600 provides zero isolation against third-party user processes, rogue IDE extensions, npm/pip scripts, or malware running as the user. |
| **Cryptographic Protection** | Plaintext on disk. Vulnerable to unallocated block inspection or volume dumps if FileVault is unlocked. | Encrypted at rest via hardware-backed AES-256 keys (Apple Silicon Secure Enclave / login keychain). | **Keychain ensures hardware encryption.** |
| **Access Control Lists (ACL)** | None. Any process executing under `UID=501` can read the file via standard POSIX `read()`. | Enforces code-signing and entitlement checks. Only binaries signed with the matching bundle ID (`com.antigravity.carbon`) can read without an OS prompt. | **Keychain enforces binary identity.** |
| **Leakage in Backups** | Included in unencrypted local backups or snapshots unless explicitly excluded. | Excluded from plaintext file backups; protected by system keychain backup encryption policies. | **Keychain prevents backup leaks.** |

> [!CAUTION]
> **POSIX 0600 is NOT equivalent to macOS Keychain protection.** POSIX permissions only protect multi-user environments. On a single-user developer MacBook Air, every application executed by the user runs under the same UID (`pratikpradeshi`), meaning any untrusted script or local software can read `data/.carbon_token` without restriction.

---

## 2. Implementation Implications

### 2.1 Native macOS SwiftUI Client (Swift 6)
- **First-Class Support:** Swift has direct, native, zero-dependency access to Keychain Services via `import Security`.
- **API Primitives:**
  - `SecItemAdd`: Atomically creates the token item (`kSecClassGenericPassword`, service: `CarbonLocalAPI`, account: `CarbonDesktopClient`).
  - `SecItemCopyMatching`: Atomically retrieves the token on boot.
  - `SecItemUpdate`: Atomically updates the token upon rotation.
  - `SecItemDelete`: Removes the token upon user reset.

### 2.2 Python 3.12 Backend Orchestrator
- **Ecosystem Constraints:** The standard Python library does not expose macOS `Security.framework` bindings out of the box.
- **Integration Options Evaluated:**
  1. *Third-Party Python Dependencies (`keyring`, `pyobjc`):* Violates the Phase 0/1 directive against installing additional dependencies.
  2. *Subprocess `/usr/bin/security`:* Unsafe. Passing credentials via command-line flags (`security add-generic-password -w <secret>`) leaks tokens into process arguments inspectable via `ps aux`.
  3. *Dual-Custody Handshake Architecture (Selected):*
     - The **native macOS client (SwiftUI)** serves as the primary custodian of the secret, storing and rotating the token in Keychain.
     - When launching the Python backend process, the Swift client injects the token via environment variable (`CARBON_API_TOKEN`) or a secure anonymous pipe, eliminating plaintext token files on disk entirely.

---

## 3. Migration Assessment & Timeline

### Current State (Phase 1 Baseline):
- `data/.carbon_token` (POSIX `0600`) is retained strictly as a **developer bootstrap and headless testing mechanism**, enabling standalone tests (`pytest`, `TestClient`) to run without interacting with macOS GUI Keychain dialogs.

### Migration Path:
- **Phase 1 Production Client:** The Swift client in `apps/macos/Carbon` will implement `KeychainHelper.swift` using `Security.framework`.
- **Backend Ingestion:** In `src/carbon/config/settings.py`, `CARBON_API_TOKEN` passed by the Swift host overrides the disk file.
- **Disk Elimination:** In production mode (`CARBON_ENV=production`), file-based token generation will be disabled completely; the backend will demand token delivery via IPC pipe or environment, leaving zero disk footprint.

---

## 4. Token Rotation and Restart Behavior

### Under macOS Keychain Services:
1. **Rotation:** Client triggers `POST /api/v1/auth/rotate-token`. Backend generates a new 256-bit token, returns it over the authenticated loopback connection, and the Swift client updates the Keychain item via `SecItemUpdate`.
2. **Application Restarts:** When the application relaunches, the Swift client reads the persistent secret from Keychain via `SecItemCopyMatching` and presents it to the backend, maintaining uninterrupted authentication across reboot cycles.

### Under Current POSIX 0600 Fallback:
1. **Rotation:** `POST /api/v1/auth/rotate-token` atomically overwrites `data/.carbon_token` with `O_CREAT | O_WRONLY | O_TRUNC` and `os.chmod(0o600)`.
2. **Application Restarts:** The backend reads `data/.carbon_token` on startup via `_get_or_create_token()`.
