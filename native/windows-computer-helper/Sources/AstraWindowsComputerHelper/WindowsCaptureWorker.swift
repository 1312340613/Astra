import Foundation
import AstraAppshotCore
import AstraWindowsNative

/// A single serial owner for blocking capture/publication/cleanup. Cancellation
/// rejects delivery immediately; an in-flight filesystem call retains its owner
/// until it returns and cleanup finishes. No additional capture starts while any
/// cancelled work or cleanup remains. WGC/UIA calls themselves remain in the
/// existing deadline-controlled, job-owned child processes.
final class WindowsCaptureWorker: @unchecked Sendable {
    private let queue = DispatchQueue(label: "astra.appshot.windows.capture")
    private let lock = NSLock()
    private var jobs = 0
    private var stopped = false
    let path: String
    init(path: String) { self.path = path }
    var isIdle: Bool { lock.lock(); defer { lock.unlock() }; return jobs == 0 }
    func stopAccepting() { lock.lock(); stopped = true; lock.unlock() }
    private func reserve() throws {
        lock.lock(); defer { lock.unlock() }
        guard !stopped, jobs == 0 else { throw AppshotBrokerError.captureBusy }
        jobs = 1
    }
    private func finished() { lock.lock(); jobs -= 1; lock.unlock() }
    func cleanup(_ files: WindowsAppshotArtifacts) {
        lock.lock(); jobs += 1; lock.unlock()
        queue.async { [self] in
            defer { finished() }
            // A changed/replaced file is intentionally preserved; its old proof
            // must never authorize deleting another file occupying that name.
            if !files.destroy() { stopAccepting() }
        }
    }
    @MainActor func start(_ binding: AppshotRecipient<AppshotWindowsProcess>,
        expectedSource: ASWindow? = nil) throws -> WindowsCaptureOperation {
        try reserve()
        let source: ASWindow
        do {
            source = try WindowsAppshot.target()
            if let expectedSource {
                guard source.handle == expectedSource.handle, source.process.pid == expectedSource.process.pid,
                    source.process.created == expectedSource.process.created else {
                    throw AppshotCaptureError.sourceWindowChanged
                }
            }
        } catch { finished(); throw error }
        let state = State(source: source, binding: binding)
        let reservation = AppshotRegistry<AppshotWindowsProcess>.captureReservationBytes
        queue.async { [self] in
            defer { finished() }
            var publicationBegan = false
            do {
                _ = try state.deadline.remaining()
                let directory = try WindowsPrivateDirectory(path: path)
                try directory.checkQuota(reserve: reservation, reserveEntries: 3)
                let capture = try WindowsAppshot.capture(source, deadline: state.deadline)
                publicationBegan = true
                let files = try WindowsAppshotArtifacts.publish(directory: directory, source: source,
                    capture: capture, recipient: binding, deadline: state.deadline)
                if !state.finish(.success(files)) { stopAccepting() }
            } catch {
                // A failed publication can have best-effort cleanup without a
                // final receipt. Preserve any residue and require fresh custody
                // validation instead of producing more files in this lifetime.
                if publicationBegan || error is WindowsArtifactError { stopAccepting() }
                _ = state.finish(.failure(error))
            }
        }
        return Operation(worker: self, state: state)
    }
    private final class State: @unchecked Sendable {
        let deadline = AppshotCaptureDeadline()
        let source: ASWindow
        let binding: AppshotRecipient<AppshotWindowsProcess>
        private let lock = NSLock()
        private var cancelled = false
        private var result: Result<WindowsAppshotArtifacts, Error>?
        init(source: ASWindow, binding: AppshotRecipient<AppshotWindowsProcess>) { self.source = source; self.binding = binding }
        func finish(_ value: Result<WindowsAppshotArtifacts, Error>) -> Bool {
            lock.lock()
            if cancelled || (try? deadline.remaining()) == nil {
                lock.unlock()
                if case .success(let files) = value { return files.destroy() }
                return true
            }
            result = value
            lock.unlock()
            return true
        }
        func take() -> Result<WindowsAppshotArtifacts, Error>? {
            lock.lock(); defer { lock.unlock() }
            let value = result; result = nil
            return value
        }
        func cancel() -> WindowsAppshotArtifacts? {
            deadline.cancel()
            lock.lock(); defer { lock.unlock() }
            cancelled = true
            defer { result = nil }
            if case .success(let files) = result { return files }
            return nil
        }
    }
    @MainActor private final class Operation: WindowsCaptureOperation {
        let worker: WindowsCaptureWorker
        let state: State
        init(worker: WindowsCaptureWorker, state: State) { self.worker = worker; self.state = state }
        deinit { if let files = state.cancel() { worker.cleanup(files) } }
        func cancel() { if let files = state.cancel() { worker.cleanup(files) } }
        func poll() -> Result<AppshotCapturedArtifact, Error>? {
            guard let result = state.take() else { return nil }
            switch result {
            case .failure(let error): return .failure(error)
            case .success(let files):
                do {
                    _ = try state.deadline.remaining()
                    var source = state.source
                    guard as_window_matches(&source, 1) != 0 else { throw AppshotCaptureError.sourceWindowChanged }
                    guard try WindowsAppshot.identity(UInt32(state.binding.identity.pid)) == state.binding.identity else {
                        throw AppshotBrokerError.recipientDisconnected
                    }
                    let worker = worker
                    let artifact = AppshotCapturedArtifact(manifestPath: files.manifestPath,
                        byteCount: files.files.reduce(0) { $0 + Int($1.proof.size) }) { worker.cleanup(files) }
                    return .success(artifact)
                } catch { worker.cleanup(files); return .failure(error) }
            }
        }
    }
}
