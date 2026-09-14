#pragma once
#include <stddef.h>
#include <stdint.h>
#if defined(_WIN32)
#define AS_STORAGE_API __declspec(dllexport)
#else
#define AS_STORAGE_API
#endif
#ifdef __cplusplus
extern "C" {
#endif

// All returned buffers have one owner and must be released using as_bytes_free.
typedef struct ASBytes { uint8_t *data; size_t size; int32_t error; } ASBytes;
typedef struct ASProcess {
    uint32_t pid;
    uint64_t created;
    char sid[192];
} ASProcess;
typedef struct ASWindow {
    uint64_t handle;
    ASProcess process;
    int32_t x, y, width, height;
    char title[4096];
    char app[1024];
} ASWindow;
AS_STORAGE_API void as_bytes_free(ASBytes bytes);
int32_t as_process_identity(uint32_t pid, ASProcess *out);
// 1 = proven exited/creation identity replaced, 0 = still alive, -1 = unknown.
int32_t as_process_has_exited(const ASProcess *expected);
int32_t as_process_same_executable(uint32_t pid);
// Up to three ancestors, all held and revalidated. Depth is internal, not wire data.
int32_t as_process_ancestor(uint32_t depth, ASProcess *out);
// Inherited backend input-pipe owner, cross-checked against kernel ancestry.
int32_t as_backend_recipient(ASProcess *out);
uint32_t as_parent_pid(uint32_t pid);
uint32_t as_current_pid(void);
uint64_t as_monotonic_ns(void);
int32_t as_foreground_window(ASWindow *out);
int32_t as_window_matches(const ASWindow *expected, int32_t requireForeground);
int32_t as_window_process(uint64_t window, ASProcess *out);
ASBytes as_capture_png(uint64_t window, uint32_t timeoutMs);

// Raw COM wrappers. Swift owns traversal, budgets, projection and password policy.
void *as_uia_open(void);
void as_uia_close(void *context);
void *as_uia_root(void *context, uint64_t window);
void *as_uia_first_child(void *context, void *element);
void *as_uia_next_sibling(void *context, void *element);
void as_uia_release(void *element);
ASBytes as_uia_properties(void *element, uint32_t maxCharacters);

// One bounded, hidden, job-owned child with only explicit stdio inheritance.
typedef int32_t (*ASCancelled)(void *);
ASBytes as_run_worker(const uint16_t *executable, const uint16_t *arguments,
    uint32_t timeoutMs, uint32_t maximumBytes, ASCancelled cancelled, void *context);

// File authority comes from held handles, not a client-supplied manifest/path.
// Directory objects pin every ancestor; callers serialize operations on each object.
typedef struct ASFileProof {
    uint64_t volume;
    uint8_t file_id[16];
    uint64_t size;
    uint64_t modified;
    uint64_t changed;
    uint32_t links;
    char owner_sid[192];
    char sha256[65];
} ASFileProof;
AS_STORAGE_API void *as_private_directory_open(const uint16_t *path, int32_t create, int32_t *error);
AS_STORAGE_API void as_private_directory_close(void *directory);
AS_STORAGE_API int32_t as_private_publish(void *directory, const char *temporary, const char *name,
    const uint8_t *data, size_t size, ASFileProof *proof);
AS_STORAGE_API ASBytes as_private_read(void *directory, const char *name, uint32_t maximum, ASFileProof *proof);
// Cleanup opens relative to the pinned directory, matches the entire original
// proof, then deletes that handle. Missing files are an idempotent success;
// replacement files and busy readers are left alone.
AS_STORAGE_API int32_t as_private_remove(void *directory, const char *name, const ASFileProof *expected);
// Versioned, synchronous Python storage ABI. Mutation authority is acquired on
// the initial open, denying other directory renames/deletes until close.
AS_STORAGE_API uint32_t as_media_storage_abi(void);
AS_STORAGE_API void *as_private_media_open(const uint16_t *path, int32_t create, int32_t mutate, int32_t *error);
// Only a sibling .appshot-media directory, exclusive rename. Never overwrite.
AS_STORAGE_API int32_t as_private_media_rename(void *directory, const uint16_t *destination);
// Preflight every direct child before deletion. Unknown names, links, ACLs,
// reparse points and busy files refuse the operation; never recurse.
AS_STORAGE_API int32_t as_private_media_delete(void *directory);
// Bounded, direct-child inventory from a revalidated relative directory handle.
// controlOnly permits broker.json/settings.json only; no file is ever deleted.
int32_t as_private_check_quota(void *directory, uint32_t maximumEntries, uint64_t maximumBytes, int32_t controlOnly);
// Self-owned fixtures only; never removes an existing file or a directory tree.
ASBytes as_private_files_self_test(const uint16_t *path);

// Local-only byte pipes. All calls for an object are serialized by its owner.
// poll never waits for a client to read/write; pending I/O has bounded storage.
typedef struct ASPipeEvent {
    int32_t kind; // 1 = connected, 2 = bytes, 3 = disconnected
    uint64_t connection;
    ASProcess peer;
    ASBytes bytes;
} ASPipeEvent;
void *as_pipe_server_open(const char *scope, int32_t *error);
int32_t as_pipe_server_poll(void *server, ASPipeEvent *event);
int32_t as_pipe_server_send(void *server, uint64_t connection, const uint8_t *bytes, size_t size);
void as_pipe_server_disconnect(void *server, uint64_t connection);
int32_t as_pipe_server_peer(void *server, uint64_t connection, ASProcess *peer, ASProcess *parent);
void as_pipe_server_close(void *server);
void *as_pipe_client_open(const char *scope, const ASProcess *expected, uint32_t timeoutMs, int32_t *error);
ASBytes as_pipe_client_read(void *client, uint32_t timeoutMs, ASCancelled cancelled, void *context);
int32_t as_pipe_client_write(void *client, const uint8_t *bytes, size_t size,
    uint32_t timeoutMs, ASCancelled cancelled, void *context);
void as_pipe_client_close(void *client);
int32_t as_pipe_client_enqueue(void *client, const uint8_t *bytes, size_t size);
int32_t as_pipe_client_flush(void *client);
ASBytes as_pipe_self_test(void);

// Duplicated overlapped stdio with a held, kernel-proven parent. Single owner;
// bounded nonblocking reads/queued writes. Close cancels all owned pending I/O.
void *as_stdio_open(int32_t *error);
ASBytes as_stdio_read(void *port);
int32_t as_stdio_send(void *port, const uint8_t *bytes, size_t size);
int32_t as_stdio_flush(void *port);
void as_stdio_close(void *port);

// Hotkeys belong to the calling native thread; no window hooks or input synthesis.
// The owner must poll/replace/close on that same thread.
void *as_hotkey_open(int32_t *error);
int32_t as_hotkey_replace(void *hotkey, uint32_t modifiers, uint32_t key, int32_t enabled);
int32_t as_hotkey_poll(void *hotkey);
void as_hotkey_close(void *hotkey);
ASBytes as_hotkey_self_test(void);

// Test fixture only: creates its own window on a dedicated thread, never captures another app.
uint64_t as_fixture_open(void);
// Bits: owned=1, visible=2, foreground=4, default desktop=8, eligible=16,
// explicit Start test click=32. Capture requires all six; no other window is read.
uint32_t as_fixture_status(uint64_t window);
void as_fixture_close(uint64_t window);
#ifdef __cplusplus
}
#endif
