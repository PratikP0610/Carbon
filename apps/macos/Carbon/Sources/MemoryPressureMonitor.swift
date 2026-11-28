import Dispatch
import Foundation

/// Mach kernel memory pressure levels mapped to Carbon CMS levels (1, 2, 4).
public enum CarbonMemoryPressureLevel: Int, Equatable, Sendable {
    case normal = 1
    case warning = 2
    case critical = 4

    /// Map a raw Darwin DispatchSource.MemoryPressureEvent mask conservatively (highest severity wins).
    public static func from(dispatchEvent event: DispatchSource.MemoryPressureEvent) -> CarbonMemoryPressureLevel {
        if event.contains(.critical) {
            return .critical
        }
        if event.contains(.warning) {
            return .warning
        }
        return .normal
    }
}

/// Deterministic debounce and de-escalation hysteresis state machine for Mach memory pressure events.
@MainActor
public final class MemoryPressureDebouncer {
    public let debounceInterval: TimeInterval
    public let recoveryHysteresisInterval: TimeInterval

    public private(set) var lastReportedLevel: CarbonMemoryPressureLevel = .normal
    public private(set) var pendingLevel: CarbonMemoryPressureLevel? = nil
    public private(set) var emittedHistory: [CarbonMemoryPressureLevel] = []

    private let nowProvider: @Sendable () -> Date
    private let onEmit: @Sendable (CarbonMemoryPressureLevel) -> Void
    private var scheduledWorkTask: Task<Void, Never>? = nil

    public init(
        debounceInterval: TimeInterval = 0.5,
        recoveryHysteresisInterval: TimeInterval = 10.0,
        nowProvider: @escaping @Sendable () -> Date = { Date() },
        onEmit: @escaping @Sendable (CarbonMemoryPressureLevel) -> Void
    ) {
        self.debounceInterval = max(0.0, debounceInterval)
        self.recoveryHysteresisInterval = max(self.debounceInterval, recoveryHysteresisInterval)
        self.nowProvider = nowProvider
        self.onEmit = onEmit
    }

    deinit {
        scheduledWorkTask?.cancel()
    }

    /// Process a raw pressure level and return the required settle delay before emission (nil if redundant).
    @discardableResult
    public func ingest(_ level: CarbonMemoryPressureLevel, scheduleTimer: Bool = true) -> TimeInterval? {
        scheduledWorkTask?.cancel()
        scheduledWorkTask = nil

        if level == lastReportedLevel {
            pendingLevel = nil
            return nil
        }

        pendingLevel = level
        let holdInterval: TimeInterval = (level.rawValue < lastReportedLevel.rawValue)
            ? recoveryHysteresisInterval
            : debounceInterval

        if holdInterval == 0.0 {
            commitPendingLevel()
            return 0.0
        }

        if scheduleTimer {
            scheduledWorkTask = Task { @MainActor [weak self] in
                let nanos = UInt64(holdInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: nanos)
                guard let self = self, !Task.isCancelled else { return }
                self.scheduledWorkTask = nil
                self.commitPendingLevel()
            }
        }
        return holdInterval
    }

    /// Deterministically flush the pending level if set (used by tests and explicit flush paths).
    @discardableResult
    public func commitPendingLevel() -> CarbonMemoryPressureLevel? {
        scheduledWorkTask?.cancel()
        scheduledWorkTask = nil
        guard let next = pendingLevel else { return nil }
        pendingLevel = nil
        guard next != lastReportedLevel else { return nil }
        lastReportedLevel = next
        emittedHistory.append(next)
        onEmit(next)
        return next
    }

    public func stop() {
        scheduledWorkTask?.cancel()
        scheduledWorkTask = nil
        pendingLevel = nil
    }
}

/// Native macOS DispatchSourceMemoryPressure observer reporting settled state transitions to Carbon backend.
public final class MemoryPressureMonitor: @unchecked Sendable {
    public typealias TokenProvider = @Sendable () async -> String?
    public typealias RequestSender = @Sendable (URLRequest) async throws -> (Data, URLResponse)

    private let baseURL: URL
    private let tokenProvider: TokenProvider
    private let requestSender: RequestSender
    private let debounceInterval: TimeInterval
    private let recoveryHysteresisInterval: TimeInterval

    private let monitorQueue = DispatchQueue(label: "ai.carbon.memory-pressure", qos: .utility)
    private let lock = NSLock()
    private var source: DispatchSourceMemoryPressure?
    private var debouncer: MemoryPressureDebouncer?
    private var isStarted: Bool = false

    public init(
        baseURL: URL = URL(string: "http://127.0.0.1:8765")!,
        debounceInterval: TimeInterval = 0.5,
        recoveryHysteresisInterval: TimeInterval = 10.0,
        tokenProvider: @escaping TokenProvider,
        requestSender: RequestSender? = nil
    ) {
        self.baseURL = baseURL
        self.debounceInterval = debounceInterval
        self.recoveryHysteresisInterval = recoveryHysteresisInterval
        self.tokenProvider = tokenProvider
        self.requestSender = requestSender ?? { request in
            try await URLSession.shared.data(for: request)
        }
    }

    deinit {
        stop()
    }

    /// Build authenticated loopback POST /api/v1/system/memory-pressure request.
    public static func buildRequest(
        baseURL: URL,
        level: CarbonMemoryPressureLevel,
        token: String?,
        sourceLabel: String = "macos_dispatch_source"
    ) -> URLRequest {
        let url = baseURL.appendingPathComponent("/api/v1/system/memory-pressure")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 3.0
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token = token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let body: [String: Any] = [
            "level": level.rawValue,
            "source": sourceLabel,
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        return request
    }

    /// Report a specific settled memory pressure level asynchronously to the loopback backend.
    public func postPressureLevel(_ level: CarbonMemoryPressureLevel) async {
        let token = await tokenProvider()
        let request = Self.buildRequest(baseURL: baseURL, level: level, token: token)
        _ = try? await requestSender(request)
    }

    private func activeDebouncerSnapshot() -> MemoryPressureDebouncer? {
        lock.withLock { debouncer }
    }

    /// Start native DispatchSourceMemoryPressure observation idempotently.
    @MainActor
    public func start() {
        let shouldStart = lock.withLock { () -> Bool in
            if isStarted { return false }
            isStarted = true
            return true
        }
        guard shouldStart else { return }

        let createdDebouncer = MemoryPressureDebouncer(
            debounceInterval: debounceInterval,
            recoveryHysteresisInterval: recoveryHysteresisInterval
        ) { [weak self] settledLevel in
            guard let self = self else { return }
            Task {
                await self.postPressureLevel(settledLevel)
            }
        }

        let dispatchSource = DispatchSource.makeMemoryPressureSource(
            eventMask: [.normal, .warning, .critical],
            queue: monitorQueue
        )
        dispatchSource.setEventHandler { [weak self, weak dispatchSource] in
            guard let self = self, let dispatchSource = dispatchSource else { return }
            let mapped = CarbonMemoryPressureLevel.from(dispatchEvent: dispatchSource.data)
            Task { @MainActor [weak self] in
                guard let self = self else { return }
                self.activeDebouncerSnapshot()?.ingest(mapped, scheduleTimer: true)
            }
        }

        lock.withLock {
            self.debouncer = createdDebouncer
            self.source = dispatchSource
        }

        dispatchSource.resume()
    }

    /// Stop monitoring and cancel pending debounce timers cleanly.
    public func stop() {
        lock.lock()
        let activeSource = source
        let activeDebouncer = debouncer
        source = nil
        debouncer = nil
        isStarted = false
        lock.unlock()

        activeSource?.cancel()
        if let activeDebouncer = activeDebouncer {
            Task { @MainActor in
                activeDebouncer.stop()
            }
        }
    }
}
