#include "AstraWindowsNative.h"
#include <windows.h>
#include <winternl.h>
#include <sddl.h>
#include <aclapi.h>
#include <array>
#include <deque>
#include <memory>
#include <string>
#include <vector>
#include <cstring>
#include <stdexcept>
#include <thread>
#include <atomic>
#include <utility>

namespace {
constexpr size_t maximumFrame = 65536, maximumOutput = 4 * maximumFrame;
constexpr size_t maximumClients = 16;
enum Error { invalid = 20, systemFailure = 21, unauthorized = 22, occupied = 23, timeout = 3, cancelled = 5 };
struct Failure { int code; };
void require(bool ok, int code = systemFailure) { if (!ok) throw Failure{code}; }
int failed() { try { throw; } catch (const Failure &e) { return e.code; } catch (...) { return systemFailure; } }
struct Handle {
    HANDLE value = INVALID_HANDLE_VALUE;
    Handle() = default;
    explicit Handle(HANDLE value) : value(value) {}
    ~Handle() { close(); }
    void close() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); value = INVALID_HANDLE_VALUE; }
    Handle(const Handle &) = delete;
    Handle &operator=(const Handle &) = delete;
    Handle(Handle &&other) noexcept : value(std::exchange(other.value, INVALID_HANDLE_VALUE)) {}
};
struct Local { void *value = nullptr; ~Local() { if (value) LocalFree(value); } };
void privatePipeACL(HANDLE pipe, const ASProcess &own) {
    Local descriptor, user;
    PACL dacl = nullptr; PSID owner = nullptr;
    require(GetSecurityInfo(pipe, SE_KERNEL_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
        &owner, nullptr, &dacl, nullptr, reinterpret_cast<PSECURITY_DESCRIPTOR *>(&descriptor.value)) == ERROR_SUCCESS);
    require(ConvertStringSidToSidA(own.sid, &user.value) && owner && EqualSid(owner, user.value));
    SECURITY_DESCRIPTOR_CONTROL control = 0; DWORD revision = 0;
    require(GetSecurityDescriptorControl(descriptor.value, &control, &revision)
        && (control & SE_DACL_PROTECTED) && dacl && dacl->AceCount == 2);
    bool foundUser = false, foundSystem = false;
    for (DWORD i = 0; i < dacl->AceCount; ++i) {
        void *raw = nullptr; require(GetAce(dacl, i, &raw) != 0);
        auto ace = static_cast<ACCESS_ALLOWED_ACE *>(raw);
        require(ace->Header.AceType == ACCESS_ALLOWED_ACE_TYPE && ace->Header.AceFlags == 0
            && ace->Mask == FILE_ALL_ACCESS && IsValidSid(&ace->SidStart));
        if (EqualSid(&ace->SidStart, user.value)) { require(!foundUser); foundUser = true; }
        else if (IsWellKnownSid(&ace->SidStart, WinLocalSystemSid)) { require(!foundSystem); foundSystem = true; }
        else { throw Failure{unauthorized}; }
    }
    require(foundUser && foundSystem);
}
ASBytes copy(const uint8_t *bytes, size_t size) {
    auto result = static_cast<uint8_t *>(malloc(size));
    require(result != nullptr);
    memcpy(result, bytes, size);
    return {result, size, 0};
}
bool same(const ASProcess &a, const ASProcess &b) {
    return a.pid == b.pid && a.created == b.created && !memcmp(a.sid, b.sid, sizeof(a.sid));
}
struct Peer {
    Handle handle;
    ASProcess identity{};
    explicit Peer(DWORD pid) : handle(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, pid)) {
        require(handle.value != nullptr, unauthorized);
        require(as_process_identity(pid, &identity) != 0, unauthorized);
        ASProcess own{};
        require(as_process_identity(as_current_pid(), &own) && !strcmp(identity.sid, own.sid), unauthorized);
        DWORD ours = 0, theirs = 0;
        require(ProcessIdToSessionId(as_current_pid(), &ours) && ProcessIdToSessionId(pid, &theirs)
            && ours == theirs, unauthorized);
        require(alive(), unauthorized);
    }
    bool alive() const {
        if (WaitForSingleObject(handle.value, 0) != WAIT_TIMEOUT || GetProcessId(handle.value) != identity.pid) return false;
        ASProcess now{};
        return as_process_identity(identity.pid, &now) && same(now, identity);
    }
};
std::wstring pipeName(const char *scope) {
    require(scope && strnlen(scope, 65) > 0 && strnlen(scope, 65) <= 64, invalid);
    std::wstring suffix;
    for (auto p = scope; *p; ++p) {
        require((*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '-', invalid);
        suffix += *p;
    }
    ASProcess own{}; DWORD session = 0;
    require(as_process_identity(as_current_pid(), &own) && ProcessIdToSessionId(as_current_pid(), &session));
    std::string sid(own.sid);
    return L"\\\\.\\pipe\\Astra.Appshot." + std::wstring(sid.begin(), sid.end()) + L"."
        + std::to_wstring(session) + L"." + suffix;
}
struct Operation {
    Handle event{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
    OVERLAPPED value{};
    bool pending = false;
    Operation() { require(event.value != nullptr); reset(); }
    void reset() { require(!pending); value = {}; value.hEvent = event.value; ResetEvent(event.value); }
    bool complete(HANDLE pipe, DWORD &count) {
        if (!pending) return true;
        if (GetOverlappedResult(pipe, &value, &count, FALSE)) { pending = false; return true; }
        require(GetLastError() == ERROR_IO_INCOMPLETE);
        return false;
    }
    void cancel(HANDLE pipe) {
        if (!pending) return;
        CancelIoEx(pipe, &value);
        DWORD ignored = 0;
        // Keep the OVERLAPPED and its buffer alive until cancellation completes.
        GetOverlappedResult(pipe, &value, &ignored, TRUE);
        pending = false;
    }
};
struct Channel {
    Handle pipe;
    Operation connect, read, write;
    std::unique_ptr<Peer> peer;
    std::array<uint8_t, 4096> input{};
    std::deque<std::vector<uint8_t>> output;
    size_t queued = 0, written = 0;
    ULONGLONG writeStarted = 0;
    uint64_t id = 0;
    bool connected = false;
    explicit Channel(Handle pipe) : pipe(std::move(pipe)) {
        require(this->pipe.value && this->pipe.value != INVALID_HANDLE_VALUE);
    }
    ~Channel() { cancel(); }
    void cancel() { connect.cancel(pipe.value); read.cancel(pipe.value); write.cancel(pipe.value); }
    void invalidate() {
        cancel(); pipe.close(); peer.reset(); connected = false;
        output.clear(); queued = written = 0;
    }
    void listen() {
        connected = false; peer.reset(); id = 0;
        connect.reset();
        if (!ConnectNamedPipe(pipe.value, &connect.value)) {
            auto error = GetLastError();
            require(error == ERROR_IO_PENDING || error == ERROR_PIPE_CONNECTED);
            connect.pending = error == ERROR_IO_PENDING;
        }
    }
    bool validPeer(bool server) {
        ULONG pid = 0;
        return peer && peer->alive()
            && (server ? GetNamedPipeClientProcessId(pipe.value, &pid) : GetNamedPipeServerProcessId(pipe.value, &pid))
            && peer->identity.pid == pid;
    }
    void enqueue(const uint8_t *bytes, size_t size) {
        require(bytes && size > 0 && size <= maximumFrame, invalid);
        require(size <= maximumOutput - queued && output.size() < 64, occupied);
        output.emplace_back(bytes, bytes + size); queued += size;
    }
    void flush() {
        if (write.pending) {
            require(GetTickCount64() - writeStarted < 3000, timeout);
            DWORD count = 0;
            if (!write.complete(pipe.value, count)) return;
            require(count > 0 && count <= output.front().size() - written);
            written += count;
        }
        for (unsigned turn = 0; turn < 4 && !output.empty(); ++turn) {
            if (written == output.front().size()) {
                queued -= output.front().size(); output.pop_front(); written = 0;
                if (output.empty()) break;
            }
            write.reset(); DWORD count = 0;
            if (!WriteFile(pipe.value, output.front().data() + written,
                static_cast<DWORD>(output.front().size() - written), &count, &write.value)) {
                require(GetLastError() == ERROR_IO_PENDING);
                write.pending = true; writeStarted = GetTickCount64(); return;
            }
            require(count > 0); written += count;
        }
    }
    ASBytes receive() {
        DWORD count = 0;
        if (read.pending) {
            if (!read.complete(pipe.value, count)) return {nullptr, 0, 0};
        } else {
            read.reset();
            if (!ReadFile(pipe.value, input.data(), static_cast<DWORD>(input.size()), &count, &read.value)) {
                require(GetLastError() == ERROR_IO_PENDING);
                read.pending = true;
                return {nullptr, 0, 0};
            }
        }
        require(count > 0 && count <= input.size());
        return copy(input.data(), count);
    }
};
// Node must spawn with stdio:'overlapped'. Reject synchronous inherited handles
// before issuing any I/O, otherwise ReadFile can block the relay owner thread.
Handle inheritedPipe(DWORD which) {
    HANDLE original = GetStdHandle(which), duplicate = INVALID_HANDLE_VALUE;
    require(original && original != INVALID_HANDLE_VALUE && GetFileType(original) == FILE_TYPE_PIPE, invalid);
    require(DuplicateHandle(GetCurrentProcess(), original, GetCurrentProcess(), &duplicate, 0, FALSE, DUPLICATE_SAME_ACCESS));
    Handle result(duplicate);
    using Query = NTSTATUS(NTAPI *)(HANDLE, PIO_STATUS_BLOCK, PVOID, ULONG, FILE_INFORMATION_CLASS);
    auto query = reinterpret_cast<Query>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtQueryInformationFile"));
    IO_STATUS_BLOCK status{}; ULONG mode = 0;
    require(query && query(result.value, &status, &mode, sizeof(mode), static_cast<FILE_INFORMATION_CLASS>(16)) == 0
        && !(mode & (0x10 | 0x20)), invalid); // FILE_SYNCHRONOUS_IO_ALERT/NONALERT
    return result;
}
struct Stdio {
    Peer parent{as_parent_pid(as_current_pid())};
    Channel input{inheritedPipe(STD_INPUT_HANDLE)}, output{inheritedPipe(STD_OUTPUT_HANDLE)};
    Stdio() {
        ASProcess own{};
        require(as_parent_pid(as_current_pid()) == parent.identity.pid
            && as_process_identity(as_current_pid(), &own) && parent.identity.created < own.created, unauthorized);
        require(valid(), unauthorized);
    }
    bool valid() const {
        // Windows creator PID is fixed for this process. Retain its live parent
        // object instead of enumerating all processes on every 2 ms I/O poll.
        if (!parent.alive()) return false;
        // libuv constructs both pipe ends in the parent before inheritance.
        // Prove the kernel-recorded peer on each handle, never a supplied PID.
        for (HANDLE pipe : {input.pipe.value, output.pipe.value}) {
            ULONG client = 0, server = 0;
            if (!GetNamedPipeClientProcessId(pipe, &client) || !GetNamedPipeServerProcessId(pipe, &server)
                || (client != parent.identity.pid && server != parent.identity.pid)) return false;
        }
        return true;
    }
};
struct Server {
    std::vector<std::unique_ptr<Channel>> channels;
    uint64_t next = 1;
    size_t cursor = 0;
    explicit Server(const char *scope) {
        auto name = pipeName(scope);
        ASProcess own{}; require(as_process_identity(as_current_pid(), &own));
        std::string user(own.sid);
        auto sid = std::wstring(user.begin(), user.end());
        auto sddl = L"O:" + sid + L"D:P(A;;FA;;;" + sid + L")(A;;FA;;;SY)";
        Local security;
        require(ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &security.value, nullptr));
        SECURITY_ATTRIBUTES attributes{sizeof(attributes), security.value, FALSE};
        for (size_t i = 0; i < maximumClients; ++i) {
            Handle pipe(CreateNamedPipeW(name.c_str(), PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED
                | (i == 0 ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0),
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                static_cast<DWORD>(maximumClients), 4096, 4096, 0, &attributes));
            require(pipe.value != INVALID_HANDLE_VALUE, occupied);
            privatePipeACL(pipe.value, own);
            auto channel = std::make_unique<Channel>(std::move(pipe));
            channel->listen(); channels.push_back(std::move(channel));
        }
    }
    Channel *find(uint64_t id) {
        for (auto &channel : channels) if (id && channel->id == id && channel->connected) return channel.get();
        return nullptr;
    }
    void reset(Channel &channel) {
        channel.cancel(); DisconnectNamedPipe(channel.pipe.value);
        channel.output.clear(); channel.queued = channel.written = 0;
        channel.listen();
    }
    bool poll(ASPipeEvent &event) {
        for (size_t turn = 0; turn < channels.size(); ++turn) {
            auto &channel = *channels[cursor++ % channels.size()];
            auto oldID = channel.id;
            try {
                if (!channel.connected) {
                    DWORD ignored = 0;
                    if (!channel.connect.complete(channel.pipe.value, ignored)) continue;
                    ULONG pid = 0;
                    require(GetNamedPipeClientProcessId(channel.pipe.value, &pid), unauthorized);
                    channel.peer = std::make_unique<Peer>(pid);
                    require(channel.validPeer(true), unauthorized);
                    channel.connected = true; channel.id = next++;
                    require(channel.id != 0, invalid);
                    event = {1, channel.id, channel.peer->identity, {nullptr, 0, 0}};
                    return true;
                }
                require(channel.validPeer(true), unauthorized);
                channel.flush();
                auto data = channel.receive();
                if (data.size) { event = {2, channel.id, channel.peer->identity, data}; return true; }
            } catch (...) {
                reset(channel);
                if (oldID) { event = {3, oldID, {}, {nullptr, 0, 0}}; return true; }
            }
        }
        return false;
    }
};
std::unique_ptr<Channel> openClient(const char *scope, const ASProcess &expected, uint32_t timeoutMs) {
    require(timeoutMs > 0 && timeoutMs <= 5000, invalid);
    auto name = pipeName(scope);
    auto until = GetTickCount64() + timeoutMs;
    do {
        Handle pipe(CreateFileW(name.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, nullptr));
        if (pipe.value != INVALID_HANDLE_VALUE) {
            auto channel = std::make_unique<Channel>(std::move(pipe));
            ULONG pid = 0;
            require(GetNamedPipeServerProcessId(channel->pipe.value, &pid) && pid == expected.pid, unauthorized);
            channel->peer = std::make_unique<Peer>(pid);
            require(same(channel->peer->identity, expected) && channel->validPeer(false), unauthorized);
            channel->connected = true;
            return channel;
        }
        auto error = GetLastError();
        require(error == ERROR_PIPE_BUSY || error == ERROR_FILE_NOT_FOUND, unauthorized);
        Sleep(2);
    } while (GetTickCount64() < until);
    throw Failure{timeout};
}
}

extern "C" void *as_pipe_server_open(const char *scope, int32_t *error) {
    if (error) *error = 0;
    try { return new Server(scope); } catch (...) { if (error) *error = failed(); return nullptr; }
}
extern "C" int32_t as_pipe_server_poll(void *server, ASPipeEvent *event) {
    try { require(server && event, invalid); *event = {}; return static_cast<Server *>(server)->poll(*event) ? 1 : 0; }
    catch (...) { return -failed(); }
}
extern "C" int32_t as_pipe_server_send(void *server, uint64_t id, const uint8_t *bytes, size_t size) {
    try {
        require(server, invalid); auto channel = static_cast<Server *>(server)->find(id);
        require(channel && channel->validPeer(true), unauthorized); channel->enqueue(bytes, size); return 0;
    } catch (...) { return failed(); }
}
extern "C" void as_pipe_server_disconnect(void *server, uint64_t id) {
    try { if (server) if (auto channel = static_cast<Server *>(server)->find(id)) static_cast<Server *>(server)->reset(*channel); }
    catch (...) {} // The next poll fails/retires an unusable instance; never rebinds a stale ID.
}
extern "C" int32_t as_pipe_server_peer(void *server, uint64_t id, ASProcess *peer, ASProcess *parent) {
    try {
        require(server && peer, invalid); auto channel = static_cast<Server *>(server)->find(id);
        require(channel && channel->validPeer(true), unauthorized);
        *peer = channel->peer->identity;
        if (parent) {
            auto pid = as_parent_pid(peer->pid);
            Peer ancestor(pid);
            require(ancestor.identity.created < peer->created && as_parent_pid(peer->pid) == pid
                && channel->validPeer(true), unauthorized);
            *parent = ancestor.identity;
        }
        return 0;
    } catch (...) { return failed(); }
}
extern "C" void as_pipe_server_close(void *server) { delete static_cast<Server *>(server); }
extern "C" void *as_pipe_client_open(const char *scope, const ASProcess *expected, uint32_t timeoutMs, int32_t *error) {
    if (error) *error = 0;
    try { require(expected, invalid); return openClient(scope, *expected, timeoutMs).release(); }
    catch (...) { if (error) *error = failed(); return nullptr; }
}
extern "C" ASBytes as_pipe_client_read(void *client, uint32_t timeoutMs, ASCancelled stop, void *context) {
    try {
        require(client && timeoutMs <= 5000, invalid);
        auto &channel = *static_cast<Channel *>(client);
        auto until = GetTickCount64() + timeoutMs;
        do {
            require(!stop || !stop(context), cancelled);
            require(channel.validPeer(false), unauthorized);
            auto data = channel.receive();
            if (data.size) return data;
            if (GetTickCount64() >= until) return {nullptr, 0, 0};
            Sleep(2);
        } while (true);
    } catch (...) {
        int code = failed();
        if (client) static_cast<Channel *>(client)->invalidate();
        return {nullptr, 0, code};
    }
}
extern "C" int32_t as_pipe_client_write(void *client, const uint8_t *bytes, size_t size,
    uint32_t timeoutMs, ASCancelled stop, void *context) {
    try {
        require(client && timeoutMs > 0 && timeoutMs <= 5000, invalid);
        auto &channel = *static_cast<Channel *>(client);
        channel.enqueue(bytes, size);
        auto until = GetTickCount64() + timeoutMs;
        do {
            require(!stop || !stop(context), cancelled);
            require(channel.validPeer(false), unauthorized);
            channel.flush();
            if (channel.output.empty()) return 0;
            require(GetTickCount64() < until, timeout); Sleep(2);
        } while (true);
    } catch (...) {
        int code = failed();
        if (client) static_cast<Channel *>(client)->invalidate();
        return code;
    }
}
extern "C" void as_pipe_client_close(void *client) { delete static_cast<Channel *>(client); }

extern "C" int32_t as_pipe_client_enqueue(void *client, const uint8_t *bytes, size_t size) {
    try {
        require(client, invalid); auto &channel = *static_cast<Channel *>(client);
        require(channel.validPeer(false), unauthorized); channel.enqueue(bytes, size); return 0;
    } catch (...) { return failed(); }
}
extern "C" int32_t as_pipe_client_flush(void *client) {
    try {
        require(client, invalid); auto &channel = *static_cast<Channel *>(client);
        require(channel.validPeer(false), unauthorized); channel.flush(); return 0;
    } catch (...) { return failed(); }
}
extern "C" void *as_stdio_open(int32_t *error) {
    if (error) *error = 0;
    try { return new Stdio(); } catch (...) { if (error) *error = failed(); return nullptr; }
}
extern "C" ASBytes as_stdio_read(void *port) {
    try { require(port, invalid); auto &io = *static_cast<Stdio *>(port);
        require(io.valid(), unauthorized); return io.input.receive();
    } catch (...) { return {nullptr, 0, failed()}; }
}
extern "C" int32_t as_stdio_send(void *port, const uint8_t *bytes, size_t size) {
    try { require(port, invalid); auto &io = *static_cast<Stdio *>(port);
        require(io.valid(), unauthorized); io.output.enqueue(bytes, size); return 0;
    } catch (...) { return failed(); }
}
extern "C" int32_t as_stdio_flush(void *port) {
    try { require(port, invalid); auto &io = *static_cast<Stdio *>(port);
        require(io.valid(), unauthorized); io.output.flush(); return 0;
    } catch (...) { return failed(); }
}
extern "C" void as_stdio_close(void *port) { delete static_cast<Stdio *>(port); }

extern "C" int32_t as_process_ancestor(uint32_t depth, ASProcess *out) {
    try {
        require(out && depth >= 1 && depth <= 3, invalid);
        std::vector<std::unique_ptr<Peer>> chain;
        chain.push_back(std::make_unique<Peer>(as_current_pid()));
        for (uint32_t i = 0; i < depth; ++i) {
            auto &child = *chain.back();
            auto parent = std::make_unique<Peer>(as_parent_pid(child.identity.pid));
            require(parent->identity.created < child.identity.created, unauthorized);
            chain.push_back(std::move(parent));
        }
        for (size_t i = 0; i < chain.size(); ++i) {
            require(chain[i]->alive(), unauthorized);
            if (i + 1 < chain.size()) require(as_parent_pid(chain[i]->identity.pid) == chain[i + 1]->identity.pid, unauthorized);
        }
        *out = chain.back()->identity; return 0;
    } catch (...) { return failed(); }
}

extern "C" int32_t as_backend_recipient(ASProcess *out) {
    try {
        require(out, invalid);
        const HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
        ULONG client = 0, server = 0;
        require(input && input != INVALID_HANDLE_VALUE && GetFileType(input) == FILE_TYPE_PIPE
            && GetNamedPipeClientProcessId(input, &client) && GetNamedPipeServerProcessId(input, &server)
            && client > 0 && client == server, unauthorized);
        Peer recipient(client);
        // Inherited backend input is a pipe created by Node, not a new Python
        // pipe. Allow exactly Python and its optional Windows venv redirector.
        for (uint32_t depth : {2u, 3u}) {
            ASProcess ancestor{};
            if (as_process_ancestor(depth, &ancestor) == 0 && same(ancestor, recipient.identity) && recipient.alive()) {
                *out = ancestor; return 0;
            }
        }
        throw Failure{unauthorized};
    } catch (...) { return failed(); }
}

extern "C" int32_t as_process_has_exited(const ASProcess *expected) {
    if (!expected || !expected->pid || !expected->created) return -1;
    Handle process(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, expected->pid));
    if (!process.value) return GetLastError() == ERROR_INVALID_PARAMETER ? 1 : -1;
    FILETIME created{}, exited{}, kernel{}, user{};
    if (!GetProcessTimes(process.value, &created, &exited, &kernel, &user)) return -1;
    const uint64_t start = (static_cast<uint64_t>(created.dwHighDateTime) << 32) | created.dwLowDateTime;
    if (start != expected->created || WaitForSingleObject(process.value, 0) == WAIT_OBJECT_0) return 1;
    return WaitForSingleObject(process.value, 0) == WAIT_TIMEOUT ? 0 : -1;
}
extern "C" int32_t as_process_same_executable(uint32_t pid) {
    Handle process(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, pid));
    if (!process.value) return 0;
    wchar_t self[32768], peer[32768]; DWORD length = 32768;
    const DWORD ownLength = GetModuleFileNameW(nullptr, self, 32768);
    return ownLength > 0 && ownLength < 32768 && QueryFullProcessImageNameW(process.value, 0, peer, &length)
        && length > 0 && length < 32768 && _wcsicmp(self, peer) == 0
        && WaitForSingleObject(process.value, 0) == WAIT_TIMEOUT;
}

extern "C" ASBytes as_pipe_self_test() {
    std::string stage = "listen", output;
    try {
        auto scope = "selftest-" + std::to_string(as_current_pid()) + "-" + std::to_string(GetTickCount64());
        int32_t error = 0;
        std::unique_ptr<Server> server(static_cast<Server *>(as_pipe_server_open(scope.c_str(), &error)));
        require(server != nullptr, error);
        stage = "exclusive_owner";
        void *duplicate = as_pipe_server_open(scope.c_str(), &error);
        as_pipe_server_close(duplicate); require(!duplicate && error == occupied);
        ASProcess own{}; require(as_process_identity(as_current_pid(), &own));
        stage = "wrong_epoch";
        auto wrong = own; ++wrong.created;
        void *rejected = as_pipe_client_open(scope.c_str(), &wrong, 100, &error);
        as_pipe_client_close(rejected); require(!rejected && error == unauthorized);
        wrong = own; wrong.sid[0] = 'X';
        rejected = as_pipe_client_open(scope.c_str(), &wrong, 100, &error);
        as_pipe_client_close(rejected); require(!rejected && error == unauthorized);
        rejected = as_pipe_client_open("../not-local", &own, 100, &error);
        as_pipe_client_close(rejected); require(!rejected && error == invalid);
        // Reap the intentionally rejected connections before the child test.
        for (unsigned turn = 0; turn < 64; ++turn) { ASPipeEvent event{}; server->poll(event); as_bytes_free(event.bytes); }
        stage = "cross_process";
        wchar_t executable[32768];
        DWORD length = GetModuleFileNameW(nullptr, executable, 32768);
        require(length > 0 && length < 32768);
        auto arguments = L"--self-test-pipe-client " + std::wstring(scope.begin(), scope.end()) + L" "
            + std::to_wstring(own.pid) + L" " + std::to_wstring(own.created);
        ASBytes child{}; std::atomic<bool> done{false};
        bool childIdentity = false, parentIdentity = false;
        {
            std::jthread worker([&] {
                child = as_run_worker(reinterpret_cast<const uint16_t *>(executable),
                    reinterpret_cast<const uint16_t *>(arguments.c_str()), 3000, 4096, nullptr, nullptr);
                done = true;
            });
            auto until = GetTickCount64() + 3500;
            while (!done.load() && GetTickCount64() < until) {
                ASPipeEvent event{};
                if (server->poll(event)) {
                    if (event.kind == 1 && event.peer.pid != own.pid) {
                        childIdentity = true;
                        ASProcess peer{}, parent{};
                        parentIdentity = as_pipe_server_peer(server.get(), event.connection, &peer, &parent) == 0
                            && same(peer, event.peer) && same(parent, own);
                    }
                    if (event.kind == 2) {
                        int code = as_pipe_server_send(server.get(), event.connection, event.bytes.data, event.bytes.size);
                        as_bytes_free(event.bytes); require(code == 0);
                    }
                }
                Sleep(1);
            }
        }
        bool childOK = child.error == 0 && child.data && std::string(reinterpret_cast<char *>(child.data), child.size) == "pipe-child-ok\n";
        as_bytes_free(child);
        require(childOK && childIdentity && parentIdentity);
        for (unsigned turn = 0; turn < 64; ++turn) { ASPipeEvent event{}; server->poll(event); as_bytes_free(event.bytes); }
        stage = "read_bounds_and_cancel";
        std::unique_ptr<Channel> client(static_cast<Channel *>(as_pipe_client_open(scope.c_str(), &own, 100, &error)));
        require(client != nullptr, error);
        uint64_t id = 0;
        for (unsigned turn = 0; turn < 64 && !id; ++turn) {
            ASPipeEvent event{};
            if (server->poll(event) && event.kind == 1 && event.peer.pid == own.pid) id = event.connection;
            as_bytes_free(event.bytes);
        }
        require(id != 0);
        auto start = GetTickCount64();
        auto empty = as_pipe_client_read(client.get(), 20, nullptr, nullptr);
        bool emptyOK = empty.error == 0 && empty.size == 0 && GetTickCount64() - start < 500;
        as_bytes_free(empty); require(emptyOK);
        stage = "output_limit";
        std::vector<uint8_t> data(maximumFrame, 'x');
        for (unsigned i = 0; i < 4; ++i) require(as_pipe_server_send(server.get(), id, data.data(), data.size()) == 0);
        require(as_pipe_server_send(server.get(), id, data.data(), data.size()) == occupied);
        require(as_pipe_server_send(server.get(), id, data.data(), data.size() + 1) == invalid);
        stage = "disconnect_and_stale_id";
        as_pipe_server_disconnect(server.get(), id);
        require(as_pipe_server_send(server.get(), id, data.data(), 1) == unauthorized);
        auto closed = as_pipe_client_read(client.get(), 100, nullptr, nullptr);
        bool closedOK = closed.error != 0; as_bytes_free(closed); require(closedOK);
        client.reset();
        stage = "cancelled_read_closes_connection";
        client = openClient(scope.c_str(), own, 100);
        auto stopped = as_pipe_client_read(client.get(), 1000, [](void *) -> int32_t { return 1; }, nullptr);
        bool cancelOK = stopped.error == cancelled; as_bytes_free(stopped); require(cancelOK);
        require(as_pipe_client_write(client.get(), data.data(), 1, 100, nullptr, nullptr) == unauthorized);
        client.reset();
        for (unsigned turn = 0; turn < 64; ++turn) { ASPipeEvent event{}; server->poll(event); as_bytes_free(event.bytes); }
        stage = "client_limit";
        std::vector<std::unique_ptr<Channel>> clients;
        for (size_t i = 0; i < maximumClients; ++i) clients.push_back(openClient(scope.c_str(), own, 100));
        rejected = as_pipe_client_open(scope.c_str(), &own, 20, &error);
        as_pipe_client_close(rejected); require(!rejected && error == timeout);
        clients.clear();
        stage = "restart_releases_name";
        server.reset();
        server.reset(static_cast<Server *>(as_pipe_server_open(scope.c_str(), &error)));
        require(server != nullptr, error);
        output = "{\"ok\":true,\"cross_process_echo\":true,\"kernel_peers\":true,\"kernel_parent\":true,"
            "\"wrong_epoch_rejected\":true,\"private_scope\":true,\"actual_pipe_dacl\":true,\"exclusive_owner\":true,"
            "\"bounded_read\":true,\"cancelled_read\":true,\"bounded_output\":true,"
            "\"client_limit\":true,\"stale_connection_rejected\":true,\"restart\":true}";
    } catch (...) { output = "{\"ok\":false,\"stage\":\"" + stage + "\",\"code\":" + std::to_string(failed()) + "}"; }
    try { return copy(reinterpret_cast<const uint8_t *>(output.data()), output.size()); }
    catch (...) { return {nullptr, 0, systemFailure}; }
}
