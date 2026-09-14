#include "AstraWindowsNative.h"
#include <windows.h>
#include <sddl.h>
#include <tlhelp32.h>
#include <dwmapi.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wincodec.h>
#include <uiautomation.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {
using winrt::com_ptr;
using winrt::check_hresult;
using winrt::handle;
struct Apartment {
    HRESULT status = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    Apartment() { check_hresult(status); }
    ~Apartment() { if (SUCCEEDED(status)) CoUninitialize(); }
};
ASBytes failure(int32_t code = 1) { return {nullptr, 0, code}; }
ASBytes copy_bytes(const void *data, size_t count) {
    auto p = static_cast<uint8_t *>(malloc(count ? count : 1));
    if (!p) return failure();
    if (count) memcpy(p, data, count);
    return {p, count, 0};
}
std::string utf8(const wchar_t *text, int count = -1) {
    if (!text) return {};
    if (count < 0) count = static_cast<int>(wcslen(text));
    int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, count, nullptr, 0, nullptr, nullptr);
    if (size <= 0) return {};
    std::string out(size, 0);
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, count, out.data(), size, nullptr, nullptr)) return {};
    return out;
}
template<size_t N> bool copy_string(char (&out)[N], const std::string &value) {
    if (value.size() >= N) return false;
    memcpy(out, value.c_str(), value.size() + 1);
    return true;
}
std::string json_string(const std::string &s) {
    std::string out = "\"";
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') { out += '\\'; out += c; }
        else if (c < 32) {
            char buffer[7]; snprintf(buffer, sizeof(buffer), "\\u%04x", c); out += buffer;
        } else out += c;
    }
    return out + "\"";
}
bool default_desktop() {
    HDESK desk = OpenInputDesktop(0, FALSE, DESKTOP_READOBJECTS);
    if (!desk) return false;
    wchar_t name[128] = {}; DWORD needed = 0;
    bool ok = GetUserObjectInformationW(desk, UOI_NAME, name, sizeof(name), &needed)
        && _wcsicmp(name, L"Default") == 0;
    CloseDesktop(desk);
    return ok;
}
bool window_info(HWND hwnd, ASWindow *out) {
    if (!hwnd || !out || !IsWindow(hwnd) || !IsWindowVisible(hwnd) || IsIconic(hwnd)) return false;
    DWORD cloaked = 0;
    if (FAILED(DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, &cloaked, sizeof(cloaked))) || cloaked) return false;
    DWORD affinity = 0;
    if (!GetWindowDisplayAffinity(hwnd, &affinity) || affinity != WDA_NONE) return false;
    DWORD pid = 0; GetWindowThreadProcessId(hwnd, &pid);
    ASWindow result{};
    if (!as_process_identity(pid, &result.process)) return false;
    ASProcess current{};
    if (!as_process_identity(GetCurrentProcessId(), &current) || strcmp(current.sid, result.process.sid)) return false;
    RECT bounds{};
    if (!GetWindowRect(hwnd, &bounds) || bounds.right <= bounds.left || bounds.bottom <= bounds.top) return false;
    result.handle = reinterpret_cast<uint64_t>(hwnd);
    result.x = bounds.left; result.y = bounds.top;
    result.width = bounds.right - bounds.left; result.height = bounds.bottom - bounds.top;
    wchar_t title[2048]{}; GetWindowTextW(hwnd, title, 2048);
    handle process(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid));
    wchar_t executable[32768]{}; DWORD size = 32768;
    if (!process || !QueryFullProcessImageNameW(process.get(), 0, executable, &size)) return false;
    auto basename = wcsrchr(executable, L'\\');
    if (!copy_string(result.title, utf8(title)) || !copy_string(result.app, utf8(basename ? basename + 1 : executable))) return false;
    *out = result;
    return true;
}
struct UIAContext {
    Apartment apartment;
    com_ptr<IUIAutomation> automation;
    com_ptr<IUIAutomationTreeWalker> walker;
    UIAContext() {
        check_hresult(CoCreateInstance(__uuidof(CUIAutomation8), nullptr, CLSCTX_INPROC_SERVER,
            __uuidof(IUIAutomation), automation.put_void()));
        check_hresult(automation->get_ControlViewWalker(walker.put()));
    }
};
std::string take_bstr(BSTR value, uint32_t limit) {
    std::string s = utf8(value, static_cast<int>(std::min<uint32_t>(SysStringLen(value), limit)));
    SysFreeString(value);
    return s;
}
}

extern "C" void as_bytes_free(ASBytes bytes) { free(bytes.data); }
extern "C" uint32_t as_current_pid() { return GetCurrentProcessId(); }
extern "C" uint64_t as_monotonic_ns() {
    LARGE_INTEGER value{}, frequency{};
    if (!QueryPerformanceCounter(&value) || !QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0) return 0;
    return (value.QuadPart / frequency.QuadPart) * 1000000000ull
        + (value.QuadPart % frequency.QuadPart) * 1000000000ull / frequency.QuadPart;
}
extern "C" int32_t as_process_identity(uint32_t pid, ASProcess *out) {
    if (!pid || !out) return 0;
    handle process(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, pid));
    if (!process || WaitForSingleObject(process.get(), 0) != WAIT_TIMEOUT) return 0;
    FILETIME created{}, exit{}, kernel{}, user{};
    if (!GetProcessTimes(process.get(), &created, &exit, &kernel, &user)) return 0;
    HANDLE raw = nullptr;
    if (!OpenProcessToken(process.get(), TOKEN_QUERY, &raw)) return 0;
    handle token(raw);
    DWORD size = 0; GetTokenInformation(token.get(), TokenUser, nullptr, 0, &size);
    if (!size || size > 65536) return 0;
    std::vector<uint8_t> data(size);
    if (!GetTokenInformation(token.get(), TokenUser, data.data(), size, &size)) return 0;
    LPSTR sid = nullptr;
    if (!ConvertSidToStringSidA(reinterpret_cast<TOKEN_USER *>(data.data())->User.Sid, &sid)) return 0;
    ASProcess result{}; result.pid = pid;
    result.created = (static_cast<uint64_t>(created.dwHighDateTime) << 32) | created.dwLowDateTime;
    bool ok = copy_string(result.sid, std::string(sid));
    LocalFree(sid);
    if (!ok || !result.created || WaitForSingleObject(process.get(), 0) != WAIT_TIMEOUT) return 0;
    *out = result;
    return 1;
}
extern "C" uint32_t as_parent_pid(uint32_t pid) {
    handle snapshot(CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0));
    if (!snapshot || snapshot.get() == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W item{}; item.dwSize = sizeof(item);
    if (Process32FirstW(snapshot.get(), &item)) do {
        if (item.th32ProcessID == pid) return item.th32ParentProcessID;
    } while (Process32NextW(snapshot.get(), &item));
    return 0;
}
extern "C" int32_t as_foreground_window(ASWindow *out) {
    if (!default_desktop()) return 0;
    HWND window = GetForegroundWindow();
    return window_info(window, out) && GetForegroundWindow() == window;
}
extern "C" int32_t as_window_matches(const ASWindow *expected, int32_t foreground) {
    if (!expected || !default_desktop()) return 0;
    ASWindow current{};
    HWND window = reinterpret_cast<HWND>(expected->handle);
    if (foreground && GetForegroundWindow() != window) return 0;
    if (!window_info(window, &current)) return 0;
    return current.handle == expected->handle && current.process.pid == expected->process.pid
        && current.process.created == expected->process.created && strcmp(current.process.sid, expected->process.sid) == 0
        && current.x == expected->x && current.y == expected->y
        && current.width == expected->width && current.height == expected->height;
}
extern "C" int32_t as_window_process(uint64_t window, ASProcess *out) {
    if (!out || !default_desktop()) return 0;
    ASWindow current{};
    if (!window_info(reinterpret_cast<HWND>(window), &current)) return 0;
    *out = current.process;
    return 1;
}
extern "C" ASBytes as_capture_png(uint64_t window, uint32_t timeoutMs) {
    try {
        Apartment apartment;
        using namespace winrt::Windows::Graphics::Capture;
        using namespace winrt::Windows::Graphics::DirectX;
        using namespace winrt::Windows::Graphics::DirectX::Direct3D11;
        if (!GraphicsCaptureSession::IsSupported()) return failure();
        auto factory = winrt::get_activation_factory<GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
        GraphicsCaptureItem item{nullptr};
        check_hresult(factory->CreateForWindow(reinterpret_cast<HWND>(window),
            winrt::guid_of<GraphicsCaptureItem>(), winrt::put_abi(item)));
        auto size = item.Size();
        if (size.Width <= 0 || size.Height <= 0 || size.Width > 16384 || size.Height > 16384
            || static_cast<uint64_t>(size.Width) * size.Height > 32000000) return failure(2);
        com_ptr<ID3D11Device> device; com_ptr<ID3D11DeviceContext> context;
        check_hresult(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION, device.put(), nullptr, context.put()));
        auto dxgi = device.as<IDXGIDevice>();
        com_ptr<IInspectable> inspectable;
        check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi.get(), inspectable.put()));
        auto directDevice = inspectable.as<IDirect3DDevice>();
        auto pool = Direct3D11CaptureFramePool::CreateFreeThreaded(directDevice,
            DirectXPixelFormat::B8G8R8A8UIntNormalized, 1, size);
        auto session = pool.CreateCaptureSession(item);
        session.IsCursorCaptureEnabled(false);
        // Keep the OS capture border; no broad-screen or PrintWindow fallback.
        session.StartCapture();
        Direct3D11CaptureFrame frame{nullptr};
        const auto end = GetTickCount64() + std::min<uint32_t>(timeoutMs, 5000);
        while (!frame && GetTickCount64() < end) { frame = pool.TryGetNextFrame(); if (!frame) Sleep(5); }
        if (!frame) { session.Close(); pool.Close(); return failure(3); }
        if (frame.ContentSize().Width != size.Width || frame.ContentSize().Height != size.Height) return failure(4);
        auto access = frame.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
        com_ptr<ID3D11Texture2D> texture;
        check_hresult(access->GetInterface(__uuidof(ID3D11Texture2D), texture.put_void()));
        D3D11_TEXTURE2D_DESC desc{}; texture->GetDesc(&desc);
        desc.Usage = D3D11_USAGE_STAGING; desc.BindFlags = 0; desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ; desc.MiscFlags = 0;
        com_ptr<ID3D11Texture2D> staging; check_hresult(device->CreateTexture2D(&desc, nullptr, staging.put()));
        context->CopyResource(staging.get(), texture.get());
        D3D11_MAPPED_SUBRESOURCE mapped{};
        check_hresult(context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &mapped));
        struct Unmap { ID3D11DeviceContext *context; ID3D11Texture2D *texture; ~Unmap() { context->Unmap(texture, 0); } } unmap{context.get(), staging.get()};
        com_ptr<IWICImagingFactory> wic;
        check_hresult(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER, __uuidof(IWICImagingFactory), wic.put_void()));
        com_ptr<IStream> stream; check_hresult(CreateStreamOnHGlobal(nullptr, TRUE, stream.put()));
        com_ptr<IWICBitmapEncoder> encoder; check_hresult(wic->CreateEncoder(GUID_ContainerFormatPng, nullptr, encoder.put()));
        check_hresult(encoder->Initialize(stream.get(), WICBitmapEncoderNoCache));
        com_ptr<IWICBitmapFrameEncode> output; check_hresult(encoder->CreateNewFrame(output.put(), nullptr));
        check_hresult(output->Initialize(nullptr)); check_hresult(output->SetSize(size.Width, size.Height));
        WICPixelFormatGUID format = GUID_WICPixelFormat32bppBGRA;
        check_hresult(output->SetPixelFormat(&format));
        if (format != GUID_WICPixelFormat32bppBGRA) return failure();
        check_hresult(output->WritePixels(size.Height, mapped.RowPitch, mapped.RowPitch * size.Height, static_cast<BYTE *>(mapped.pData)));
        check_hresult(output->Commit()); check_hresult(encoder->Commit());
        STATSTG stat{}; check_hresult(stream->Stat(&stat, STATFLAG_NONAME));
        if (!stat.cbSize.QuadPart || stat.cbSize.QuadPart > 10 * 1024 * 1024) return failure(2);
        std::vector<uint8_t> png(static_cast<size_t>(stat.cbSize.QuadPart));
        LARGE_INTEGER zero{}; check_hresult(stream->Seek(zero, STREAM_SEEK_SET, nullptr));
        ULONG read = 0; check_hresult(stream->Read(png.data(), static_cast<ULONG>(png.size()), &read));
        if (read != png.size()) return failure();
        frame.Close(); session.Close(); pool.Close();
        return copy_bytes(png.data(), png.size());
    } catch (...) { return failure(); }
}

extern "C" void *as_uia_open() { try { return new UIAContext(); } catch (...) { return nullptr; } }
extern "C" void as_uia_close(void *context) { delete static_cast<UIAContext *>(context); }
extern "C" void *as_uia_root(void *context, uint64_t window) {
    if (!context) return nullptr;
    IUIAutomationElement *element = nullptr;
    if (FAILED(static_cast<UIAContext *>(context)->automation->ElementFromHandle(reinterpret_cast<HWND>(window), &element))) return nullptr;
    return element;
}
extern "C" void *as_uia_first_child(void *context, void *element) {
    if (!context || !element) return nullptr;
    IUIAutomationElement *child = nullptr;
    if (FAILED(static_cast<UIAContext *>(context)->walker->GetFirstChildElement(static_cast<IUIAutomationElement *>(element), &child))) return nullptr;
    return child;
}
extern "C" void *as_uia_next_sibling(void *context, void *element) {
    if (!context || !element) return nullptr;
    IUIAutomationElement *sibling = nullptr;
    if (FAILED(static_cast<UIAContext *>(context)->walker->GetNextSiblingElement(static_cast<IUIAutomationElement *>(element), &sibling))) return nullptr;
    return sibling;
}
extern "C" void as_uia_release(void *element) { if (element) static_cast<IUIAutomationElement *>(element)->Release(); }
extern "C" ASBytes as_uia_properties(void *raw, uint32_t maxCharacters) {
    if (!raw || maxCharacters > 65536) return failure();
    auto element = static_cast<IUIAutomationElement *>(raw);
    BOOL password = TRUE;
    if (FAILED(element->get_CurrentIsPassword(&password))) return failure();
    // Do not read even the name of a password control.
    if (password) return copy_bytes("{\"password\":true}", 17);
    BSTR name = nullptr; CONTROLTYPEID role = 0;
    if (FAILED(element->get_CurrentControlType(&role)) || FAILED(element->get_CurrentName(&name))) return failure();
    bool truncated = SysStringLen(name) > maxCharacters;
    std::string result = "{\"password\":false,\"control_type\":" + std::to_string(role)
        + ",\"name\":" + json_string(take_bstr(name, maxCharacters));
    com_ptr<IUIAutomationTextPattern> textPattern;
    if (SUCCEEDED(element->GetCurrentPatternAs(UIA_TextPatternId, __uuidof(IUIAutomationTextPattern), textPattern.put_void())) && textPattern) {
        com_ptr<IUIAutomationTextRange> range;
        if (SUCCEEDED(textPattern->get_DocumentRange(range.put()))) {
            BSTR text = nullptr;
            if (SUCCEEDED(range->GetText(static_cast<int>(maxCharacters + 1), &text))) {
                truncated = truncated || SysStringLen(text) > maxCharacters;
                result += ",\"text\":" + json_string(take_bstr(text, maxCharacters));
            }
        }
    }
    result += truncated ? ",\"text_truncated\":true}" : ",\"text_truncated\":false}";
    return copy_bytes(result.data(), result.size());
}

extern "C" ASBytes as_run_worker(const uint16_t *executable, const uint16_t *arguments,
    uint32_t timeoutMs, uint32_t maximumBytes, ASCancelled cancelled, void *context) {
    if (!executable || !arguments || !timeoutMs || timeoutMs > 5000 || maximumBytes > 16 * 1024 * 1024) return failure();
    SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
    HANDLE readRaw = nullptr, writeRaw = nullptr;
    if (!CreatePipe(&readRaw, &writeRaw, &security, 0)) return failure();
    handle read(readRaw), write(writeRaw);
    if (!SetHandleInformation(read.get(), HANDLE_FLAG_INHERIT, 0)) return failure();
    handle input(CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_EXISTING, 0, nullptr));
    handle error(CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_EXISTING, 0, nullptr));
    handle job(CreateJobObjectW(nullptr, nullptr));
    if (!input || !error || !job) return failure();
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY;
    limits.BasicLimitInformation.ActiveProcessLimit = 1;
    limits.ProcessMemoryLimit = 768ull * 1024 * 1024;
    if (!SetInformationJobObject(job.get(), JobObjectExtendedLimitInformation, &limits, sizeof(limits))) return failure();
    SIZE_T size = 0; InitializeProcThreadAttributeList(nullptr, 1, 0, &size);
    std::vector<uint8_t> attributes(size);
    STARTUPINFOEXW startup{}; startup.StartupInfo.cb = sizeof(startup);
    startup.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(attributes.data());
    if (!InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0, &size)) return failure();
    struct AttrCleanup { LPPROC_THREAD_ATTRIBUTE_LIST list; ~AttrCleanup() { DeleteProcThreadAttributeList(list); } } cleanup{startup.lpAttributeList};
    HANDLE inherited[] = {write.get(), input.get(), error.get()};
    if (!UpdateProcThreadAttribute(startup.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        inherited, sizeof(inherited), nullptr, nullptr)) return failure();
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    startup.StartupInfo.wShowWindow = SW_HIDE;
    startup.StartupInfo.hStdOutput = write.get(); startup.StartupInfo.hStdInput = input.get(); startup.StartupInfo.hStdError = error.get();
    const wchar_t *exe = reinterpret_cast<const wchar_t *>(executable);
    std::wstring command = L"\"" + std::wstring(exe) + L"\" " + reinterpret_cast<const wchar_t *>(arguments);
    PROCESS_INFORMATION info{};
    const ULONGLONG end = GetTickCount64() + timeoutMs;
    if (!CreateProcessW(exe, command.data(), nullptr, nullptr, TRUE,
        CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
        nullptr, nullptr, &startup.StartupInfo, &info)) return failure();
    handle process(info.hProcess), thread(info.hThread);
    if (!AssignProcessToJobObject(job.get(), process.get())) {
        TerminateProcess(process.get(), 1); WaitForSingleObject(process.get(), 1000); return failure();
    }
    if (ResumeThread(thread.get()) == static_cast<DWORD>(-1)) return failure();
    write.close();
    std::vector<uint8_t> output;
    while (GetTickCount64() < end) {
        if (cancelled && cancelled(context)) {
            TerminateJobObject(job.get(), 1);
            WaitForSingleObject(process.get(), 1000);
            return failure(5);
        }
        DWORD available = 0;
        if (!PeekNamedPipe(read.get(), nullptr, 0, nullptr, &available, nullptr)) {
            if (GetLastError() != ERROR_BROKEN_PIPE) return failure();
            if (WaitForSingleObject(process.get(), 0) == WAIT_OBJECT_0) {
                DWORD code = 1; GetExitCodeProcess(process.get(), &code);
                return code == 0 ? copy_bytes(output.data(), output.size()) : failure();
            }
        } else if (available) {
            if (output.size() + available > maximumBytes) return failure(2);
            size_t before = output.size(); output.resize(before + available);
            DWORD readCount = 0;
            if (!ReadFile(read.get(), output.data() + before, available, &readCount, nullptr)) return failure();
            output.resize(before + readCount);
            continue;
        }
        Sleep(2);
    }
    TerminateJobObject(job.get(), 1);
    WaitForSingleObject(process.get(), 1000);
    return failure(3);
}

namespace {
std::thread fixtureThread;
std::atomic<HWND> fixtureWindow{nullptr};
std::atomic<bool> fixtureStarted{false};
constexpr int fixtureStartButton = 1001;
LRESULT CALLBACK fixture_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == WM_COMMAND && LOWORD(wparam) == fixtureStartButton && HIWORD(wparam) == BN_CLICKED) {
        // A real user starts this fixture. Showing the prompt must not bypass
        // the production foreground check or start capture in another window.
        if (window == fixtureWindow.load() && GetForegroundWindow() == window) {
            fixtureStarted = true;
            SetWindowPos(window, HWND_NOTOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        }
        return 0;
    }
    if (message == WM_DESTROY) { PostQuitMessage(0); return 0; }
    return DefWindowProcW(window, message, wparam, lparam);
}
}
extern "C" uint64_t as_fixture_open() {
    if (fixtureThread.joinable()) return 0;
    fixtureStarted = false;
    fixtureThread = std::thread([] {
        CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
        WNDCLASSW cls{}; cls.hInstance = GetModuleHandleW(nullptr);
        cls.lpfnWndProc = fixture_proc; cls.lpszClassName = L"AstraAppshotTestFixture";
        cls.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
        cls.hCursor = LoadCursorA(nullptr, IDC_ARROW);
        RegisterClassW(&cls);
        RECT work{};
        int x = CW_USEDEFAULT, y = CW_USEDEFAULT;
        if (SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0)) {
            x = work.left + std::max<LONG>(0, (work.right - work.left - 640) / 2);
            y = work.top + std::max<LONG>(0, (work.bottom - work.top - 420) / 2);
        }
        HWND window = CreateWindowExW(WS_EX_APPWINDOW, cls.lpszClassName, L"Astra Appshot controlled test",
            WS_OVERLAPPEDWINDOW, x, y, 640, 420, nullptr, nullptr, cls.hInstance, nullptr);
        if (window) {
            CreateWindowExW(0, L"STATIC", L"Astra fixture: readable UI text 12345", WS_CHILD | WS_VISIBLE,
                20, 40, 560, 40, window, nullptr, cls.hInstance, nullptr);
            CreateWindowExW(0, L"EDIT", L"fixture-password-must-not-appear", WS_CHILD | WS_VISIBLE | ES_PASSWORD,
                20, 100, 500, 32, window, nullptr, cls.hInstance, nullptr);
            CreateWindowExW(0, L"STATIC", L"Click Start test, then keep this window in front.\nOnly this test window is captured. It closes automatically.",
                WS_CHILD | WS_VISIBLE, 20, 160, 570, 55, window, nullptr, cls.hInstance, nullptr);
            CreateWindowExW(0, L"BUTTON", L"Start test / \u5f00\u59cb\u6d4b\u8bd5", WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_DEFPUSHBUTTON,
                20, 235, 260, 50, window, reinterpret_cast<HMENU>(fixtureStartButton), cls.hInstance, nullptr);
            // Test prompt only: visible above other windows, without stealing
            // keyboard focus. The user must activate it and press Start test.
            ShowWindow(window, SW_SHOWNOACTIVATE);
            SetWindowPos(window, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
            UpdateWindow(window);
        }
        fixtureWindow = window;
        MSG message{};
        while (window && GetMessageW(&message, nullptr, 0, 0) > 0) {
            TranslateMessage(&message); DispatchMessageW(&message);
        }
        fixtureWindow = nullptr;
        CoUninitialize();
    });
    const auto end = GetTickCount64() + 1000;
    while (!fixtureWindow.load() && GetTickCount64() < end) Sleep(5);
    return reinterpret_cast<uint64_t>(fixtureWindow.load());
}
extern "C" void as_fixture_close(uint64_t window) {
    DWORD pid = 0;
    if (window) GetWindowThreadProcessId(reinterpret_cast<HWND>(window), &pid);
    if (reinterpret_cast<HWND>(window) == fixtureWindow.load() && window && pid == GetCurrentProcessId())
        PostMessageW(reinterpret_cast<HWND>(window), WM_CLOSE, 0, 0);
    if (fixtureThread.joinable()) fixtureThread.join();
}
extern "C" uint32_t as_fixture_status(uint64_t window) {
    auto target = reinterpret_cast<HWND>(window);
    DWORD pid = 0;
    if (target) GetWindowThreadProcessId(target, &pid);
    if (!target || target != fixtureWindow.load() || pid != GetCurrentProcessId()) return 0;
    uint32_t result = 1;
    if (IsWindowVisible(target) && !IsIconic(target)) result |= 2;
    if (GetForegroundWindow() == target) result |= 4;
    if (default_desktop()) result |= 8;
    ASWindow own{};
    if (window_info(target, &own)) result |= 16;
    if (fixtureStarted.load()) result |= 32;
    return result;
}
