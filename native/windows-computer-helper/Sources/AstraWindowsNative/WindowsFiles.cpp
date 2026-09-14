#include "AstraWindowsNative.h"
#include <windows.h>
#include <winternl.h>
#include <aclapi.h>
#include <sddl.h>
#include <bcrypt.h>
#include <objbase.h>
#include <winioctl.h>
#include <algorithm>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

// Deliberately separate from capture/COM. All filesystem authority is checked
// through handles; no lstat-then-fopen, path-based ACL repair or recursive delete.
namespace {
constexpr size_t maximumFile = 10 * 1024 * 1024;
enum FileError { unsafe = 10, io = 11, collision = 12, tooLarge = 13, notFound = 14 };
struct Failure { int code; };
void require(bool condition, int code = unsafe) { if (!condition) throw Failure{code}; }
struct Handle {
    HANDLE value = INVALID_HANDLE_VALUE;
    explicit Handle(HANDLE h = INVALID_HANDLE_VALUE) : value(h) {}
    void close() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); value = INVALID_HANDLE_VALUE; }
    ~Handle() { close(); }
    Handle(Handle &&other) noexcept : value(other.value) { other.value = INVALID_HANDLE_VALUE; }
    Handle(const Handle &) = delete;
    Handle &operator=(const Handle &) = delete;
};
struct Local {
    void *value = nullptr;
    ~Local() { if (value) LocalFree(value); }
};
using NtOpen = NTSTATUS(NTAPI *)(PHANDLE, ACCESS_MASK, POBJECT_ATTRIBUTES,
    PIO_STATUS_BLOCK, PLARGE_INTEGER, ULONG, ULONG, ULONG, ULONG, PVOID, ULONG);
using NtSet = NTSTATUS(NTAPI *)(HANDLE, PIO_STATUS_BLOCK, PVOID, ULONG, FILE_INFORMATION_CLASS);

bool component(const std::wstring &name) {
    if (name.empty() || name.size() > 255 || name == L"." || name == L".."
        || name.back() == L'.' || name.back() == L' ') return false;
    for (auto c : name) if (c < 32 || wcschr(L"<>:\"\\/|?*", c)) return false;
    auto stem = name.substr(0, name.find(L'.'));
    std::transform(stem.begin(), stem.end(), stem.begin(), towupper);
    if (stem == L"CON" || stem == L"PRN" || stem == L"AUX" || stem == L"NUL"
        || stem == L"CONIN$" || stem == L"CONOUT$") return false;
    if (stem.size() == 4 && (stem.substr(0, 3) == L"COM" || stem.substr(0, 3) == L"LPT")
        && ((stem[3] >= L'1' && stem[3] <= L'9') || wcschr(L"\u00b9\u00b2\u00b3", stem[3]))) return false;
    return true;
}
std::wstring basename(const char *name) {
    require(name != nullptr);
    auto size = strnlen(name, 129);
    require(size > 0 && size <= 128);
    std::wstring result;
    for (size_t i = 0; i < size; ++i) {
        char c = name[i];
        require((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
            || (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.');
        result += static_cast<wchar_t>(c);
    }
    require(component(result));
    return result;
}
Handle relative(HANDLE directory, const std::wstring &name, ACCESS_MASK access,
    ULONG disposition, bool isDirectory, PSECURITY_DESCRIPTOR security = nullptr,
    ULONG sharing = FILE_SHARE_READ) {
    require(component(name));
    static auto open = reinterpret_cast<NtOpen>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtCreateFile"));
    require(open != nullptr, io);
    UNICODE_STRING text{};
    text.Length = static_cast<USHORT>(name.size() * sizeof(wchar_t));
    text.MaximumLength = text.Length;
    text.Buffer = const_cast<PWSTR>(name.c_str());
    OBJECT_ATTRIBUTES attributes{};
    attributes.Length = sizeof(attributes);
    attributes.RootDirectory = directory;
    attributes.ObjectName = &text;
    attributes.Attributes = OBJ_CASE_INSENSITIVE;
    attributes.SecurityDescriptor = security;
    IO_STATUS_BLOCK status{};
    HANDLE file = INVALID_HANDLE_VALUE;
    // NtCreateFile's option is 0x00200000, not Win32 FILE_FLAG_OPEN_REPARSE_POINT.
    auto result = open(&file, access | SYNCHRONIZE, &attributes, &status, nullptr,
        FILE_ATTRIBUTE_NORMAL, sharing, disposition,
        (isDirectory ? 0x00000001 : 0x00000040) | 0x00000020 | 0x00200000, nullptr, 0);
    require(result >= 0, result == static_cast<NTSTATUS>(0xC0000035L) ? collision
        : result == static_cast<NTSTATUS>(0xC0000034L) || result == static_cast<NTSTATUS>(0xC000003AL) ? notFound : io);
    return Handle(file);
}
bool ordinary(HANDLE handle, bool directory) {
    FILE_ATTRIBUTE_TAG_INFO tag{};
    return GetFileType(handle) == FILE_TYPE_DISK
        && GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &tag, sizeof(tag))
        && !(tag.FileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_OFFLINE))
        && static_cast<bool>(tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) == directory;
}
std::wstring finalPath(HANDLE handle) {
    wchar_t path[32768];
    auto n = GetFinalPathNameByHandleW(handle, path, 32768, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    require(n > 0 && n < 32768);
    return std::wstring(path, n);
}

struct Directory {
    std::vector<Handle> ancestors;
    std::wstring path;
    std::string sidText;
    Local sid, system, security;
    bool mutableMedia = false;
    HANDLE handle() const { return ancestors.back().value; }
    Directory() {
        ASProcess identity{};
        require(as_process_identity(as_current_pid(), &identity) != 0);
        sidText = identity.sid;
        std::wstring sidW(sidText.begin(), sidText.end());
        require(ConvertStringSidToSidW(sidW.c_str(), &sid.value) != 0);
        require(ConvertStringSidToSidW(L"S-1-5-18", &system.value) != 0);
        auto sddl = L"O:" + sidW + L"D:P(A;;FA;;;" + sidW + L")(A;;FA;;;SY)";
        require(ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1,
            &security.value, nullptr) != 0);
    }
    void privateACL(HANDLE file) const {
        Local descriptor;
        PSID owner = nullptr;
        PACL acl = nullptr;
        require(GetSecurityInfo(file, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &owner, nullptr, &acl, nullptr, &descriptor.value) == ERROR_SUCCESS);
        require(owner && IsValidSid(owner) && EqualSid(owner, sid.value) && acl && IsValidAcl(acl));
        SECURITY_DESCRIPTOR_CONTROL control{}; DWORD revision = 0;
        require(GetSecurityDescriptorControl(descriptor.value, &control, &revision)
            && (control & SE_DACL_PROTECTED));
        require(acl->AceCount == 2);
        bool userFound = false, systemFound = false;
        for (DWORD index = 0; index < acl->AceCount; ++index) {
            void *raw = nullptr;
            require(GetAce(acl, index, &raw) && raw);
            auto ace = static_cast<ACCESS_ALLOWED_ACE *>(raw);
            require(ace->Header.AceType == ACCESS_ALLOWED_ACE_TYPE && ace->Header.AceFlags == 0
                && ace->Header.AceSize >= offsetof(ACCESS_ALLOWED_ACE, SidStart) + sizeof(SID)
                && ace->Mask == FILE_ALL_ACCESS);
            auto who = reinterpret_cast<PSID>(&ace->SidStart);
            require(IsValidSid(who));
            require(offsetof(ACCESS_ALLOWED_ACE, SidStart) + GetLengthSid(who) <= ace->Header.AceSize);
            if (EqualSid(who, sid.value)) { require(!userFound); userFound = true; }
            else if (EqualSid(who, system.value)) { require(!systemFound); systemFound = true; }
            else throw Failure{unsafe};
        }
        require(userFound && systemFound);
    }
    void validate() const {
        for (const auto &part : ancestors) require(ordinary(part.value, true));
        require(_wcsicmp(finalPath(handle()).c_str(), (L"\\\\?\\" + path).c_str()) == 0);
        privateACL(handle());
    }
};

ASFileProof inspect(Directory &directory, HANDLE file) {
    directory.validate();
    require(ordinary(file, false));
    directory.privateACL(file);
    FILE_ID_INFO identity{};
    FILE_STANDARD_INFO standard{};
    FILE_BASIC_INFO basic{};
    require(GetFileInformationByHandleEx(file, FileIdInfo, &identity, sizeof(identity))
        && GetFileInformationByHandleEx(file, FileStandardInfo, &standard, sizeof(standard))
        && GetFileInformationByHandleEx(file, FileBasicInfo, &basic, sizeof(basic)));
    require(!standard.Directory && !standard.DeletePending && standard.NumberOfLinks == 1
        && standard.EndOfFile.QuadPart >= 0);
    ASFileProof result{};
    result.volume = identity.VolumeSerialNumber;
    memcpy(result.file_id, identity.FileId.Identifier, 16);
    result.size = static_cast<uint64_t>(standard.EndOfFile.QuadPart);
    result.modified = static_cast<uint64_t>(basic.LastWriteTime.QuadPart);
    result.changed = static_cast<uint64_t>(basic.ChangeTime.QuadPart);
    result.links = standard.NumberOfLinks;
    require(directory.sidText.size() < sizeof(result.owner_sid));
    memcpy(result.owner_sid, directory.sidText.c_str(), directory.sidText.size() + 1);
    return result;
}
bool same(const ASFileProof &a, const ASFileProof &b, bool hash = false) {
    return a.volume == b.volume && !memcmp(a.file_id, b.file_id, 16)
        && a.size == b.size && a.modified == b.modified && a.changed == b.changed
        && a.links == b.links && !memcmp(a.owner_sid, b.owner_sid, sizeof(a.owner_sid))
        && (!hash || !memcmp(a.sha256, b.sha256, sizeof(a.sha256)));
}
bool sameContent(const ASFileProof &a, const ASFileProof &b) {
    return a.volume == b.volume && !memcmp(a.file_id, b.file_id, 16)
        && a.size == b.size && a.links == b.links
        && !memcmp(a.owner_sid, b.owner_sid, sizeof(a.owner_sid))
        && !memcmp(a.sha256, b.sha256, sizeof(a.sha256));
}
void digest(const uint8_t *bytes, size_t size, char (&output)[65]) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    require(BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0, io);
    struct Close { BCRYPT_ALG_HANDLE value; ~Close() { BCryptCloseAlgorithmProvider(value, 0); } } close{algorithm};
    uint8_t hash[32];
    require(BCryptHash(algorithm, nullptr, 0, const_cast<PUCHAR>(bytes), static_cast<ULONG>(size), hash, 32) >= 0, io);
    const char *hex = "0123456789abcdef";
    for (size_t i = 0; i < 32; ++i) { output[2*i] = hex[hash[i] >> 4]; output[2*i+1] = hex[hash[i] & 15]; }
    output[64] = 0;
}
std::vector<uint8_t> readHeld(Directory &directory, HANDLE file, size_t maximum, ASFileProof &proof) {
    proof = inspect(directory, file);
    require(proof.size > 0 && proof.size <= maximum && maximum <= maximumFile, tooLarge);
    LARGE_INTEGER start{};
    require(SetFilePointerEx(file, start, nullptr, FILE_BEGIN) != 0, io);
    std::vector<uint8_t> data(static_cast<size_t>(proof.size));
    size_t position = 0;
    while (position < data.size()) {
        DWORD count = 0;
        require(ReadFile(file, data.data() + position, static_cast<DWORD>(data.size() - position), &count, nullptr)
            && count > 0, io);
        position += count;
    }
    require(same(proof, inspect(directory, file)));
    digest(data.data(), data.size(), proof.sha256);
    return data;
}
void removeHeld(HANDLE file) {
    FILE_DISPOSITION_INFO disposition{TRUE};
    require(SetFileInformationByHandle(file, FileDispositionInfo, &disposition, sizeof(disposition)) != 0, io);
}
int failure() {
    try { throw; } catch (const Failure &e) { return e.code; } catch (...) { return io; }
}
}

static void *openDirectory(const uint16_t *input, int32_t create, bool mutate, int32_t *error) {
    if (error) *error = 0;
    try {
        require(input != nullptr);
        auto length = wcsnlen(reinterpret_cast<const wchar_t *>(input), 4097);
        require(length >= 4 && length <= 4096);
        std::wstring path(reinterpret_cast<const wchar_t *>(input), length);
        std::replace(path.begin(), path.end(), L'/', L'\\');
        require(path[0] >= L'A' && path[0] <= L'Z' && path[1] == L':' && path[2] == L'\\');
        require(GetDriveTypeW(path.substr(0, 3).c_str()) == DRIVE_FIXED);
        auto directory = std::make_unique<Directory>();
        directory->mutableMedia = mutate;
        auto root = CreateFileW(path.substr(0, 3).c_str(), FILE_READ_ATTRIBUTES | READ_CONTROL,
            FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
        require(root != INVALID_HANDLE_VALUE, io);
        directory->ancestors.emplace_back(root);
        require(ordinary(root, true));
        require(_wcsicmp(finalPath(root).c_str(), (L"\\\\?\\" + path.substr(0, 3)).c_str()) == 0);
        size_t position = 3;
        while (position < path.size()) {
            auto end = path.find(L'\\', position);
            bool leaf = end == std::wstring::npos;
            auto name = path.substr(position, leaf ? path.size() - position : end - position);
            require(component(name));
            auto next = relative(directory->handle(), name, FILE_READ_ATTRIBUTES | READ_CONTROL | FILE_TRAVERSE
                | (leaf ? FILE_ADD_FILE | (mutate ? DELETE | FILE_LIST_DIRECTORY : 0) : 0),
                leaf && create ? 3 : 1, true, leaf && create ? directory->security.value : nullptr,
                FILE_SHARE_READ | FILE_SHARE_WRITE);
            require(ordinary(next.value, true));
            directory->ancestors.push_back(std::move(next));
            if (leaf) break;
            position = end + 1;
            require(position < path.size());
        }
        directory->path = path;
        directory->validate();
        return directory.release();
    } catch (...) { if (error) *error = failure(); return nullptr; }
}
extern "C" void *as_private_directory_open(const uint16_t *input, int32_t create, int32_t *error) {
    return openDirectory(input, create, false, error);
}
extern "C" void as_private_directory_close(void *pointer) { delete static_cast<Directory *>(pointer); }

namespace {
bool mediaDirectoryName(const std::wstring &path) {
    const std::wstring suffix = L".appshot-media";
    auto name = path.substr(path.find_last_of(L'\\') + 1);
    return component(name) && name.size() > suffix.size()
        && name.compare(name.size() - suffix.size(), suffix.size(), suffix) == 0;
}
void renameHeld(HANDLE file, HANDLE parent, const std::wstring &name) {
    require(component(name));
    size_t bytes = sizeof(FILE_RENAME_INFO) + (name.size() + 1) * sizeof(wchar_t);
    std::vector<uint8_t> buffer(bytes);
    auto rename = reinterpret_cast<FILE_RENAME_INFO *>(buffer.data());
    rename->ReplaceIfExists = FALSE;
    rename->RootDirectory = parent;
    rename->FileNameLength = static_cast<DWORD>(name.size() * sizeof(wchar_t));
    memcpy(rename->FileName, name.c_str(), rename->FileNameLength);
    static auto set = reinterpret_cast<NtSet>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtSetInformationFile"));
    require(set != nullptr, io);
    IO_STATUS_BLOCK status{};
    auto result = set(file, &status, rename, static_cast<ULONG>(bytes), static_cast<FILE_INFORMATION_CLASS>(10));
    require(result >= 0, result == static_cast<NTSTATUS>(0xC0000035L) ? collision : io);
}
bool mediaFileName(const std::wstring &name) {
    if (name.size() != 36 || (name.substr(32) != L".png" && name.substr(32) != L".tmp")) return false;
    return std::all_of(name.begin(), name.begin() + 32, [](wchar_t c) {
        return (c >= L'0' && c <= L'9') || (c >= L'a' && c <= L'f');
    });
}
}
extern "C" uint32_t as_media_storage_abi(void) { return 1; }
extern "C" void *as_private_media_open(const uint16_t *input, int32_t create, int32_t mutate, int32_t *error) {
    if (error) *error = 0;
    try {
        require(input && (create == 0 || create == 1) && (mutate == 0 || mutate == 1));
        auto length = wcsnlen(reinterpret_cast<const wchar_t *>(input), 4097);
        require(length >= 4 && length <= 4096);
        std::wstring path(reinterpret_cast<const wchar_t *>(input), length);
        std::replace(path.begin(), path.end(), L'/', L'\\');
        require(mediaDirectoryName(path));
        return openDirectory(input, create, mutate != 0, error);
    } catch (...) { if (error) *error = failure(); return nullptr; }
}
extern "C" int32_t as_private_media_rename(void *pointer, const uint16_t *input) {
    try {
        require(pointer && input);
        auto &directory = *static_cast<Directory *>(pointer);
        require(directory.mutableMedia && directory.ancestors.size() >= 2 && mediaDirectoryName(directory.path));
        directory.validate();
        auto length = wcsnlen(reinterpret_cast<const wchar_t *>(input), 4097);
        require(length >= 4 && length <= 4096);
        std::wstring destination(reinterpret_cast<const wchar_t *>(input), length);
        std::replace(destination.begin(), destination.end(), L'/', L'\\');
        auto split = destination.find_last_of(L'\\');
        require(split != std::wstring::npos && mediaDirectoryName(destination)
            && _wcsicmp(destination.substr(0, split).c_str(),
                directory.path.substr(0, directory.path.find_last_of(L'\\')).c_str()) == 0
            && _wcsicmp(destination.c_str(), directory.path.c_str()) != 0);
        // The original handle is the authority through the whole rename, not a
        // check followed by reopening its pathname with DELETE access.
        renameHeld(directory.handle(), directory.ancestors[directory.ancestors.size() - 2].value,
            destination.substr(split + 1));
        directory.path = destination;
        directory.validate();
        return 0;
    } catch (...) { return failure(); }
}
extern "C" int32_t as_private_media_delete(void *pointer) {
    try {
        require(pointer);
        auto &directory = *static_cast<Directory *>(pointer);
        require(directory.mutableMedia && mediaDirectoryName(directory.path));
        directory.validate();
        std::vector<Handle> files;
        std::vector<ASFileProof> proofs;
        bool first = true;
        alignas(FILE_ID_BOTH_DIR_INFO) uint8_t buffer[65536];
        while (true) {
            memset(buffer, 0, sizeof(buffer));
            if (!GetFileInformationByHandleEx(directory.handle(),
                first ? FileIdBothDirectoryRestartInfo : FileIdBothDirectoryInfo, buffer, sizeof(buffer))) {
                require(GetLastError() == ERROR_NO_MORE_FILES, io); break;
            }
            first = false;
            size_t offset = 0;
            while (true) {
                const auto header = offsetof(FILE_ID_BOTH_DIR_INFO, FileName);
                require(offset <= sizeof(buffer) - sizeof(FILE_ID_BOTH_DIR_INFO));
                auto item = reinterpret_cast<FILE_ID_BOTH_DIR_INFO *>(buffer + offset);
                require(item->FileNameLength % sizeof(wchar_t) == 0
                    && item->FileNameLength <= sizeof(buffer) - offset - header);
                std::wstring name(item->FileName, item->FileNameLength / sizeof(wchar_t));
                if (name != L"." && name != L"..") {
                    require(mediaFileName(name) && files.size() < 4096);
                    auto file = relative(directory.handle(), name, GENERIC_READ | READ_CONTROL | DELETE, 1, false);
                    auto proof = inspect(directory, file.value);
                    require(proof.size <= maximumFile);
                    files.push_back(std::move(file)); proofs.push_back(proof);
                }
                if (!item->NextEntryOffset) break;
                require(item->NextEntryOffset >= header + item->FileNameLength
                    && item->NextEntryOffset <= sizeof(buffer) - offset);
                offset += item->NextEntryOffset;
            }
        }
        // All files remain held and deny writing, linking, renaming or deletion.
        // Preflight the entire set before marking even one for deletion.
        for (size_t i = 0; i < files.size(); ++i) require(same(proofs[i], inspect(directory, files[i].value)));
        directory.validate();
        for (auto &file : files) removeHeld(file.value);
        files.clear();
        directory.validate();
        // A concurrently added unknown child makes this fail, preserving that
        // child. No recursive/path-based fallback is permitted.
        removeHeld(directory.handle());
        return 0;
    } catch (...) { return failure(); }
}

extern "C" int32_t as_private_publish(void *pointer, const char *temporary, const char *name,
    const uint8_t *data, size_t size, ASFileProof *proof) {
    try {
        require(pointer && data && proof && size > 0 && size <= maximumFile);
        auto &directory = *static_cast<Directory *>(pointer);
        directory.validate();
        auto staging = basename(temporary), destination = basename(name);
        require(_wcsicmp(staging.c_str(), destination.c_str()) != 0);
        auto file = relative(directory.handle(), staging, GENERIC_READ | GENERIC_WRITE | DELETE | READ_CONTROL,
            2, false, directory.security.value);
        bool published = false;
        try {
            inspect(directory, file.value);
            size_t position = 0;
            while (position < size) {
                DWORD count = 0;
                require(WriteFile(file.value, data + position, static_cast<DWORD>(size - position), &count, nullptr)
                    && count > 0, io);
                position += count;
            }
            require(FlushFileBuffers(file.value) != 0, io);
            auto before = inspect(directory, file.value);
            require(before.size == size);
            size_t bytes = sizeof(FILE_RENAME_INFO) + (destination.size() + 1) * sizeof(wchar_t);
            std::vector<uint8_t> buffer(bytes);
            auto rename = reinterpret_cast<FILE_RENAME_INFO *>(buffer.data());
            rename->ReplaceIfExists = FALSE;
            rename->RootDirectory = directory.handle();
            rename->FileNameLength = static_cast<DWORD>(destination.size() * sizeof(wchar_t));
            memcpy(rename->FileName, destination.c_str(), rename->FileNameLength);
            // The Win32 wrapper rejects this relative RootDirectory form on
            // supported Windows hosts (ERROR_INVALID_PARAMETER). Keep the pinned
            // root and use the native FileRenameInformation operation; never
            // fall back to building an absolute rename path.
            static auto set = reinterpret_cast<NtSet>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtSetInformationFile"));
            require(set != nullptr, io);
            IO_STATUS_BLOCK status{};
            auto renamed = set(file.value, &status, rename, static_cast<ULONG>(bytes), static_cast<FILE_INFORMATION_CLASS>(10));
            require(renamed >= 0, renamed == static_cast<NTSTATUS>(0xC0000035L) ? collision : io);
            *proof = inspect(directory, file.value);
            digest(data, size, proof->sha256);
            published = true;
        } catch (...) {
            // Only the original held file can be removed, even after a rename.
            try { inspect(directory, file.value); removeHeld(file.value); } catch (...) {}
            throw;
        }
        require(published);
        // Freeze the receipt only after the last writer closes. A replacement
        // between publish and reopen is detected by file ID plus content hash;
        // the new reader then denies further writes/deletes for its whole read.
        file.close();
        try {
            auto reader = relative(directory.handle(), destination, GENERIC_READ | READ_CONTROL, 1, false);
            ASFileProof actual{};
            readHeld(directory, reader.value, maximumFile, actual);
            require(sameContent(actual, *proof));
            *proof = actual;
        } catch (...) {
            // Best effort only. A replaced file or an active reader is never
            // removed just because it occupies our original publication name.
            try {
                auto owned = relative(directory.handle(), destination, GENERIC_READ | READ_CONTROL | DELETE, 1, false);
                ASFileProof actual{};
                readHeld(directory, owned.value, maximumFile, actual);
                if (sameContent(actual, *proof)) removeHeld(owned.value);
            } catch (...) {}
            throw;
        }
        return 0;
    } catch (...) { return failure(); }
}
extern "C" ASBytes as_private_read(void *pointer, const char *name, uint32_t maximum, ASFileProof *proof) {
    try {
        require(pointer && proof && maximum > 0 && maximum <= maximumFile);
        auto &directory = *static_cast<Directory *>(pointer);
        directory.validate();
        auto file = relative(directory.handle(), basename(name), GENERIC_READ | READ_CONTROL, 1, false);
        auto data = readHeld(directory, file.value, maximum, *proof);
        auto bytes = static_cast<uint8_t *>(malloc(data.size()));
        require(bytes != nullptr, io);
        memcpy(bytes, data.data(), data.size());
        return {bytes, data.size(), 0};
    } catch (...) { return {nullptr, 0, failure()}; }
}
extern "C" int32_t as_private_remove(void *pointer, const char *name, const ASFileProof *expected) {
    try {
        require(pointer && expected);
        auto &directory = *static_cast<Directory *>(pointer);
        directory.validate();
        auto file = relative(directory.handle(), basename(name), GENERIC_READ | READ_CONTROL | DELETE, 1, false);
        ASFileProof actual{};
        readHeld(directory, file.value, maximumFile, actual);
        require(same(actual, *expected, true));
        removeHeld(file.value);
        return 0;
    } catch (...) { int code = failure(); return code == notFound ? 0 : code; }
}

extern "C" int32_t as_private_check_quota(void *pointer, uint32_t maximumEntries,
    uint64_t maximumBytes, int32_t controlOnly) {
    try {
        require(pointer && maximumEntries > 0 && maximumEntries <= 256 && maximumBytes <= 256 * 1024 * 1024, unsafe);
        auto &directory = *static_cast<Directory *>(pointer);
        directory.validate();
        require(directory.ancestors.size() >= 2);
        auto leaf = directory.path.substr(directory.path.find_last_of(L'\\') + 1);
        auto scan = relative(directory.ancestors[directory.ancestors.size() - 2].value, leaf,
            FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | READ_CONTROL, 1, true, nullptr,
            FILE_SHARE_READ | FILE_SHARE_WRITE);
        FILE_ID_INFO original{}, current{};
        require(GetFileInformationByHandleEx(directory.handle(), FileIdInfo, &original, sizeof(original))
            && GetFileInformationByHandleEx(scan.value, FileIdInfo, &current, sizeof(current))
            && original.VolumeSerialNumber == current.VolumeSerialNumber
            && !memcmp(original.FileId.Identifier, current.FileId.Identifier, 16));
        directory.privateACL(scan.value);
        size_t entries = 0;
        uint64_t bytes = 0;
        bool first = true;
        alignas(FILE_ID_BOTH_DIR_INFO) uint8_t buffer[65536];
        while (true) {
            memset(buffer, 0, sizeof(buffer));
            if (!GetFileInformationByHandleEx(scan.value,
                first ? FileIdBothDirectoryRestartInfo : FileIdBothDirectoryInfo, buffer, sizeof(buffer))) {
                require(GetLastError() == ERROR_NO_MORE_FILES, io); break;
            }
            first = false;
            size_t offset = 0;
            while (true) {
                require(offset <= sizeof(buffer) - sizeof(FILE_ID_BOTH_DIR_INFO));
                auto item = reinterpret_cast<FILE_ID_BOTH_DIR_INFO *>(buffer + offset);
                const auto header = offsetof(FILE_ID_BOTH_DIR_INFO, FileName);
                require(item->FileNameLength % sizeof(wchar_t) == 0
                    && item->FileNameLength <= sizeof(buffer) - offset - header);
                std::wstring name(item->FileName, item->FileNameLength / sizeof(wchar_t));
                if (name != L"." && name != L"..") {
                    require(!name.empty() && ++entries <= maximumEntries, tooLarge);
                    require(!(item->FileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_OFFLINE)));
                    require(!controlOnly || name == L"broker.json" || name == L"settings.json");
                    require(item->EndOfFile.QuadPart >= 0 && static_cast<uint64_t>(item->EndOfFile.QuadPart) <= maximumBytes - bytes, tooLarge);
                    bytes += static_cast<uint64_t>(item->EndOfFile.QuadPart);
                }
                if (item->NextEntryOffset == 0) break;
                require(item->NextEntryOffset >= header + item->FileNameLength
                    && item->NextEntryOffset <= sizeof(buffer) - offset);
                offset += item->NextEntryOffset;
            }
        }
        directory.validate();
        return 0;
    } catch (...) { return failure(); }
}

extern "C" ASBytes as_private_files_self_test(const uint16_t *path) {
    std::string stage = "private_root";
    std::string output;
    try {
        int32_t error = 0;
        std::unique_ptr<Directory> directory(static_cast<Directory *>(as_private_directory_open(path, 1, &error)));
        require(directory != nullptr, error);
        // The test runner creates a fresh parent. A unique leaf prefix plus
        // FILE_CREATE means even a reused private directory cannot overwrite data.
        GUID id{}; require(SUCCEEDED(CoCreateGuid(&id)), io);
        wchar_t uuid[40]; require(StringFromGUID2(id, uuid, 40) > 0, io);
        std::wstring prefix = L"fixture-";
        for (auto c : std::wstring(uuid)) if (c != L'{' && c != L'}') prefix += c;
        std::string name(prefix.begin(), prefix.end()); name += ".json";
        std::string staging = name + ".pending", again = name + ".retry", alias = name + ".link";
        const uint8_t abc[] = {'a', 'b', 'c'}, xyz[] = {'x', 'y', 'z'};
        ASFileProof proof{};
        stage = "publish";
        int publishCode = as_private_publish(directory.get(), staging.c_str(), name.c_str(), abc, 3, &proof);
        require(publishCode == 0, publishCode);
        stage = "sha256";
        require(!strcmp(proof.sha256, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
        auto read = [&](const std::string &leaf, uint32_t limit, bool success) {
            ASFileProof info{};
            auto bytes = as_private_read(directory.get(), leaf.c_str(), limit, &info);
            bool ok = bytes.error == 0;
            bool content = bytes.size == 3 && bytes.data && !memcmp(bytes.data, abc, 3);
            as_bytes_free(bytes);
            require(ok == success);
            if (success) require(content && same(info, proof, true));
        };
        stage = "read_back"; read(name, 1024, true);
        stage = "directory_quota";
        require(as_private_check_quota(directory.get(), 256, 1024, 0) == 0);
        require(as_private_check_quota(directory.get(), 256, 2, 0) == tooLarge);
        require(as_private_check_quota(directory.get(), 256, 1024, 1) == unsafe);
        auto quotaName = name + ".quota";
        ASFileProof quotaProof{};
        require(as_private_publish(directory.get(), staging.c_str(), quotaName.c_str(), abc, 3, &quotaProof) == 0);
        require(as_private_check_quota(directory.get(), 1, 1024, 0) == tooLarge);
        require(as_private_remove(directory.get(), quotaName.c_str(), &quotaProof) == 0);
        stage = "collision";
        ASFileProof ignored{};
        require(as_private_publish(directory.get(), again.c_str(), name.c_str(), xyz, 3, &ignored) != 0);
        read(name, 1024, true);
        read(again, 1024, false); // Failed reservation was removed by its own handle.
        stage = "bounded_read"; read(name, 2, false);
        stage = "names";
        for (auto invalid : {"../outside", "..\\outside", "C:/outside", "file:stream", "CON.txt", "LPT1", ".", "..", "file."}) {
            ASFileProof info{};
            auto bytes = as_private_read(directory.get(), invalid, 1024, &info);
            int code = bytes.error; as_bytes_free(bytes); require(code == unsafe);
        }
        stage = "wrong_identity";
        auto wrong = proof; wrong.file_id[0] ^= 1;
        require(as_private_remove(directory.get(), name.c_str(), &wrong) == unsafe);
        wrong = proof; wrong.owner_sid[0] = 'X';
        require(as_private_remove(directory.get(), name.c_str(), &wrong) == unsafe);
        read(name, 1024, true);
        stage = "writer_and_delete_denied";
        {
            auto reader = relative(directory->handle(), basename(name.c_str()), GENERIC_READ | READ_CONTROL, 1, false);
            for (auto access : {GENERIC_WRITE, DELETE}) {
                bool rejected = false;
                try { auto writer = relative(directory->handle(), basename(name.c_str()), access, 1, false,
                    nullptr, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE); }
                catch (...) { rejected = true; }
                require(rejected);
            }
            require(as_private_remove(directory.get(), name.c_str(), &proof) != 0);
        }
        stage = "pinned_directory";
        {
            Handle deletion(CreateFileW(directory->path.c_str(), DELETE,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
            require(deletion.value == INVALID_HANDLE_VALUE && GetLastError() == ERROR_SHARING_VIOLATION);
        }
        stage = "hardlink_rejected";
        auto originalPath = directory->path + L"\\" + basename(name.c_str());
        auto aliasPath = directory->path + L"\\" + basename(alias.c_str());
        require(CreateHardLinkW(aliasPath.c_str(), originalPath.c_str(), nullptr) != 0, io);
        read(name, 1024, false);
        require(as_private_remove(directory.get(), name.c_str(), &proof) == unsafe);
        // Remove only the alias this test exclusively created, after proving its
        // held file ID is the fixture's. This intentionally tests a link count of 2.
        {
            auto link = relative(directory->handle(), basename(alias.c_str()), DELETE | FILE_READ_ATTRIBUTES, 1, false);
            FILE_ID_INFO info{};
            require(ordinary(link.value, false) && GetFileInformationByHandleEx(link.value, FileIdInfo, &info, sizeof(info))
                && info.VolumeSerialNumber == proof.volume && !memcmp(info.FileId.Identifier, proof.file_id, 16));
            removeHeld(link.value);
        }
        // Link creation/deletion changes metadata, so read a fresh proof.
        {
            auto bytes = as_private_read(directory.get(), name.c_str(), 1024, &proof);
            int code = bytes.error; as_bytes_free(bytes); require(code == 0);
        }
        stage = "replacement_survives_cleanup";
        auto previous = proof;
        require(as_private_remove(directory.get(), name.c_str(), &previous) == 0);
        require(as_private_publish(directory.get(), staging.c_str(), name.c_str(), abc, 3, &proof) == 0);
        require(memcmp(previous.file_id, proof.file_id, 16) != 0);
        require(as_private_remove(directory.get(), name.c_str(), &previous) == unsafe);
        read(name, 1024, true);
        stage = "actual_dacl";
        {
            auto file = relative(directory->handle(), basename(name.c_str()), WRITE_DAC | READ_CONTROL, 1, false);
            Local publicSD;
            require(ConvertStringSecurityDescriptorToSecurityDescriptorW(L"D:P(A;;FA;;;WD)", SDDL_REVISION_1,
                &publicSD.value, nullptr) != 0);
            BOOL present = FALSE, defaulted = FALSE; PACL acl = nullptr;
            require(GetSecurityDescriptorDacl(publicSD.value, &present, &acl, &defaulted) && present && acl);
            require(SetSecurityInfo(file.value, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                nullptr, nullptr, acl, nullptr) == ERROR_SUCCESS);
            auto bytes = as_private_read(directory.get(), name.c_str(), 1024, &ignored);
            int code = bytes.error; as_bytes_free(bytes);
            // Restore the fixture through the same held handle, never repair a
            // production path or inherit the parent directory's broad DACL.
            require(GetSecurityDescriptorDacl(directory->security.value, &present, &acl, &defaulted) && present && acl);
            require(SetSecurityInfo(file.value, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                nullptr, nullptr, acl, nullptr) == ERROR_SUCCESS);
            require(code == unsafe);
        }
        stage = "reparse_ancestor";
        std::wstring junction = prefix + L"-junction";
        FILE_ID_INFO junctionID{};
        {
            auto file = relative(directory->handle(), junction, GENERIC_WRITE | READ_CONTROL | DELETE,
                2, true, directory->security.value);
            // Mount points require no symlink privilege. It targets only this
            // self-owned fixture directory; production never follows it.
            auto target = L"\\??\\" + directory->path;
            struct Mount { DWORD tag; WORD length, reserved, subOffset, subLength, printOffset, printLength; };
            auto subBytes = static_cast<WORD>(target.size() * sizeof(wchar_t));
            std::vector<uint8_t> data(sizeof(Mount) + subBytes + 4);
            auto mount = reinterpret_cast<Mount *>(data.data());
            mount->tag = IO_REPARSE_TAG_MOUNT_POINT;
            mount->length = static_cast<WORD>(data.size() - 8);
            mount->subLength = subBytes; mount->printOffset = subBytes + 2;
            memcpy(data.data() + sizeof(Mount), target.data(), subBytes);
            DWORD count = 0;
            require(DeviceIoControl(file.value, FSCTL_SET_REPARSE_POINT, data.data(), static_cast<DWORD>(data.size()),
                nullptr, 0, &count, nullptr) != 0, io);
            require(GetFileInformationByHandleEx(file.value, FileIdInfo, &junctionID, sizeof(junctionID)) != 0);
        }
        auto junctionPath = directory->path + L"\\" + junction;
        void *escaped = as_private_directory_open(reinterpret_cast<const uint16_t *>(junctionPath.c_str()), 0, &error);
        as_private_directory_close(escaped);
        {
            auto file = relative(directory->handle(), junction, DELETE | FILE_READ_ATTRIBUTES, 1, true);
            FILE_ID_INFO info{};
            require(GetFileInformationByHandleEx(file.value, FileIdInfo, &info, sizeof(info))
                && !memcmp(&info, &junctionID, sizeof(info)));
            removeHeld(file.value); // The junction itself, not its target/tree.
        }
        require(escaped == nullptr && error == unsafe);
        stage = "cleanup";
        {
            auto bytes = as_private_read(directory.get(), name.c_str(), 1024, &proof);
            int code = bytes.error; as_bytes_free(bytes); require(code == 0);
        }
        require(as_private_remove(directory.get(), name.c_str(), &proof) == 0);
        require(as_private_remove(directory.get(), name.c_str(), &proof) == 0);
        output = "{\"ok\":true,\"file_id_hash\":true,\"private_dacl\":true,\"exclusive_publish\":true,"
            "\"bounded_read\":true,\"writer_delete_denied\":true,\"pinned_ancestors\":true,"
            "\"hardlinks_rejected\":true,\"reparse_rejected\":true,\"replacement_preserved\":true,\"directory_quota\":true}";
    } catch (...) {
        output = "{\"ok\":false,\"stage\":\"" + stage + "\",\"code\":" + std::to_string(failure()) + "}";
    }
    auto data = static_cast<uint8_t *>(malloc(output.size()));
    if (!data) return {nullptr, 0, io};
    memcpy(data, output.data(), output.size());
    return {data, output.size(), 0};
}
