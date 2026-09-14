#include "AstraWindowsNative.h"
#include <windows.h>
#include <string>
#include <cstring>
#include <memory>
#include <mutex>

namespace {
struct Hotkey {
    HWND window = nullptr;
    DWORD thread = GetCurrentThreadId();
    int id = 1;
    UINT modifiers = 0, key = 0;
    bool enabled = false;
    Hotkey() {
        static std::once_flag registered;
        std::call_once(registered, [] {
            WNDCLASSW type{}; type.lpfnWndProc = DefWindowProcW;
            type.hInstance = GetModuleHandleW(nullptr); type.lpszClassName = L"AstraAppshotHotkey";
            RegisterClassW(&type);
        });
        window = CreateWindowExW(0, L"AstraAppshotHotkey", L"", 0, 0, 0, 0, 0,
            HWND_MESSAGE, nullptr, GetModuleHandleW(nullptr), nullptr);
    }
    ~Hotkey() { if (window) { if (enabled) UnregisterHotKey(window, id); DestroyWindow(window); } }
    bool owner() const { return window && thread == GetCurrentThreadId(); }
    int replace(UINT flags, UINT value, bool on) {
        if (!owner()) return 21;
        // Never occupy unmodified keys or the OS-reserved Windows-key namespace.
        if (on && (!flags || (flags & ~(MOD_CONTROL | MOD_ALT | MOD_SHIFT)) ||
            !((value >= 'A' && value <= 'Z') || (value >= '0' && value <= '9') || (value >= VK_F1 && value <= VK_F24)))) return 20;
        if (on && enabled && flags == modifiers && value == key) return 0;
        if (!on) {
            if (enabled && !UnregisterHotKey(window, id)) return 21;
            enabled = false;
        } else {
            const int candidate = id == 1 ? 2 : 1;
            if (!RegisterHotKey(window, candidate, flags | MOD_NOREPEAT, value))
                return GetLastError() == ERROR_HOTKEY_ALREADY_REGISTERED ? 23 : 21;
            if (enabled && !UnregisterHotKey(window, id)) { UnregisterHotKey(window, candidate); return 21; }
            id = candidate; modifiers = flags; key = value; enabled = true;
        }
        MSG event{};
        while (PeekMessageW(&event, window, WM_HOTKEY, WM_HOTKEY, PM_REMOVE)) {}
        return 0;
    }
};
}
extern "C" void *as_hotkey_open(int32_t *error) {
    if (error) *error = 0;
    try {
        auto result = std::make_unique<Hotkey>();
        if (!result->owner()) { if (error) *error = 21; return nullptr; }
        return result.release();
    } catch (...) { if (error) *error = 21; return nullptr; }
}
extern "C" int32_t as_hotkey_replace(void *hotkey, uint32_t modifiers, uint32_t key, int32_t enabled) {
    return hotkey ? static_cast<Hotkey *>(hotkey)->replace(modifiers, key, enabled != 0) : 20;
}
extern "C" int32_t as_hotkey_poll(void *hotkey) {
    if (!hotkey) return 0;
    auto &state = *static_cast<Hotkey *>(hotkey);
    if (!state.owner()) return -21;
    MSG event{}; bool triggered = false;
    while (PeekMessageW(&event, state.window, WM_HOTKEY, WM_HOTKEY, PM_REMOVE)) {
        if (state.enabled && event.wParam == state.id && HIWORD(event.lParam) == state.key
            && LOWORD(event.lParam) == state.modifiers) triggered = true;
    }
    return triggered ? 1 : 0;
}
extern "C" void as_hotkey_close(void *hotkey) { delete static_cast<Hotkey *>(hotkey); }
extern "C" ASBytes as_hotkey_self_test() {
    std::string output;
    try {
        auto first = std::make_unique<Hotkey>(), second = std::make_unique<Hotkey>();
        const UINT modifiers = MOD_CONTROL | MOD_ALT | MOD_SHIFT;
        auto check = [](bool value) { if (!value) throw 1; };
        check(first->owner() && second->owner());
        int available = first->replace(modifiers, VK_F23, true);
        if (available == 23) output = "{\"ok\":true,\"hotkey_test_skipped\":\"fixture_chord_in_use\"}";
        else {
            check(available == 0);
            check(second->replace(modifiers, VK_F23, true) == 23);
            check(first->replace(modifiers, VK_F23, true) == 0);
            check(first->replace(0, 'Z', true) == 20 && first->enabled);
            check(first->replace(MOD_WIN, 'Z', true) == 20 && first->enabled);
            check(second->replace(modifiers, VK_F24, true) == 0);
            check(first->replace(modifiers, VK_F24, true) == 23 && first->key == VK_F23 && first->enabled);
            check(first->replace(0, 0, false) == 0);
            check(second->replace(modifiers, VK_F23, true) == 0);
            second.reset();
            check(first->replace(modifiers, VK_F23, true) == 0);
            // No keyboard events are synthesized and no capture callback exists here.
            output = "{\"ok\":true,\"hotkey_conflict\":true,\"invalid_chord_rejected\":true,"
                "\"failed_replace_preserves_old\":true,\"disable_unregisters\":true,\"close_unregisters\":true}";
        }
    } catch (...) { output = "{\"ok\":false,\"stage\":\"hotkey_lifecycle\"}"; }
    auto bytes = static_cast<uint8_t *>(malloc(output.size()));
    if (!bytes) return {nullptr, 0, 21};
    memcpy(bytes, output.data(), output.size());
    return {bytes, output.size(), 0};
}
