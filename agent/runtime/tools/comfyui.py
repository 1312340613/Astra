"""ComfyUI image generation tools."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import random
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    fcntl = None
else:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - unavailable on some POSIX hosts
        fcntl = None

from ..process_env import hidden_process_creationflags
from .registry import ToolDef, ToolRegistry

MAX_SEED = 2**63 - 1
NATIVE_METADATA_SCHEMA_VERSION = 1


class _NativeLifecycleBusyError(OSError):
    pass


def _posix_getpgid(pid: int) -> int:
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:
        raise OSError("Native process groups are unavailable on this platform")
    return int(getpgid(pid))


def _posix_killpg(pgid: int, signal_number: int) -> None:
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        raise OSError("Native process groups are unavailable on this platform")
    killpg(pgid, signal_number)


BASE_TAGS = (
    "masterpiece, best quality, 1girl, catgirl, cat ears, "
    "long black hair, black hair, red eyes, crimson eyes, ahoge, tail, "
    "slender, small breasts, sharp focus, beautiful detailed face"
)

NEGATIVE_TEMPLATE = (
    "embedding:easynegative, anatomical nonsense, interlocked fingers, "
    "extra fingers, watermark, simple background, transparent, low quality, "
    "logo, text, signature, (worst quality, bad quality:1.2), jpeg artifacts, "
    "username, censored, extra digit, ugly, bad_hands, bad_feet, bad_anatomy, "
    "deformed anatomy, bad proportions, lowres, bad_quality, "
    "1boy, male, penis, nsfw, nude, nipples, explicit, erotic, "
    "blue eyes, green eyes, brown eyes, yellow eyes"
)

HAIR_TAGS = {
    "up": (
        "hair up, hair bun, pinned up hair, (swept back:1.3), "
        "(no bangs:1.2), (forehead visible:1.2), exposed neck, elegant, mature"
    ),
    "down": "hair down, loose hair, long hair flowing, cute, sweet, innocent",
}

PRESETS = {
    "monochrome": {
        "cfg": 4.5,
        "steps": 28,
        "width": 896,
        "height": 1152,
        "positive": (
            "spot color, fine lineart, intricate linework, monochrome, detailed, "
            "highly detailed, shading, subtle shading, depth"
        ),
        "negative": "",
    },
    "sunset": {
        "cfg": 4.8,
        "steps": 30,
        "width": 1152,
        "height": 896,
        "positive": (
            "full color, vibrant, colorful, fine lineart, intricate linework, "
            "sunset, golden hour, orange sky, warm light"
        ),
        "negative": "spot color, monochrome",
    },
    "starry": {
        "cfg": 4.8,
        "steps": 30,
        "width": 1152,
        "height": 896,
        "positive": (
            "full color, vibrant, colorful, fine lineart, intricate linework, "
            "night sky, starry sky, milky way, stars, twilight, deep blue sky, "
            "purple sky gradient, city lights below, moonlight"
        ),
        "negative": "spot color, monochrome",
    },
    "custom": {
        "cfg": 4.5,
        "steps": 28,
        "width": 896,
        "height": 1152,
        "positive": "",
        "negative": "",
    },
}


ANIMA_MODELS = {
    "v1": "oneObsessionAnima_v10.safetensors",
    "v2": "oneObsessionAnima_v20.safetensors",
    "v3": "oneObsessionAnima_v30.safetensors",
}
ANIMA_TURBO_LORA = "anima-turbo-lora-v0.2.safetensors"
ANIMA_HIGHRES_LORA = "anima-highres-aesthetic-boost.safetensors"
ANIMA_SATURATION_LORA = "saturation_v6.safetensors"
ANIMA_LYRA_V2_LORA = "lyra_anima_lineart_v2/lyra_anima_lineart_v2-step00000600.safetensors"
ANIMA_CLIP = "qwen_3_06b_base.safetensors"
ANIMA_VAE = "qwen_image_vae.safetensors"

ANIMA_QUALITY = (
    "masterpiece, best quality, score_9, score_8, score_7, year 2025, newest, "
    "highres, absurdres, very aesthetic"
)
ANIMA_IDENTITY = (
    "1girl, solo, catgirl, cat ears, cat tail, black hair, long black hair, "
    "red eyes, crimson eyes, pale skin, small face, delicate face, "
    "beautiful detailed face, elegant anime lineart"
)
ANIMA_HAIR = {
    "soft": (
        "lyra_soft_bangs, hair down, soft bangs, slightly curved bangs, "
        "loose side locks, gentle cute expression"
    ),
    "hairup": (
        "lyra_mature_hairup, (hair up:1.5), (swept back:1.4), "
        "(no bangs:1.3), (forehead visible:1.3), low bun, loose side locks"
    ),
}
ANIMA_STYLE = (
    "lyra_lineart_v2, monochrome, greyscale, black and white, fine lineart, "
    "intricate linework, clean ink lines, delicate hatching, grayscale shading, "
    "high contrast, red eyes as only color"
)
ANIMA_PURE_BW_RED_STYLE = (
    "lyra_lineart_v2, pure grayscale, true black and white, black and white only, "
    "except red eyes, spot color red eyes only, only red eyes are colored, "
    "no other color, fully desaturated except eyes, fine lineart, intricate linework"
)
ANIMA_PURE_BW_STYLE = (
    "lyra_lineart_v2, pure grayscale, true black and white, no color at all, "
    "fully desaturated, black ink on white paper, fine lineart, intricate linework"
)
ANIMA_NEGATIVE = (
    "worst quality, low quality, score_1, score_2, score_3, blurry, lowres, "
    "jpeg artifacts, bad anatomy, bad hands, extra fingers, missing fingers, "
    "text, watermark, logo, artist name, full color, vibrant, colorful, saturated, "
    "warm skin color, pink skin, orange skin, rainbow palette, neon colors, "
    "oil painting, watercolor, painterly, rough sketch, thick outline, simplistic, "
    "flat cartoon, 3d render, photorealistic, plastic skin, blue eyes, green eyes, "
    "brown eyes, yellow eyes"
)
ANIMA_SFW_BLOCKERS = "nsfw, nude, nipples, explicit, erotic, penis, testicles, cum"
ANIMA_PROFILES = {
    "signature-muted": {"saturation": -1.8, "style": ANIMA_STYLE},
    "mono-extreme": {"saturation": -3.2, "style": ANIMA_STYLE},
    "pure-bw-red": {"saturation": -3.2, "style": ANIMA_PURE_BW_RED_STYLE},
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _wsl_lifecycle_config() -> tuple[str, str, str, str]:
    distro = os.getenv("COMFYUI_WSL_DISTRO", "Ubuntu").strip()
    root = os.getenv("COMFYUI_WSL_ROOT", "").strip().rstrip("/")
    python = os.getenv("COMFYUI_WSL_PYTHON", ".venv/bin/python").strip()
    log_path = os.getenv("COMFYUI_WSL_LOG", "/tmp/agent-system-comfyui.log").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", distro):
        raise ValueError("COMFYUI_WSL_DISTRO contains unsupported characters")
    if not root:
        raise ValueError("Set COMFYUI_WSL_ROOT to the absolute ComfyUI directory inside WSL")
    if not root.startswith("/") or not python or not log_path.startswith("/"):
        raise ValueError("ComfyUI WSL root/log must be absolute and Python must be non-empty")
    return distro, root, python, log_path


def _run_wsl(script: str, *, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    distro, _, _, _ = _wsl_lifecycle_config()
    # Windows' command-line quoting can consume shell variables and nested
    # quotes before bash sees them. Send an opaque base64 payload so lifecycle
    # scripts arrive byte-for-byte and the model never has to solve this layer.
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    bootstrap = f"echo {encoded} | base64 -d | bash"
    return subprocess.run(
        ["wsl.exe", "-d", distro, "--", "bash", "-lc", bootstrap],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _comfy_process_script(root: str) -> str:
    quoted_root = shlex.quote(root)
    return (
        f"root={quoted_root}; "
        "for proc in /proc/[0-9]*; do "
        "pid=${proc##*/}; "
        "cwd=$(readlink \"$proc/cwd\" 2>/dev/null || true); "
        "cmd=$(tr '\\0' ' ' < \"$proc/cmdline\" 2>/dev/null || true); "
        "if [ \"$cwd\" = \"$root\" ] && [[ \"$cmd\" == *main.py* ]]; then "
        "printf '%s\\n' \"$pid\"; fi; done"
    )


def _spawn_wsl_comfy(server_url: str, host_log_path: Path) -> subprocess.Popen:
    distro, root, python, _ = _wsl_lifecycle_config()
    parsed = urllib.parse.urlparse(server_url)
    port = parsed.port or 8188
    host_log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = hidden_process_creationflags(new_process_group=True)
    with host_log_path.open("ab", buffering=0) as log_file:
        return subprocess.Popen(
            [
                "wsl.exe", "-d", distro, "--cd", root, "--",
                python, "main.py", "--listen", "0.0.0.0", "--port", str(port), "--lowvram",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
        )


def _comfyui_lifecycle_mode() -> str:
    mode = os.getenv("COMFYUI_LIFECYCLE", "auto").strip().lower()
    if mode not in {"auto", "wsl", "native", "external"}:
        raise ValueError("COMFYUI_LIFECYCLE must be auto, wsl, native, or external")
    is_windows = sys.platform == "win32"
    native_supported = sys.platform == "darwin" or sys.platform.startswith("linux")
    if mode == "wsl" and not is_windows:
        raise ValueError("COMFYUI_LIFECYCLE=wsl is not supported off Windows")
    if mode == "native" and not native_supported:
        raise ValueError("COMFYUI_LIFECYCLE=native is not supported on this host platform")
    if mode != "auto":
        return mode
    if is_windows:
        return "wsl"
    if not native_supported:
        return "external"
    native_root = os.getenv("COMFYUI_NATIVE_ROOT", "").strip()
    native_python = os.getenv("COMFYUI_NATIVE_PYTHON", "").strip()
    return "native" if native_root and native_python else "external"


def _native_lifecycle_config(workdir: str) -> tuple[Path, Path, Path, Path]:
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise ValueError("COMFYUI_LIFECYCLE=native is supported only on macOS and Linux")
    raw_root = os.getenv("COMFYUI_NATIVE_ROOT", "").strip()
    raw_python = os.getenv("COMFYUI_NATIVE_PYTHON", "").strip()
    if not raw_root or not raw_python:
        raise ValueError("COMFYUI_NATIVE_ROOT and COMFYUI_NATIVE_PYTHON are required for native lifecycle")

    root = Path(os.path.expanduser(raw_root)).resolve()
    python = Path(os.path.expanduser(raw_python))
    if not python.is_absolute():
        python = root / python
    python = python.resolve()
    main_path = (root / "main.py").resolve()
    raw_log = os.getenv("COMFYUI_NATIVE_LOG", "").strip()
    log_path = (
        Path(os.path.expanduser(raw_log)).resolve()
        if raw_log
        else Path(workdir).resolve() / ".astra" / "comfyui-native.log"
    )

    if not root.is_dir():
        raise ValueError(f"COMFYUI_NATIVE_ROOT is not a directory: {root}")
    if not main_path.is_file():
        raise ValueError(f"Configured ComfyUI main.py was not found: {main_path}")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"COMFYUI_NATIVE_PYTHON is not executable: {python}")
    return root, python, main_path, log_path


def _native_pid_path(workdir: str) -> Path:
    return Path(workdir).resolve() / ".astra" / "comfyui-native.json"


def _native_launch_argv(server_url: str, python: Path, main_path: Path) -> list[str]:
    parsed = urllib.parse.urlparse(server_url)
    port = parsed.port or 8188
    return [
        str(python), str(main_path), "--listen", "0.0.0.0",
        "--port", str(port), "--lowvram",
    ]


@dataclass(frozen=True)
class _NativeProcessIdentity:
    pid: int
    executable: str
    argv: tuple[str, ...]
    cwd: str
    start_token: str
    pgid: int


def _linux_process_start_token(pid: int, *, proc_root: Path = Path("/proc")) -> str:
    try:
        boot_id = (proc_root / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        process_stat = (proc_root / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return ""
    fields = process_stat[process_stat.rfind(")") + 2:].split()
    if not boot_id or len(fields) <= 19:
        return ""
    return f"linux:{boot_id}:{fields[19]}"


def _linux_process_identity(pid: int) -> _NativeProcessIdentity | None:
    process_dir = Path("/proc") / str(pid)
    try:
        executable = os.path.realpath(os.readlink(process_dir / "exe"))
        cwd = os.path.realpath(os.readlink(process_dir / "cwd"))
        raw = (process_dir / "cmdline").read_bytes()
        argv = tuple(os.fsdecode(part) for part in raw.split(b"\0") if part)
        start_token = _linux_process_start_token(pid)
        pgid = _posix_getpgid(pid)
    except OSError:
        return None
    if not executable or not argv or not cwd or not start_token:
        return None
    return _NativeProcessIdentity(pid, executable, argv, cwd, start_token, pgid)


def _native_process_start_token(pid: int) -> str:
    if sys.platform == "darwin":
        return _darwin_process_start_token(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_start_token(pid)
    return ""


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _DarwinVinfoStat(ctypes.Structure):
    _fields_ = [
        ("vst_dev", ctypes.c_uint32),
        ("vst_mode", ctypes.c_uint16),
        ("vst_nlink", ctypes.c_uint16),
        ("vst_ino", ctypes.c_uint64),
        ("vst_uid", ctypes.c_uint32),
        ("vst_gid", ctypes.c_uint32),
        ("vst_atime", ctypes.c_int64),
        ("vst_atimensec", ctypes.c_int64),
        ("vst_mtime", ctypes.c_int64),
        ("vst_mtimensec", ctypes.c_int64),
        ("vst_ctime", ctypes.c_int64),
        ("vst_ctimensec", ctypes.c_int64),
        ("vst_birthtime", ctypes.c_int64),
        ("vst_birthtimensec", ctypes.c_int64),
        ("vst_size", ctypes.c_int64),
        ("vst_blocks", ctypes.c_int64),
        ("vst_blksize", ctypes.c_int32),
        ("vst_flags", ctypes.c_uint32),
        ("vst_gen", ctypes.c_uint32),
        ("vst_rdev", ctypes.c_uint32),
        ("vst_qspare", ctypes.c_int64 * 2),
    ]


class _DarwinFsid(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]


class _DarwinVnodeInfo(ctypes.Structure):
    _fields_ = [
        ("vi_stat", _DarwinVinfoStat),
        ("vi_type", ctypes.c_int),
        ("vi_pad", ctypes.c_int),
        ("vi_fsid", _DarwinFsid),
    ]


class _DarwinVnodeInfoPath(ctypes.Structure):
    _fields_ = [("vip_vi", _DarwinVnodeInfo), ("vip_path", ctypes.c_char * 1024)]


class _DarwinProcVnodePathInfo(ctypes.Structure):
    _fields_ = [("pvi_cdir", _DarwinVnodeInfoPath), ("pvi_rdir", _DarwinVnodeInfoPath)]


def _darwin_libproc():
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    proc_pidpath = libproc.proc_pidpath
    proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc_pidpath.restype = ctypes.c_int
    return proc_pidinfo, proc_pidpath


def _darwin_process_start_token(pid: int) -> str:
    try:
        proc_pidinfo, _ = _darwin_libproc()
        info = _DarwinProcBsdInfo()
        size = ctypes.sizeof(info)
        result = proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
    except (AttributeError, OSError):
        return ""
    if result != size or info.pbi_pid != pid:
        return ""
    return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"


def _darwin_process_argv(pid: int) -> tuple[str, ...]:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        sysctl = libc.sysctl
        sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
        ]
        sysctl.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value <= 4:
            return ()
        buffer = ctypes.create_string_buffer(size.value)
        if sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return ()
    except (AttributeError, OSError, ValueError):
        return ()

    raw = buffer.raw[:size.value]
    argc = int.from_bytes(raw[:4], byteorder=sys.byteorder, signed=True)
    if argc <= 0:
        return ()
    offset = raw.find(b"\0", 4)
    if offset < 0:
        return ()
    offset += 1
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    argv: list[str] = []
    while offset < len(raw) and len(argv) < argc:
        end = raw.find(b"\0", offset)
        if end < 0:
            return ()
        argv.append(os.fsdecode(raw[offset:end]))
        offset = end + 1
    return tuple(argv) if len(argv) == argc else ()


def _darwin_process_identity(pid: int) -> _NativeProcessIdentity | None:
    try:
        proc_pidinfo, proc_pidpath = _darwin_libproc()
        executable_buffer = ctypes.create_string_buffer(4096)
        executable_size = proc_pidpath(pid, executable_buffer, len(executable_buffer))
        cwd_info = _DarwinProcVnodePathInfo()
        cwd_size = ctypes.sizeof(cwd_info)
        cwd_result = proc_pidinfo(pid, 9, 0, ctypes.byref(cwd_info), cwd_size)
        pgid = _posix_getpgid(pid)
    except (AttributeError, OSError, ValueError):
        return None
    argv = _darwin_process_argv(pid)
    start_token = _darwin_process_start_token(pid)
    if executable_size <= 0 or cwd_result != cwd_size or not argv or not start_token:
        return None
    executable = os.fsdecode(executable_buffer.value)
    cwd = os.fsdecode(cwd_info.pvi_cdir.vip_path)
    if not executable or not cwd:
        return None
    return _NativeProcessIdentity(pid, executable, argv, cwd, start_token, pgid)


def _native_process_identity(pid: int) -> _NativeProcessIdentity | None:
    if sys.platform == "darwin":
        return _darwin_process_identity(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_identity(pid)
    return None


def _open_native_state_dir(path: Path, *, create: bool) -> int:
    created = False
    if create:
        try:
            os.mkdir(path, 0o700)
            created = True
        except FileExistsError:
            pass
    if created:
        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(path.parent, parent_flags)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    path_stat = os.lstat(path)
    if stat.S_ISLNK(path_stat.st_mode):
        raise OSError(f"Native lifecycle state directory is a symbolic link: {path}")
    if not stat.S_ISDIR(path_stat.st_mode):
        raise OSError(f"Native lifecycle state path is not a directory: {path}")
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and path_stat.st_uid != geteuid():
        raise OSError(f"Native lifecycle state directory has an unexpected owner: {path}")
    if stat.S_IMODE(path_stat.st_mode) & 0o022:
        raise OSError(f"Native lifecycle state directory has unsafe permissions: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    opened_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(opened_stat.st_mode):
        os.close(descriptor)
        raise OSError(f"Native lifecycle state path is not a directory: {path}")
    if geteuid is not None and opened_stat.st_uid != geteuid():
        os.close(descriptor)
        raise OSError(f"Native lifecycle state directory has an unexpected owner: {path}")
    if stat.S_IMODE(opened_stat.st_mode) & 0o022:
        os.close(descriptor)
        raise OSError(f"Native lifecycle state directory has unsafe permissions: {path}")
    if (opened_stat.st_dev, opened_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        os.close(descriptor)
        raise OSError(f"Native lifecycle state directory changed while opening: {path}")
    return descriptor


def _validate_native_state_file_stat(entry_stat, label: str) -> None:
    if stat.S_ISLNK(entry_stat.st_mode):
        raise OSError(f"Native lifecycle {label} is a symbolic link")
    if not stat.S_ISREG(entry_stat.st_mode):
        raise OSError(f"Native lifecycle {label} is not a regular file")
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and entry_stat.st_uid != geteuid():
        raise OSError(f"Native lifecycle {label} has an unexpected owner")
    if stat.S_IMODE(entry_stat.st_mode) & 0o022:
        raise OSError(f"Native lifecycle {label} has unsafe permissions")


def _safe_state_entry_stat(directory_fd: int, name: str, label: str):
    try:
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _validate_native_state_file_stat(entry_stat, label)
    return entry_stat


@contextmanager
def _native_lifecycle_lock(metadata_path: Path, *, timeout: float = 5.0):
    if fcntl is None:
        raise OSError("Native lifecycle locking is unavailable on this platform")
    directory_fd = _open_native_state_dir(metadata_path.parent, create=True)
    lock_fd = -1
    lock_acquired = False
    try:
        lock_name = "comfyui-native.lock"
        _safe_state_entry_stat(directory_fd, lock_name, "lock path")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_name, flags, 0o600, dir_fd=directory_fd)
        opened_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise OSError("Native lifecycle lock path is not a regular file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_acquired = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise _NativeLifecycleBusyError(
                        "Native ComfyUI lifecycle is busy; retry after the active operation finishes",
                    ) from exc
                time.sleep(0.05)
        current_stat = _safe_state_entry_stat(directory_fd, lock_name, "lock path")
        if current_stat is None or (current_stat.st_dev, current_stat.st_ino) != (
            opened_stat.st_dev, opened_stat.st_ino,
        ):
            raise OSError("Native lifecycle lock path changed while acquiring the lock")
        _safe_state_entry_stat(directory_fd, metadata_path.name, "metadata path")
        yield
    finally:
        if lock_fd >= 0 and lock_acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
                lock_fd = -1
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def _read_native_pid_metadata(path: Path) -> dict[str, Any] | None:
    try:
        directory_fd = _open_native_state_dir(path.parent, create=False)
    except FileNotFoundError:
        return None
    try:
        entry_stat = _safe_state_entry_stat(directory_fd, path.name, "metadata path")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            metadata_fd = os.open(path.name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        try:
            opened_stat = os.fstat(metadata_fd)
            _validate_native_state_file_stat(opened_stat, "metadata path")
            if entry_stat is None or (opened_stat.st_dev, opened_stat.st_ino) != (
                entry_stat.st_dev, entry_stat.st_ino,
            ):
                raise OSError("Native lifecycle metadata path changed while opening")
            chunks: list[bytes] = []
            while chunk := os.read(metadata_fd, 65536):
                chunks.append(chunk)
        finally:
            os.close(metadata_fd)
    finally:
        os.close(directory_fd)
    try:
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _native_pid_metadata_exists(path: Path) -> bool:
    try:
        directory_fd = _open_native_state_dir(path.parent, create=False)
    except FileNotFoundError:
        return False
    try:
        return _safe_state_entry_stat(directory_fd, path.name, "metadata path") is not None
    finally:
        os.close(directory_fd)


def _write_native_pid_metadata(path: Path, metadata: dict[str, Any]) -> None:
    directory_fd = _open_native_state_dir(path.parent, create=True)
    temporary_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temporary_fd = -1
    created_stat = None
    try:
        _safe_state_entry_stat(directory_fd, path.name, "metadata path")
        existing_temp = _safe_state_entry_stat(
            directory_fd, temporary_name, "temporary metadata path",
        )
        if existing_temp is not None:
            raise OSError("Native lifecycle temporary metadata path already exists")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        created_stat = os.fstat(temporary_fd)
        payload = (json.dumps(metadata, indent=2) + "\n").encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(temporary_fd, payload[offset:])
        os.fsync(temporary_fd)
        current_temp = _safe_state_entry_stat(
            directory_fd, temporary_name, "temporary metadata path",
        )
        if current_temp is None or (current_temp.st_dev, current_temp.st_ino) != (
            created_stat.st_dev, created_stat.st_ino,
        ):
            raise OSError("Native lifecycle temporary metadata path changed before rename")
        os.replace(
            temporary_name, path.name,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        created_stat = None
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if created_stat is not None:
            try:
                current_temp = _safe_state_entry_stat(
                    directory_fd, temporary_name, "temporary metadata path",
                )
                if current_temp is not None and (current_temp.st_dev, current_temp.st_ino) == (
                    created_stat.st_dev, created_stat.st_ino,
                ):
                    os.unlink(temporary_name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _discard_native_pid_metadata(
    path: Path, *, expected: dict[str, Any] | None = None,
) -> bool:
    try:
        directory_fd = _open_native_state_dir(path.parent, create=False)
    except FileNotFoundError:
        return False
    try:
        entry_stat = _safe_state_entry_stat(directory_fd, path.name, "metadata path")
        if entry_stat is None:
            return False
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            metadata_fd = os.open(path.name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return False
        try:
            opened_stat = os.fstat(metadata_fd)
            _validate_native_state_file_stat(opened_stat, "metadata path")
            if (opened_stat.st_dev, opened_stat.st_ino) != (
                entry_stat.st_dev, entry_stat.st_ino,
            ):
                return False
            chunks: list[bytes] = []
            while chunk := os.read(metadata_fd, 65536):
                chunks.append(chunk)
        finally:
            os.close(metadata_fd)
        try:
            current = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(current, dict) or (expected is not None and current != expected):
            return False
        current_stat = _safe_state_entry_stat(directory_fd, path.name, "metadata path")
        if current_stat is None or (current_stat.st_dev, current_stat.st_ino) != (
            opened_stat.st_dev, opened_stat.st_ino,
        ):
            return False
        os.unlink(path.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return True
    finally:
        os.close(directory_fd)


def _validate_native_metadata_schema(metadata: dict[str, Any]) -> tuple[bool, str]:
    required_keys = {
        "schema_version", "pid", "root", "python", "main_path", "argv", "start_token",
    }
    state = metadata.get("state")
    allowed_keys = required_keys | ({"state"} if state == "quarantine" else set())
    if set(metadata) != allowed_keys:
        return False, "PID metadata fields do not match the supported schema"
    version = metadata.get("schema_version")
    if type(version) is not int or version != NATIVE_METADATA_SCHEMA_VERSION:
        return False, "PID metadata schema version is unsupported"
    pid = metadata.get("pid")
    if type(pid) is not int:
        return False, "PID metadata does not contain a valid PID"
    if pid <= 0:
        return False, "PID metadata does not contain a positive PID"
    recorded_root = metadata.get("root")
    recorded_python = metadata.get("python")
    recorded_main = metadata.get("main_path")
    recorded_argv = metadata.get("argv")
    if not isinstance(recorded_root, str) or not recorded_root:
        return False, "PID metadata does not contain a valid ComfyUI root"
    if not isinstance(recorded_python, str) or not recorded_python:
        return False, "PID metadata does not contain a valid Python executable"
    if not isinstance(recorded_main, str) or not recorded_main:
        return False, "PID metadata does not contain a valid ComfyUI main.py"
    root_path = Path(recorded_root)
    python_path = Path(recorded_python)
    if not root_path.is_absolute() or str(root_path.resolve()) != recorded_root:
        return False, "PID metadata ComfyUI root is not a canonical absolute path"
    if not python_path.is_absolute() or str(python_path.resolve()) != recorded_python:
        return False, "PID metadata Python executable is not a canonical absolute path"
    expected_main = str((root_path / "main.py").resolve())
    if recorded_main != expected_main:
        return False, "PID metadata main.py is not the recorded root/main.py"
    if (
        not isinstance(recorded_argv, list)
        or len(recorded_argv) != 7
        or any(not isinstance(value, str) for value in recorded_argv)
    ):
        return False, "PID metadata does not contain the exact native ComfyUI command shape"
    port_text = recorded_argv[5]
    if not re.fullmatch(r"[0-9]+", port_text):
        return False, "PID metadata contains a non-decimal ComfyUI port"
    port = int(port_text)
    if not 1 <= port <= 65535 or str(port) != port_text:
        return False, "PID metadata contains an invalid ComfyUI port"
    expected_argv = [
        recorded_python, recorded_main, "--listen", "0.0.0.0",
        "--port", port_text, "--lowvram",
    ]
    if recorded_argv != expected_argv:
        return False, "PID metadata does not contain the exact native ComfyUI command"
    start_token = metadata.get("start_token")
    if not isinstance(start_token, str) or (state != "quarantine" and not start_token):
        return False, "PID metadata does not contain a valid process start identity"
    return True, ""


def _native_metadata_matches_process(metadata: dict[str, Any]) -> tuple[bool, str]:
    valid, reason = _validate_native_metadata_schema(metadata)
    if not valid:
        return False, reason
    pid = int(metadata["pid"])
    recorded_root = metadata["root"]
    recorded_python = metadata["python"]
    recorded_argv = metadata["argv"]
    identity = _native_process_identity(pid)
    if identity is None:
        return False, "exact process identity is unavailable"
    if identity.executable != recorded_python:
        return False, "process executable does not match the recorded Python executable"
    if list(identity.argv) != recorded_argv:
        return False, "process argv does not exactly match the recorded ComfyUI command"
    if identity.cwd != recorded_root:
        return False, "process working directory does not match the recorded ComfyUI root"
    if identity.pgid != pid:
        return False, "process is not the leader of its recorded process group"
    expected_token = metadata.get("start_token")
    if not expected_token or identity.start_token != expected_token:
        return False, "process start identity does not match the Astra-owned process"
    return True, ""


def _native_metadata_matches_config(
    metadata: dict[str, Any], root: Path, python: Path, expected_argv: list[str],
) -> bool:
    return (
        metadata.get("root") == str(root)
        and metadata.get("python") == str(python)
        and metadata.get("main_path") == expected_argv[1]
        and metadata.get("argv") == expected_argv
    )


def _spawn_native_comfy(
    server_url: str, root: Path, python: Path, main_path: Path, log_path: Path,
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log_file:
        return subprocess.Popen(
            _native_launch_argv(server_url, python, main_path),
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def _native_group_exists(pgid: int) -> bool:
    try:
        _posix_killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _linux_process_group_members(pgid: int) -> frozenset[tuple[int, str]] | None:
    members: set[tuple[int, str]] = set()
    unreadable_member = False
    try:
        process_directories = list(Path("/proc").iterdir())
    except OSError:
        return None
    for process_directory in process_directories:
        if not process_directory.name.isdigit():
            continue
        pid = int(process_directory.name)
        try:
            if _posix_getpgid(pid) != pgid:
                continue
        except OSError:
            continue
        start_token = _linux_process_start_token(pid)
        if start_token:
            members.add((pid, start_token))
        else:
            unreadable_member = True
    return None if unreadable_member else frozenset(members)


def _darwin_process_group_members(pgid: int) -> frozenset[tuple[int, str]] | None:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_listpgrppids = libproc.proc_listpgrppids
        proc_listpgrppids.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
        ]
        proc_listpgrppids.restype = ctypes.c_int
        required_size = proc_listpgrppids(pgid, None, 0)
        if required_size < 0:
            return None
        capacity = max(required_size + 32, 1024)
        pid_buffer = (ctypes.c_int * capacity)()
        result_count = proc_listpgrppids(
            pgid, ctypes.byref(pid_buffer), ctypes.sizeof(pid_buffer),
        )
    except (AttributeError, OSError, ValueError):
        return None
    if result_count < 0:
        return None
    members: set[tuple[int, str]] = set()
    unreadable_member = False
    for pid in pid_buffer[:result_count]:
        if pid <= 0:
            continue
        start_token = _darwin_process_start_token(pid)
        if start_token:
            members.add((pid, start_token))
        else:
            unreadable_member = True
    return None if unreadable_member else frozenset(members)


def _native_process_group_members(pgid: int) -> frozenset[tuple[int, str]] | None:
    if sys.platform == "darwin":
        return _darwin_process_group_members(pgid)
    if sys.platform.startswith("linux"):
        return _linux_process_group_members(pgid)
    return None


def _native_process_state(pid: int) -> str:
    if sys.platform.startswith("linux"):
        try:
            process_stat = (Path("/proc") / str(pid) / "stat").read_text(
                encoding="utf-8", errors="replace",
            )
        except OSError:
            return ""
        fields = process_stat[process_stat.rfind(")") + 2:].split()
        return fields[0] if fields else ""
    if sys.platform == "darwin":
        try:
            proc_pidinfo, _ = _darwin_libproc()
            info = _DarwinProcBsdInfo()
            size = ctypes.sizeof(info)
            result = proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
        except (AttributeError, OSError, ValueError):
            return ""
        if result != size or info.pbi_pid != pid:
            return ""
        # Darwin's sys/proc.h defines SZOMB as 5.
        return "Z" if info.pbi_status == 5 else "R"
    return ""


def _native_group_has_only_exact_zombies(
    members: frozenset[tuple[int, str]],
) -> bool:
    return bool(members) and all(_native_process_state(pid) == "Z" for pid, _ in members)


def _wait_for_native_group_exit(
    pgid: int, expected_members: frozenset[tuple[int, str]],
    *, process: subprocess.Popen | None = None,
) -> tuple[str, bool]:
    if process is not None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        except (AttributeError, ChildProcessError, ProcessLookupError):
            pass

    for _ in range(20):
        try:
            if not _native_group_exists(pgid):
                return "", False
        except OSError as exc:
            return f"could not verify process-group exit: {exc}", True
        members = _native_process_group_members(pgid)
        if members is None:
            return "cleanup could not read the remaining process-group membership", True
        if members and expected_members.isdisjoint(members):
            return "cleanup could not prove continuous process-group identity", True
        if members and _native_group_has_only_exact_zombies(members):
            return "", False
        # An empty enumeration while killpg(0) still succeeds is ambiguous, not absence.
        if not members:
            return "cleanup could not prove exit from an empty process-group snapshot", True
        time.sleep(0.05)
    return "", True


def _terminate_continuous_native_group(
    pgid: int, members_before_term: frozenset[tuple[int, str]],
    *, process: subprocess.Popen | None = None,
) -> tuple[str, bool]:
    try:
        _posix_killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "", False
    except OSError as exc:
        return f"cleanup failed: {exc}", True
    time.sleep(3)
    members_after_term = _native_process_group_members(pgid)
    if members_after_term is None:
        return "cleanup refused because process-group membership could not be rechecked", True
    if not members_after_term:
        try:
            if not _native_group_exists(pgid):
                return "", False
        except OSError as exc:
            return f"cleanup could not verify process-group exit: {exc}", True
        return "cleanup refused because process-group membership is unreadable", True
    if members_before_term.isdisjoint(members_after_term):
        return "cleanup refused because process-group identity changed after SIGTERM", True
    try:
        sigkill = getattr(signal, "SIGKILL", None)
        if sigkill is None:
            return "cleanup failed: native SIGKILL is unavailable on this platform", True
        _posix_killpg(pgid, sigkill)
    except ProcessLookupError:
        return "", False
    except OSError as exc:
        return f"cleanup failed: {exc}", True
    return _wait_for_native_group_exit(
        pgid, members_after_term, process=process,
    )


def _terminate_verified_native_group(
    metadata: dict[str, Any],
    *, ownership_verified: bool = False, process: subprocess.Popen | None = None,
) -> tuple[str, bool]:
    if not ownership_verified:
        owned, reason = _native_metadata_matches_process(metadata)
        if not owned:
            return f"cleanup refused because {reason}", True
    pid = int(metadata["pid"])
    members_before_term = _native_process_group_members(pid)
    leader_identity = (pid, str(metadata.get("start_token", "")))
    if members_before_term is None or leader_identity not in members_before_term:
        return "cleanup refused because process-group membership could not be anchored", True
    return _terminate_continuous_native_group(
        pid, members_before_term, process=process,
    )


def _spawned_native_group_anchor(
    process: subprocess.Popen,
) -> tuple[str, frozenset[tuple[int, str]]]:
    try:
        if process.poll() is not None:
            return "", frozenset()
        pgid = _posix_getpgid(process.pid)
    except (ChildProcessError, ProcessLookupError):
        return "", frozenset()
    except OSError as exc:
        raise OSError(f"the spawned child could not be checked: {exc}") from exc
    if pgid != process.pid:
        raise OSError("the spawned child is not its process-group leader")
    members_before_term = _native_process_group_members(pgid)
    if members_before_term is None:
        raise OSError("the spawned child could not snapshot its process group")
    leader_tokens = [
        start_token
        for member_pid, start_token in members_before_term
        if member_pid == process.pid
    ]
    if len(leader_tokens) != 1:
        raise OSError("the spawned child could not anchor its process group")
    return leader_tokens[0], members_before_term


def _validated_comfy_url(url: str, setting: str) -> str:
    url = url.rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{setting} must be an http(s) URL")
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{setting} must point to localhost/127.0.0.1")
    return url


def _local_comfy_url() -> str:
    return _validated_comfy_url(
        os.getenv("COMFYUI_SERVER", "http://127.0.0.1:8188"),
        "COMFYUI_SERVER",
    )


def _anima_comfy_url() -> str:
    value = os.getenv("COMFYUI_ANIMA_SERVER", "").strip()
    if not value:
        return _local_comfy_url()
    return _validated_comfy_url(value, "COMFYUI_ANIMA_SERVER")


def _workflow_candidates() -> list[Path]:
    rel = Path("user/default/workflows/JANKU_workflow_api.json")
    candidates: list[Path] = []

    explicit = os.getenv("COMFYUI_WORKFLOW_PATH", "").strip()
    if explicit:
        candidates.append(Path(os.path.expanduser(explicit)))

    root = os.getenv("COMFYUI_ROOT", "").strip()
    if root:
        candidates.append(Path(os.path.expanduser(root)) / rel)

    candidates.append(Path.home() / "comfy" / "ComfyUI" / rel)
    wsl_root = os.getenv("COMFYUI_WSL_ROOT", "").strip().rstrip("/")
    distro = os.getenv("COMFYUI_WSL_DISTRO", "Ubuntu").strip()
    if wsl_root.startswith("/") and re.fullmatch(r"[A-Za-z0-9_.-]+", distro):
        windows_root = wsl_root.replace("/", "\\")
        for host in ("wsl.localhost", "wsl$"):
            candidates.append(Path(f"\\\\{host}\\{distro}{windows_root}") / rel)
    return candidates


def _resolve_workflow_path() -> Path:
    inaccessible: list[str] = []
    for path in _workflow_candidates():
        try:
            if path.exists():
                return path
        except OSError as exc:
            inaccessible.append(f"- {path} ({exc})")
    searched = "\n".join(f"- {path}" for path in _workflow_candidates())
    details = f"\nInaccessible:\n{chr(10).join(inaccessible)}" if inaccessible else ""
    raise FileNotFoundError(
        "ComfyUI workflow not found. Set COMFYUI_WORKFLOW_PATH to the API workflow JSON.\n"
        f"Searched:\n{searched}{details}"
    )


def _load_workflow(workflow_path: str = "") -> dict:
    path = Path(os.path.expanduser(workflow_path)).resolve() if workflow_path else _resolve_workflow_path()
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _set_input(workflow: dict, node_id: str, key: str, value) -> None:
    try:
        workflow[node_id]["inputs"][key] = value
    except KeyError as exc:
        raise KeyError(f"Workflow node {node_id!r} is missing input {key!r}") from exc


def _set_filename_prefix(workflow: dict, prefix: str) -> None:
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if isinstance(inputs, dict) and "filename_prefix" in inputs:
            inputs["filename_prefix"] = prefix


def _apply_workflow_settings(
    workflow: dict,
    *,
    prompt: str,
    negative_prompt: str,
    seed: int,
    cfg: float,
    steps: int,
    width: int,
    height: int,
    lora_strength: float,
    filename_prefix: str,
) -> dict:
    _set_input(workflow, "5", "text", prompt)
    _set_input(workflow, "6", "text", negative_prompt)
    _set_input(workflow, "8", "seed", seed)
    _set_input(workflow, "8", "cfg", cfg)
    _set_input(workflow, "8", "steps", steps)
    _set_input(workflow, "10", "width", width)
    _set_input(workflow, "10", "height", height)

    for node_id, strength in {"2": lora_strength, "11": 0.0, "4": 0.0, "3": 0.0}.items():
        node = workflow.get(node_id, {})
        inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
        if "strength_model" in inputs:
            inputs["strength_model"] = strength
        if "strength_clip" in inputs:
            inputs["strength_clip"] = strength

    _set_filename_prefix(workflow, filename_prefix)
    return workflow


def _join_tags(*parts: str) -> str:
    return ", ".join(part.strip(" ,") for part in parts if part and part.strip(" ,"))


def _build_prompts(
    prompt: str,
    negative_prompt: str = "",
    *,
    preset: str = "monochrome",
    hair_mode: str = "auto",
    include_base_tags: bool = True,
) -> tuple[str, str]:
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset}. Use one of {', '.join(PRESETS)}")
    if hair_mode not in {"auto", "up", "down"}:
        raise ValueError("hair_mode must be one of: auto, up, down")

    preset_data = PRESETS[preset]
    positive = _join_tags(
        BASE_TAGS if include_base_tags else "",
        preset_data["positive"],
        HAIR_TAGS.get(hair_mode, ""),
        prompt,
    )
    negative = _join_tags(NEGATIVE_TEMPLATE, preset_data["negative"], negative_prompt)
    return positive, negative


def _validate_generation_args(seed: int | None, width: int, height: int, lora_strength: float) -> int:
    if seed is None:
        seed = random.randint(1, MAX_SEED)
    if seed < 0 or seed > MAX_SEED:
        raise ValueError(f"seed must be between 0 and {MAX_SEED}")
    if width < 512 or height < 512 or width > 2048 or height > 2048:
        raise ValueError("width and height must be between 512 and 2048")
    if width % 8 != 0 or height % 8 != 0:
        raise ValueError("width and height must be multiples of 8")
    if lora_strength < 0 or lora_strength > 2:
        raise ValueError("lora_strength must be between 0 and 2")
    return seed


def _anima_dimensions(orientation: str, resolution: str) -> tuple[int, int]:
    dimensions = {
        ("portrait", "normal"): (1024, 1344),
        ("landscape", "normal"): (1344, 1024),
        ("square", "normal"): (1024, 1024),
        ("portrait", "high"): (1152, 1536),
        ("landscape", "high"): (1536, 1152),
        ("square", "high"): (1344, 1344),
    }
    try:
        return dimensions[(orientation, resolution)]
    except KeyError as exc:
        raise ValueError("Invalid Anima orientation/resolution") from exc


def _build_anima_prompts(
    prompt: str,
    *,
    profile: str = "signature-muted",
    hair_mode: str = "soft",
    rating: str = "sfw",
    pure_bw: bool = False,
    negative_prompt: str = "",
    extra_tags: str = "",
) -> tuple[str, str, float]:
    if profile not in ANIMA_PROFILES:
        raise ValueError(f"Unknown Anima profile: {profile}")
    if hair_mode not in ANIMA_HAIR:
        raise ValueError("hair_mode must be soft or hairup")
    if rating not in {"sfw", "adult"}:
        raise ValueError("rating must be sfw or adult")
    scene = " ".join(prompt.replace("\n", " ").split()).strip(" ,")
    if not scene:
        raise ValueError("prompt must describe concrete clothing, pose, expression, or scene details")

    profile_data = ANIMA_PROFILES[profile]
    style = ANIMA_PURE_BW_STYLE if pure_bw else str(profile_data["style"])
    identity = ANIMA_IDENTITY
    if pure_bw:
        identity = identity.replace("red eyes, crimson eyes", "black eyes, dark eyes")
    if any(marker in extra_tags.lower() for marker in ("1boy", "2girl", "2boys", "multiple")):
        identity = identity.replace("1girl, solo", "1girl")
    positive = _join_tags(ANIMA_QUALITY, style, identity, ANIMA_HAIR[hair_mode], scene, extra_tags)
    negative = ANIMA_NEGATIVE
    if rating == "sfw":
        negative = _join_tags(negative, ANIMA_SFW_BLOCKERS)
    if profile == "pure-bw-red" and not pure_bw:
        negative = _join_tags(
            negative,
            "spot color on anything except eyes, any color except red eyes, tint, sepia, hue",
        )
    if pure_bw:
        negative = _join_tags(
            negative,
            "red eyes, crimson eyes, colored eyes, spot color, any color, tint, sepia, hue",
        )
    return positive, _join_tags(negative, negative_prompt), float(profile_data["saturation"])


def _build_anima_workflow(
    *,
    prompt: str,
    negative_prompt: str,
    seed: int,
    width: int,
    height: int,
    saturation: float,
    model_version: str,
    filename_prefix: str,
) -> dict[str, Any]:
    if model_version not in ANIMA_MODELS:
        raise ValueError("model_version must be v1, v2 or v3")
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": ANIMA_MODELS[model_version], "weight_dtype": "default",
        }},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": ANIMA_CLIP, "type": "qwen_image",
        }},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": ANIMA_VAE}},
        "16": {"class_type": "LoraLoader", "inputs": {
            "model": ["1", 0], "clip": ["2", 0], "lora_name": ANIMA_TURBO_LORA,
            "strength_model": 1.0, "strength_clip": 0.0,
        }},
        "17": {"class_type": "LoraLoader", "inputs": {
            "model": ["16", 0], "clip": ["16", 1], "lora_name": ANIMA_HIGHRES_LORA,
            "strength_model": 0.6, "strength_clip": 0.0,
        }},
        "18": {"class_type": "LoraLoader", "inputs": {
            "model": ["17", 0], "clip": ["17", 1], "lora_name": ANIMA_SATURATION_LORA,
            "strength_model": saturation, "strength_clip": 0.0,
        }},
        "19": {"class_type": "LoraLoader", "inputs": {
            "model": ["18", 0], "clip": ["18", 1], "lora_name": ANIMA_LYRA_V2_LORA,
            "strength_model": 0.8, "strength_clip": 0.0,
        }},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["19", 1], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["19", 1], "text": negative_prompt}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1,
        }},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["19", 0], "positive": ["4", 0], "negative": ["5", 0],
            "latent_image": ["6", 0], "seed": seed, "control_after_generate": "fixed",
            "steps": 16, "cfg": 1.0, "sampler_name": "euler_ancestral",
            "scheduler": "normal", "denoise": 1.0,
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "images": ["8", 0], "filename_prefix": filename_prefix,
        }},
    }


def _request_json(url: str, payload: dict | None = None, timeout: int = 30) -> Any:
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_image(server_url: str, image: dict, output_dir: Path, prompt_id: str) -> Path:
    filename = image["filename"]
    subfolder = image.get("subfolder", "")
    image_type = image.get("type", "output")
    query = urllib.parse.urlencode({
        "filename": filename,
        "subfolder": subfolder,
        "type": image_type,
    })
    safe_subfolder = subfolder.replace("/", "_").replace("\\", "_")
    prefix = f"{prompt_id[:8]}_"
    local_name = prefix + (f"{safe_subfolder}_" if safe_subfolder else "") + filename
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / local_name
    urllib.request.urlretrieve(f"{server_url}/view?{query}", out_path)
    return out_path


def register_comfyui_tools(registry: ToolRegistry, workdir: str = "."):
    default_output_dir = Path(workdir).resolve() / ".generated" / "comfyui"
    # The registry deadline must outlive both readiness polling and the bounded
    # identity/TERM/KILL/reaping cleanup path (plus the initial locked probes).
    comfyui_start_wait_seconds = _positive_int_env("COMFYUI_START_TIMEOUT", 45)
    comfyui_start_tool_timeout = comfyui_start_wait_seconds + 35

    def _comfyui_status() -> str:
        server_url = _local_comfy_url()
        status = {
            "server": server_url,
            "lifecycle": "",
            "server_ok": False,
            "workflow_ok": False,
            "workflow_path": "",
            "device": "",
            "error": "",
        }
        try:
            status["lifecycle"] = _comfyui_lifecycle_mode()
        except ValueError as exc:
            status["error"] = str(exc)
        try:
            workflow_path = _resolve_workflow_path()
            status["workflow_ok"] = True
            status["workflow_path"] = str(workflow_path)
        except Exception as exc:
            status["error"] = (status["error"] + "\n" if status["error"] else "") + str(exc)

        try:
            stats = _request_json(f"{server_url}/system_stats", timeout=5)
            status["server_ok"] = True
            devices = stats.get("devices") or []
            if devices:
                device = devices[0]
                status["device"] = " ".join(
                    str(device.get(key, "")) for key in ("name", "type", "vram_total") if device.get(key) is not None
                ).strip()
        except Exception as exc:
            status["error"] = (status["error"] + "\n" if status["error"] else "") + str(exc)
        return json.dumps(status, ensure_ascii=False, indent=2)

    def _server_online(server_url: str) -> tuple[bool, str]:
        try:
            stats = _request_json(f"{server_url}/system_stats", timeout=5)
            devices = stats.get("devices") or []
            device = str(devices[0].get("name", "")) if devices else ""
            return True, device
        except Exception as exc:
            return False, str(exc)

    def _comfyui_start() -> str:
        """Start a managed ComfyUI instance and verify its HTTP API."""
        server_url = _local_comfy_url()
        lifecycle = _comfyui_lifecycle_mode()
        online, device = _server_online(server_url)
        if online:
            return json.dumps({
                "success": True,
                "status": "already_running",
                "lifecycle": lifecycle,
                "server_ok": True,
                "server": server_url,
                "device": device,
                "message": "ComfyUI is already online; proceed with a draw tool.",
            }, ensure_ascii=False, indent=2)

        if lifecycle == "external":
            return json.dumps({
                "success": False,
                "status": "external_management_required",
                "lifecycle": lifecycle,
                "server_ok": False,
                "server": server_url,
                "error": "ComfyUI is configured as externally managed and its HTTP API is offline.",
                "fallback": "Start the external ComfyUI service, then retry after /system_stats is available.",
            }, ensure_ascii=False, indent=2)

        existing_process = False
        pid = ""
        if lifecycle == "wsl":
            distro, root, python, _ = _wsl_lifecycle_config()
            check = _run_wsl(
                f"test -d {shlex.quote(root)} && test -x {shlex.quote(root + '/' + python)}",
                timeout=10,
            )
            if check.returncode != 0:
                return json.dumps({
                    "success": False,
                    "status": "not_started",
                    "lifecycle": lifecycle,
                    "server_ok": False,
                    "server": server_url,
                    "distro": distro,
                    "root": root,
                    "error": (check.stderr or check.stdout or "Configured ComfyUI root/Python was not found").strip(),
                    "fallback": "Inspect the configured WSL root, Python path, and startup log with shell.",
                }, ensure_ascii=False, indent=2)

            process_check = _run_wsl(_comfy_process_script(root), timeout=10)
            existing_pids = [line for line in process_check.stdout.splitlines() if line.strip().isdigit()]
            existing_process = bool(existing_pids)
            pid = existing_pids[0] if existing_pids else ""
            host_log_path = Path(
                os.getenv("COMFYUI_HOST_LOG", str(Path(workdir).resolve() / ".logs" / "comfyui-server.log"))
            ).expanduser().resolve()
            if not existing_process:
                try:
                    started = _spawn_wsl_comfy(server_url, host_log_path)
                    pid = str(started.pid)
                except (OSError, ValueError) as exc:
                    return json.dumps({
                        "success": False,
                        "status": "not_started",
                        "lifecycle": lifecycle,
                        "server_ok": False,
                        "server": server_url,
                        "error": str(exc),
                        "fallback": "Inspect the startup log and use shell only for the uncovered failure.",
                    }, ensure_ascii=False, indent=2)
        else:
            try:
                root_path, python_path, main_path, host_log_path = _native_lifecycle_config(workdir)
            except (OSError, ValueError) as exc:
                return json.dumps({
                    "success": False,
                    "status": "not_started",
                    "lifecycle": lifecycle,
                    "server_ok": False,
                    "server": server_url,
                    "error": str(exc),
                    "fallback": "Fix COMFYUI_NATIVE_ROOT and COMFYUI_NATIVE_PYTHON, then retry.",
                }, ensure_ascii=False, indent=2)
            metadata_path = _native_pid_path(workdir)
            expected_argv = _native_launch_argv(server_url, python_path, main_path)
            try:
                with _native_lifecycle_lock(metadata_path):
                    locked_online, locked_device = _server_online(server_url)
                    if locked_online:
                        return json.dumps({
                            "success": True,
                            "status": "already_running",
                            "lifecycle": lifecycle,
                            "server_ok": True,
                            "server": server_url,
                            "device": locked_device,
                            "message": "ComfyUI is already online; proceed with a draw tool.",
                        }, ensure_ascii=False, indent=2)

                    metadata = _read_native_pid_metadata(metadata_path)
                    if metadata is None and _native_pid_metadata_exists(metadata_path):
                        return json.dumps({
                            "success": False,
                            "status": "not_started",
                            "lifecycle": lifecycle,
                            "server_ok": False,
                            "server": server_url,
                            "error": "Native lifecycle metadata is malformed; refusing to replace it.",
                            "fallback": "Inspect the native PID metadata before retrying.",
                        }, ensure_ascii=False, indent=2)
                    if metadata is not None:
                        metadata_valid, metadata_reason = _validate_native_metadata_schema(metadata)
                        if not metadata_valid:
                            return json.dumps({
                                "success": False,
                                "status": "invalid_pid_metadata",
                                "lifecycle": lifecycle,
                                "server_ok": False,
                                "server": server_url,
                                "pid": str(metadata.get("pid", "")),
                                "error": metadata_reason + "; refusing process access or replacement.",
                                "fallback": "Inspect the retained native ownership metadata.",
                            }, ensure_ascii=False, indent=2)
                    if metadata is not None:
                        if metadata.get("state") == "quarantine":
                            try:
                                quarantined_pid = int(metadata.get("pid", ""))
                                quarantine_present = (
                                    quarantined_pid > 0 and _native_group_exists(quarantined_pid)
                                )
                            except (OSError, TypeError, ValueError):
                                quarantine_present = True
                            if quarantine_present:
                                return json.dumps({
                                    "success": False,
                                    "status": "cleanup_incomplete",
                                    "lifecycle": lifecycle,
                                    "server_ok": False,
                                    "server": server_url,
                                    "pid": str(metadata.get("pid", "")),
                                    "error": (
                                        "A quarantined Astra-spawned native process group is still "
                                        "present; refusing to spawn a duplicate."
                                    ),
                                    "fallback": "Inspect or stop the quarantined process group before retrying.",
                                }, ensure_ascii=False, indent=2)
                            _discard_native_pid_metadata(metadata_path, expected=metadata)
                            metadata = None
                    if metadata is not None:
                        existing_process, ownership_reason = _native_metadata_matches_process(metadata)
                        if existing_process:
                            if not _native_metadata_matches_config(
                                metadata, root_path, python_path, expected_argv,
                            ):
                                return json.dumps({
                                    "success": False,
                                    "status": "owned_config_mismatch",
                                    "lifecycle": lifecycle,
                                    "server_ok": False,
                                    "server": server_url,
                                    "pid": str(metadata["pid"]),
                                    "error": (
                                        "A live Astra-owned native ComfyUI group uses the "
                                        "recorded previous configuration; retaining ownership "
                                        "metadata and refusing to spawn a duplicate."
                                    ),
                                    "fallback": "Stop the recorded owned group before starting the new configuration.",
                                }, ensure_ascii=False, indent=2)
                            pid = str(metadata["pid"])
                        else:
                            try:
                                recorded_pid = int(metadata.get("pid", ""))
                                recorded_group_present = (
                                    recorded_pid > 0 and _native_group_exists(recorded_pid)
                                )
                            except (OSError, TypeError, ValueError):
                                recorded_group_present = True
                            if (
                                ownership_reason == "exact process identity is unavailable"
                                and recorded_group_present
                            ):
                                return json.dumps({
                                    "success": False,
                                    "status": "cleanup_incomplete",
                                    "lifecycle": lifecycle,
                                    "server_ok": False,
                                    "server": server_url,
                                    "pid": str(metadata.get("pid", "")),
                                    "error": (
                                        "The recorded native group is still present but exact "
                                        "ownership is unreadable; retaining metadata and refusing "
                                        "to spawn a duplicate."
                                    ),
                                    "fallback": "Inspect the retained process group before retrying.",
                                }, ensure_ascii=False, indent=2)
                            _discard_native_pid_metadata(metadata_path, expected=metadata)

                    spawned_metadata = None
                    started = None
                    persisted_metadata = None
                    if not existing_process:
                        try:
                            started = _spawn_native_comfy(
                                server_url, root_path, python_path, main_path, host_log_path,
                            )
                            pid = str(started.pid)
                            spawned_metadata = {
                                "schema_version": NATIVE_METADATA_SCHEMA_VERSION,
                                "pid": started.pid,
                                "root": str(root_path),
                                "python": str(python_path),
                                "main_path": str(main_path),
                                "argv": expected_argv,
                                "start_token": "",
                                "state": "quarantine",
                            }
                            _write_native_pid_metadata(metadata_path, spawned_metadata)
                            persisted_metadata = spawned_metadata
                            identity = None
                            for _ in range(20):
                                candidate = _native_process_identity(started.pid)
                                if (
                                    candidate is not None
                                    and candidate.executable == str(python_path)
                                    and list(candidate.argv) == expected_argv
                                    and candidate.cwd == str(root_path)
                                    and candidate.pgid == started.pid
                                    and candidate.start_token
                                ):
                                    identity = candidate
                                    break
                                time.sleep(0.05)
                            if identity is None:
                                raise OSError(
                                    "Could not record the exact native ComfyUI process identity",
                                )
                            spawned_metadata = {
                                "schema_version": NATIVE_METADATA_SCHEMA_VERSION,
                                "pid": started.pid,
                                "root": str(root_path),
                                "python": str(python_path),
                                "main_path": str(main_path),
                                "argv": expected_argv,
                                "start_token": identity.start_token,
                            }
                            _write_native_pid_metadata(metadata_path, spawned_metadata)
                            persisted_metadata = spawned_metadata
                        except (OSError, ValueError) as exc:
                            cleanup_error = ""
                            group_remains = False
                            if started is not None:
                                if (
                                    spawned_metadata is not None
                                    and spawned_metadata.get("state") != "quarantine"
                                ):
                                    cleanup_error, group_remains = _terminate_verified_native_group(
                                        spawned_metadata, process=started,
                                    )
                                else:
                                    try:
                                        start_token, members_before_term = (
                                            _spawned_native_group_anchor(started)
                                        )
                                    except OSError as anchor_exc:
                                        cleanup_error = f"cleanup refused because {anchor_exc}"
                                        group_remains = True
                                    else:
                                        if members_before_term:
                                            spawned_metadata = {
                                                "schema_version": NATIVE_METADATA_SCHEMA_VERSION,
                                                "pid": started.pid,
                                                "root": str(root_path),
                                                "python": str(python_path),
                                                "main_path": str(main_path),
                                                "argv": expected_argv,
                                                "start_token": start_token,
                                                "state": "quarantine",
                                            }
                                            persistence_error = ""
                                            try:
                                                _write_native_pid_metadata(
                                                    metadata_path, spawned_metadata,
                                                )
                                                persisted_metadata = spawned_metadata
                                            except OSError as metadata_exc:
                                                persistence_error = (
                                                    f"could not persist quarantine metadata: {metadata_exc}"
                                                )
                                            cleanup_error, group_remains = (
                                                _terminate_continuous_native_group(
                                                    started.pid, members_before_term,
                                                    process=started,
                                                )
                                            )
                                            if persistence_error:
                                                cleanup_error = (
                                                    persistence_error
                                                    + (f"; {cleanup_error}" if cleanup_error else "")
                                                )
                                if not cleanup_error and not group_remains:
                                    _discard_native_pid_metadata(
                                        metadata_path, expected=persisted_metadata,
                                    )
                            return json.dumps({
                                "success": False,
                                "status": "not_started",
                                "lifecycle": lifecycle,
                                "server_ok": False,
                                "server": server_url,
                                "error": str(exc) + (f"; {cleanup_error}" if cleanup_error else ""),
                                "log_path": str(host_log_path),
                                "fallback": "Inspect the native startup log and configuration.",
                            }, ensure_ascii=False, indent=2)

                    wait_seconds = comfyui_start_wait_seconds
                    deadline = time.monotonic() + wait_seconds
                    last_error = ""
                    while time.monotonic() < deadline:
                        online, device_or_error = _server_online(server_url)
                        if online:
                            return json.dumps({
                                "success": True,
                                "status": "started" if not existing_process else "became_ready",
                                "lifecycle": lifecycle,
                                "server_ok": True,
                                "server": server_url,
                                "pid": pid,
                                "device": device_or_error,
                                "log_path": str(host_log_path),
                                "message": "ComfyUI is verified online; proceed with a draw tool.",
                            }, ensure_ascii=False, indent=2)
                        last_error = device_or_error
                        time.sleep(1)

                    cleanup_error = ""
                    cleanup_incomplete = False
                    if spawned_metadata is not None:
                        cleanup_error, cleanup_incomplete = _terminate_verified_native_group(
                            spawned_metadata, process=started,
                        )
                        if not cleanup_error and not cleanup_incomplete:
                            _discard_native_pid_metadata(
                                metadata_path, expected=spawned_metadata,
                            )
                    try:
                        log_lines = host_log_path.read_text(
                            encoding="utf-8", errors="replace",
                        ).splitlines()
                        log_tail = "\n".join(log_lines[-30:])
                    except OSError:
                        log_tail = ""
                    return json.dumps({
                        "success": False,
                        "status": "starting_or_failed",
                        "lifecycle": lifecycle,
                        "server_ok": False,
                        "server": server_url,
                        "pid": pid,
                        "log_path": str(host_log_path),
                        "error": (last_error or "ComfyUI did not become ready before the startup timeout")
                        + (f"; {cleanup_error}" if cleanup_error else "")
                        + ("; owned process group is still present" if cleanup_incomplete else ""),
                        "log_tail": log_tail,
                        "fallback": "Use shell to diagnose this specific startup failure; do not claim the server is ready.",
                    }, ensure_ascii=False, indent=2)
            except _NativeLifecycleBusyError as exc:
                return json.dumps({
                    "success": False,
                    "status": "lifecycle_busy",
                    "lifecycle": lifecycle,
                    "server_ok": False,
                    "server": server_url,
                    "error": str(exc),
                    "log_path": str(host_log_path),
                    "fallback": "Retry after the active native lifecycle operation finishes.",
                }, ensure_ascii=False, indent=2)
            except OSError as exc:
                return json.dumps({
                    "success": False,
                    "status": "not_started",
                    "lifecycle": lifecycle,
                    "server_ok": False,
                    "server": server_url,
                    "error": str(exc),
                    "log_path": str(host_log_path),
                    "fallback": "Inspect the native lifecycle state path and configuration.",
                }, ensure_ascii=False, indent=2)

        wait_seconds = comfyui_start_wait_seconds
        deadline = time.monotonic() + wait_seconds
        last_error = ""
        while time.monotonic() < deadline:
            online, device_or_error = _server_online(server_url)
            if online:
                return json.dumps({
                    "success": True,
                    "status": "started" if not existing_process else "became_ready",
                    "lifecycle": lifecycle,
                    "server_ok": True,
                    "server": server_url,
                    "pid": pid,
                    "device": device_or_error,
                    "log_path": str(host_log_path),
                    "message": "ComfyUI is verified online; proceed with a draw tool.",
                }, ensure_ascii=False, indent=2)
            last_error = device_or_error
            time.sleep(1)

        try:
            log_lines = host_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(log_lines[-30:])
        except OSError:
            log_tail = ""
        return json.dumps({
            "success": False,
            "status": "starting_or_failed",
            "lifecycle": lifecycle,
            "server_ok": False,
            "server": server_url,
            "pid": pid,
            "log_path": str(host_log_path),
            "error": last_error or "ComfyUI did not become ready before the startup timeout",
            "log_tail": log_tail,
            "fallback": "Use shell to diagnose this specific startup failure; do not claim the server is ready.",
        }, ensure_ascii=False, indent=2)

    def _comfyui_stop() -> str:
        """Stop only a ComfyUI process verified as owned by the selected lifecycle."""
        server_url = _local_comfy_url()
        lifecycle = _comfyui_lifecycle_mode()
        if lifecycle == "external":
            online, device_or_error = _server_online(server_url)
            return json.dumps({
                "success": not online,
                "status": "already_stopped" if not online else "external_management_required",
                "lifecycle": lifecycle,
                "server_ok": online,
                "server": server_url,
                "device": device_or_error if online else "",
                "error": "" if not online else "ComfyUI is externally managed; Astra will not signal its process.",
                "fallback": "Stop the service with its external process manager." if online else "",
            }, ensure_ascii=False, indent=2)

        if lifecycle == "wsl":
            _, root, _, _ = _wsl_lifecycle_config()
            found = _run_wsl(_comfy_process_script(root), timeout=10)
            pids = [line.strip() for line in found.stdout.splitlines() if line.strip().isdigit()]
            if not pids:
                online, device_or_error = _server_online(server_url)
                return json.dumps({
                    "success": not online,
                    "status": "already_stopped" if not online else "unmanaged_process",
                    "lifecycle": lifecycle,
                    "server_ok": online,
                    "server": server_url,
                    "device": device_or_error if online else "",
                    "error": "" if not online else "Server is online but no matching WSL ComfyUI process was found.",
                    "fallback": "Use shell to identify the unmanaged listener before stopping it." if online else "",
                }, ensure_ascii=False, indent=2)

            pid_args = " ".join(pids)
            stopped = _run_wsl(
                f"kill -TERM {pid_args}; sleep 3; "
                f"for pid in {pid_args}; do kill -0 $pid 2>/dev/null && kill -KILL $pid || true; done",
                timeout=15,
            )
            stderr = stopped.stderr.strip()
        else:
            metadata_path = _native_pid_path(workdir)
            try:
                with _native_lifecycle_lock(metadata_path):
                    metadata = _read_native_pid_metadata(metadata_path)
                    if metadata is None:
                        if _native_pid_metadata_exists(metadata_path):
                            return json.dumps({
                                "success": False,
                                "status": "not_stopped",
                                "lifecycle": lifecycle,
                                "server_ok": _server_online(server_url)[0],
                                "server": server_url,
                                "error": "Native lifecycle metadata is malformed; refusing to replace or signal it.",
                                "fallback": "Inspect the native PID metadata before retrying.",
                            }, ensure_ascii=False, indent=2)
                        online, device_or_error = _server_online(server_url)
                        return json.dumps({
                            "success": not online,
                            "status": "already_stopped" if not online else "unmanaged_process",
                            "lifecycle": lifecycle,
                            "server_ok": online,
                            "server": server_url,
                            "device": device_or_error if online else "",
                            "error": "" if not online else "Server is online but Astra has no native PID ownership metadata.",
                            "fallback": "Use the process owner to stop the unmanaged server." if online else "",
                        }, ensure_ascii=False, indent=2)

                    metadata_valid, metadata_reason = _validate_native_metadata_schema(metadata)
                    if not metadata_valid:
                        return json.dumps({
                            "success": False,
                            "status": "invalid_pid_metadata",
                            "lifecycle": lifecycle,
                            "server_ok": False,
                            "server": server_url,
                            "pid": str(metadata.get("pid", "")),
                            "error": metadata_reason + "; refusing process access or deletion.",
                            "fallback": "Inspect the retained native ownership metadata.",
                        }, ensure_ascii=False, indent=2)

                    if metadata.get("state") == "quarantine":
                        try:
                            quarantined_pid = int(metadata.get("pid", ""))
                            quarantine_present = (
                                quarantined_pid > 0 and _native_group_exists(quarantined_pid)
                            )
                        except (OSError, TypeError, ValueError):
                            quarantine_present = True
                        if quarantine_present:
                            online, device_or_error = _server_online(server_url)
                            return json.dumps({
                                "success": False,
                                "status": "stop_incomplete",
                                "lifecycle": lifecycle,
                                "server_ok": online,
                                "server": server_url,
                                "pid": str(metadata.get("pid", "")),
                                "device": device_or_error if online else "",
                                "error": (
                                    "The Astra-spawned group has quarantine metadata without an "
                                    "exact identity; retaining it and refusing to signal."
                                ),
                                "fallback": "Inspect the quarantined process group before manual action.",
                            }, ensure_ascii=False, indent=2)
                        _discard_native_pid_metadata(metadata_path, expected=metadata)
                        online, device_or_error = _server_online(server_url)
                        return json.dumps({
                            "success": not online,
                            "status": "already_stopped" if not online else "unmanaged_process",
                            "lifecycle": lifecycle,
                            "server_ok": online,
                            "server": server_url,
                            "device": device_or_error if online else "",
                            "error": "" if not online else "The quarantined group exited but the API remains online.",
                            "fallback": "Identify the unmanaged listener." if online else "",
                        }, ensure_ascii=False, indent=2)

                    owned, reason = _native_metadata_matches_process(metadata)
                    pid_value = metadata.get("pid", "")
                    if not owned:
                        retained_group_pid = None
                        if reason == "exact process identity is unavailable":
                            try:
                                candidate_pid = int(pid_value)
                                if candidate_pid > 0 and _native_group_exists(candidate_pid):
                                    retained_group_pid = candidate_pid
                            except (OSError, TypeError, ValueError):
                                retained_group_pid = None
                        if retained_group_pid is not None:
                            online, device_or_error = _server_online(server_url)
                            return json.dumps({
                                "success": False,
                                "status": "stop_incomplete",
                                "lifecycle": lifecycle,
                                "server_ok": online,
                                "server": server_url,
                                "pid": str(retained_group_pid),
                                "device": device_or_error if online else "",
                                "error": (
                                    "The recorded leader identity is unavailable, but its process "
                                    "group still exists; retaining metadata and refusing to signal."
                                ),
                                "fallback": "Inspect the retained process group before taking manual action.",
                            }, ensure_ascii=False, indent=2)
                        _discard_native_pid_metadata(metadata_path, expected=metadata)
                        online, device_or_error = _server_online(server_url)
                        return json.dumps({
                            "success": False,
                            "status": "stale_pid_metadata",
                            "lifecycle": lifecycle,
                            "server_ok": online,
                            "server": server_url,
                            "pid": str(pid_value),
                            "device": device_or_error if online else "",
                            "error": f"Refusing to signal PID {pid_value}: {reason}.",
                            "fallback": "Identify the current process owner before taking any manual action.",
                        }, ensure_ascii=False, indent=2)

                    pid = int(pid_value)
                    stop_error, group_remains = _terminate_verified_native_group(
                        metadata, ownership_verified=True,
                    )
                    if stop_error:
                        return json.dumps({
                            "success": False,
                            "status": "stop_failed",
                            "lifecycle": lifecycle,
                            "server_ok": _server_online(server_url)[0],
                            "server": server_url,
                            "pid": str(pid),
                            "error": stop_error,
                            "fallback": "Inspect the owned process group and permissions.",
                        }, ensure_ascii=False, indent=2)
                    time.sleep(1)
                    online, device_or_error = _server_online(server_url)
                    if group_remains or online:
                        return json.dumps({
                            "success": False,
                            "status": "stop_incomplete",
                            "lifecycle": lifecycle,
                            "server_ok": online,
                            "server": server_url,
                            "stopped_pids": [str(pid)],
                            "error": (
                                "Owned process group is still present after SIGKILL."
                                if group_remains
                                else device_or_error
                            ),
                            "stderr": "",
                            "fallback": "Use shell to inspect the remaining owned group or listener.",
                        }, ensure_ascii=False, indent=2)
                    _discard_native_pid_metadata(metadata_path, expected=metadata)
                    return json.dumps({
                        "success": True,
                        "status": "stopped",
                        "lifecycle": lifecycle,
                        "server_ok": False,
                        "server": server_url,
                        "stopped_pids": [str(pid)],
                        "error": "",
                        "stderr": "",
                        "fallback": "",
                    }, ensure_ascii=False, indent=2)
            except _NativeLifecycleBusyError as exc:
                return json.dumps({
                    "success": False,
                    "status": "lifecycle_busy",
                    "lifecycle": lifecycle,
                    "server_ok": _server_online(server_url)[0],
                    "server": server_url,
                    "error": str(exc),
                    "fallback": "Retry after the active native lifecycle operation finishes.",
                }, ensure_ascii=False, indent=2)
            except OSError as exc:
                return json.dumps({
                    "success": False,
                    "status": "not_stopped",
                    "lifecycle": lifecycle,
                    "server_ok": _server_online(server_url)[0],
                    "server": server_url,
                    "error": str(exc),
                    "fallback": "Inspect the native lifecycle state path and permissions.",
                }, ensure_ascii=False, indent=2)

        time.sleep(1)
        online, device_or_error = _server_online(server_url)
        return json.dumps({
            "success": not online,
            "status": "stopped" if not online else "stop_incomplete",
            "lifecycle": lifecycle,
            "server_ok": online,
            "server": server_url,
            "stopped_pids": pids,
            "error": "" if not online else device_or_error,
            "stderr": stderr,
            "fallback": "Use shell to inspect the remaining matching process or listener." if online else "",
        }, ensure_ascii=False, indent=2)

    def _comfyui_draw(
        prompt: str,
        negative_prompt: str = "",
        preset: str = "monochrome",
        hair_mode: str = "auto",
        seed: int | None = None,
        cfg: float | None = None,
        steps: int | None = None,
        width: int | None = None,
        height: int | None = None,
        lora_strength: float = 0.8,
        include_base_tags: bool = True,
        copy_to_astra_cache: bool = False,
        workflow_path: str = "",
        poll_timeout_seconds: int | None = None,
        _progress=None,
    ) -> str:
        preset_data = PRESETS[preset]
        cfg = float(cfg if cfg is not None else preset_data["cfg"])
        steps = int(steps if steps is not None else preset_data["steps"])
        width = int(width if width is not None else preset_data["width"])
        height = int(height if height is not None else preset_data["height"])
        seed = _validate_generation_args(seed, width, height, lora_strength)
        poll_timeout_seconds = int(poll_timeout_seconds or _positive_int_env("COMFYUI_POLL_TIMEOUT", 180))

        positive, negative = _build_prompts(
            prompt,
            negative_prompt,
            preset=preset,
            hair_mode=hair_mode,
            include_base_tags=include_base_tags,
        )

        if _progress:
            _progress("building_workflow")
        workflow = _load_workflow(workflow_path)
        filename_prefix = f"agent_output/agent_{uuid.uuid4().hex[:8]}"
        workflow = _apply_workflow_settings(
            workflow,
            prompt=positive,
            negative_prompt=negative,
            seed=seed,
            cfg=cfg,
            steps=steps,
            width=width,
            height=height,
            lora_strength=lora_strength,
            filename_prefix=filename_prefix,
        )

        server_url = _local_comfy_url()
        client_id = "agent-" + uuid.uuid4().hex[:8]
        if _progress:
            _progress("submitting")
        response = _request_json(
            f"{server_url}/prompt",
            {"prompt": workflow, "client_id": client_id},
            timeout=30,
        )
        prompt_id = response["prompt_id"]
        if _progress:
            _progress("generating", message=f"job {prompt_id[:8]}")
        deadline = time.monotonic() + poll_timeout_seconds
        started_waiting = time.monotonic()
        last_reported_second = -5
        downloaded: list[str] = []

        while time.monotonic() < deadline:
            time.sleep(1)
            elapsed_seconds = int(time.monotonic() - started_waiting)
            if _progress and elapsed_seconds - last_reported_second >= 5:
                _progress("generating", current=elapsed_seconds, unit="s", message=f"job {prompt_id[:8]}")
                last_reported_second = elapsed_seconds
            history = _request_json(f"{server_url}/history/{prompt_id}", timeout=10)
            if prompt_id not in history:
                continue
            images = [
                image
                for output in history[prompt_id].get("outputs", {}).values()
                for image in output.get("images", [])
            ]
            for image_index, image in enumerate(images, start=1):
                if _progress:
                    _progress("downloading", current=image_index, total=len(images), unit="images")
                out_path = _download_image(server_url, image, default_output_dir, prompt_id)
                downloaded.append(str(out_path))
                if copy_to_astra_cache or _env_bool("COMFYUI_COPY_TO_ASTRA_CACHE"):
                    configured_cache = os.getenv("ASTRA_IMAGE_CACHE", "").strip()
                    cache = (
                        Path(configured_cache).expanduser()
                        if configured_cache
                        else Path(workdir).resolve() / ".astra" / "cache" / "images"
                    )
                    cache.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(out_path, cache / out_path.name)
            if downloaded:
                result = {
                    "success": True,
                    "paths": downloaded,
                    "prompt_id": prompt_id,
                    "seed": seed,
                    "preset": preset,
                    "width": width,
                    "height": height,
                    "cfg": cfg,
                    "steps": steps,
                    "prompt": positive,
                    "negative_prompt": negative,
                }
                return json.dumps(result, ensure_ascii=False, indent=2)

        raise TimeoutError(f"ComfyUI prompt {prompt_id} did not finish within {poll_timeout_seconds}s")

    def _comfyui_anima_status() -> str:
        server_url = _anima_comfy_url()
        status: dict[str, Any] = {
            "server": server_url,
            "server_ok": False,
            "anima_ready": False,
            "model_versions": [],
            "missing_assets": [],
            "device": "",
            "error": "",
        }
        try:
            stats = _request_json(f"{server_url}/system_stats", timeout=5)
            status["server_ok"] = True
            devices = stats.get("devices") or []
            if devices:
                status["device"] = str(devices[0].get("name", ""))
            inventories = {
                "models": set(_request_json(f"{server_url}/models/diffusion_models", timeout=10)),
                "loras": set(_request_json(f"{server_url}/models/loras", timeout=10)),
                "clips": set(_request_json(f"{server_url}/models/text_encoders", timeout=10)),
                "vaes": set(_request_json(f"{server_url}/models/vae", timeout=10)),
            }
            status["model_versions"] = [
                version for version, filename in ANIMA_MODELS.items()
                if filename in inventories["models"]
            ]
            required = {
                "clip": (ANIMA_CLIP, inventories["clips"]),
                "vae": (ANIMA_VAE, inventories["vaes"]),
                "turbo_lora": (ANIMA_TURBO_LORA, inventories["loras"]),
                "highres_lora": (ANIMA_HIGHRES_LORA, inventories["loras"]),
                "saturation_lora": (ANIMA_SATURATION_LORA, inventories["loras"]),
                "lyra_v2_lora": (ANIMA_LYRA_V2_LORA, inventories["loras"]),
            }
            status["missing_assets"] = [
                f"{label}: {filename}" for label, (filename, available) in required.items()
                if filename not in available
            ]
            status["anima_ready"] = bool(status["model_versions"]) and not status["missing_assets"]
        except Exception as exc:
            status["error"] = str(exc)
        return json.dumps(status, ensure_ascii=False, indent=2)

    def _comfyui_anima_draw(
        prompt: str,
        negative_prompt: str = "",
        profile: str = "signature-muted",
        hair_mode: str = "soft",
        rating: str = "sfw",
        orientation: str = "portrait",
        resolution: str = "normal",
        model_version: str = "v2",
        seed: int | None = None,
        saturation: float | None = None,
        pure_bw: bool = False,
        extra_tags: str = "",
        _progress=None,
    ) -> str:
        if _progress:
            _progress("building_workflow")
        width, height = _anima_dimensions(orientation, resolution)
        seed = _validate_generation_args(seed, width, height, 0.8)
        positive, negative, profile_saturation = _build_anima_prompts(
            prompt,
            profile=profile,
            hair_mode=hair_mode,
            rating=rating,
            pure_bw=pure_bw,
            negative_prompt=negative_prompt,
            extra_tags=extra_tags,
        )
        if saturation is not None:
            if profile != "signature-muted" or pure_bw:
                raise ValueError("saturation override is only available for signature-muted without pure_bw")
            if saturation < -3.2 or saturation > -1.2:
                raise ValueError("saturation must be between -3.2 and -1.2")
            profile_saturation = float(saturation)
        run_name = f"{profile.replace('-', '_')}_{uuid.uuid4().hex[:8]}"
        filename_prefix = f"agent_anima/{run_name}"
        workflow = _build_anima_workflow(
            prompt=positive,
            negative_prompt=negative,
            seed=seed,
            width=width,
            height=height,
            saturation=profile_saturation,
            model_version=model_version,
            filename_prefix=filename_prefix,
        )
        server_url = _anima_comfy_url()
        if _progress:
            _progress("submitting")
        response = _request_json(
            f"{server_url}/prompt",
            {"prompt": workflow, "client_id": "agent-anima-" + uuid.uuid4().hex[:8]},
            timeout=30,
        )
        prompt_id = str(response.get("prompt_id", ""))
        if not prompt_id:
            raise RuntimeError(f"ComfyUI rejected the Anima job: {response}")
        if _progress:
            _progress("queued", status="completed", message=f"job {prompt_id[:8]}")
        return json.dumps({
            "success": True,
            "status": "queued",
            "engine": "anima",
            "prompt_id": prompt_id,
            "server": server_url,
            "model_version": model_version,
            "profile": profile,
            "hair_mode": hair_mode,
            "seed": seed,
            "width": width,
            "height": height,
            "saturation": profile_saturation,
            "filename_prefix": filename_prefix,
            "message": (
                "Anima job submitted. Do not poll in this turn. "
                "Use comfyui_result with this prompt_id later when the user asks to inspect it."
            ),
        }, ensure_ascii=False, indent=2)

    def _comfyui_result(prompt_id: str, engine: str = "anima", _progress=None) -> str:
        prompt_id = prompt_id.strip()
        if not prompt_id or any(ch not in "0123456789abcdefABCDEF-" for ch in prompt_id):
            raise ValueError("prompt_id must be a UUID-like ComfyUI prompt id")
        if engine not in {"anima", "noobai"}:
            raise ValueError("engine must be anima or noobai")
        server_url = _anima_comfy_url() if engine == "anima" else _local_comfy_url()
        if _progress:
            _progress("checking", message=f"job {prompt_id[:8]}")
        history = _request_json(f"{server_url}/history/{prompt_id}", timeout=15)
        entry = history.get(prompt_id)
        if not entry:
            return json.dumps({
                "success": True,
                "status": "pending",
                "prompt_id": prompt_id,
                "message": "No completed output yet. Do not repeatedly poll; ask again later.",
            }, ensure_ascii=False, indent=2)
        downloaded: list[str] = []
        images = [
            image
            for output in entry.get("outputs", {}).values()
            for image in output.get("images", [])
        ]
        for image_index, image in enumerate(images, start=1):
            if _progress:
                _progress("downloading", current=image_index, total=len(images), unit="images")
            downloaded.append(str(_download_image(server_url, image, default_output_dir, prompt_id)))
        if not downloaded:
            status = entry.get("status", {})
            return json.dumps({
                "success": False,
                "status": "failed" if status.get("status_str") == "error" else "pending",
                "prompt_id": prompt_id,
                "details": status,
            }, ensure_ascii=False, indent=2)
        return json.dumps({
            "success": True,
            "status": "completed",
            "type": "image_attachment",
            "engine": engine,
            "prompt_id": prompt_id,
            "image_paths": downloaded,
            "paths": downloaded,
            "question": "Inspect the generated image and report visible quality, composition, and prompt adherence.",
        }, ensure_ascii=False, indent=2)

    registry.register(ToolDef(
        name="comfyui_status",
        description=(
            "Check whether local ComfyUI is online and report workflow/device state. "
            "A successful tool call is not proof that the server is online: inspect server_ok. "
            "If server_ok is false, prefer comfyui_start before using shell."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_comfyui_status,
        sandboxed=False,
        timeout=10, risk="read", idempotent=True, group="image",
    ))
    registry.register(ToolDef(
        name="comfyui_start",
        description=(
            "Start the configured WSL/native ComfyUI instance, or diagnose an externally managed one, "
            "and verify /system_stats before reporting success. "
            "Use for routine startup. If it returns success=false, inspect its error/log_tail and only then "
            "use shell for the specific uncovered failure."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_comfyui_start,
        sandboxed=False, timeout=comfyui_start_tool_timeout, risk="execute", approval="on_risk",
        max_retries=0, repeat_guard=True, group="image",
    ))
    registry.register(ToolDef(
        name="comfyui_stop",
        description=(
            "Stop the selected managed ComfyUI instance after an explicit user request or when a restart is necessary. "
            "WSL targets matching main.py processes; native mode targets only the Astra-owned verified process group."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_comfyui_stop,
        sandboxed=False, timeout=40, risk="execute", approval="on_risk",
        max_retries=0, repeat_guard=True, group="image",
    ))
    registry.register(ToolDef(
        name="comfyui_draw",
        description=(
            "Use local ComfyUI to generate an image from Danbooru-style tags. "
            "Default preset is a SFW monochrome line-art catgirl selfie. "
            "Use presets: monochrome, sunset, starry, custom. Prefer this over manually posting workflow JSON; "
            "use shell only when this tool cannot express or diagnose the requested workflow."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Positive Danbooru-style tags for the scene/pose/clothing."},
                "negative_prompt": {"type": "string", "description": "Optional extra negative tags.", "default": ""},
                "preset": {"type": "string", "enum": list(PRESETS.keys()), "default": "monochrome"},
                "hair_mode": {"type": "string", "enum": ["auto", "up", "down"], "default": "auto"},
                "seed": {"type": "integer", "description": "Optional deterministic seed."},
                "cfg": {"type": "number", "description": "Optional CFG override."},
                "steps": {"type": "integer", "description": "Optional sampler steps override."},
                "width": {"type": "integer", "description": "Optional width override; multiple of 8."},
                "height": {"type": "integer", "description": "Optional height override; multiple of 8."},
                "lora_strength": {"type": "number", "description": "Main artist LoRA strength.", "default": 0.8},
                "include_base_tags": {"type": "boolean", "description": "Whether to prepend the Lyra base tags.", "default": True},
                "copy_to_astra_cache": {"type": "boolean", "description": "Copy output to ASTRA_IMAGE_CACHE, or .astra/cache/images by default, after download.", "default": False},
                "workflow_path": {"type": "string", "description": "Optional workflow JSON path override.", "default": ""},
                "poll_timeout_seconds": {"type": "integer", "description": "Optional generation wait timeout.", "default": 180},
            },
            "required": ["prompt"],
        },
        fn=_comfyui_draw, risk="write",
        sandboxed=False,
        timeout=None,
        max_retries=0,
        repeat_guard=False,
        group="image",
    ))
    registry.register(ToolDef(
        name="comfyui_anima_status",
        description=(
            "Check whether the local Anima/Qwen ComfyUI stack is online and all locked model, "
            "CLIP, VAE, and LoRA assets are available."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_comfyui_anima_status,
        sandboxed=False, timeout=20, risk="read", idempotent=True, group="image",
    ))
    registry.register(ToolDef(
        name="comfyui_anima_draw",
        description=(
            "Submit a Lyra image to the mature Anima/Qwen signature-lineart workflow. "
            "Uses Anima v2, Qwen 0.6B CLIP, correct Qwen VAE, Turbo/Aesthetic/Saturation/Lyra-v2 LoRAs, "
            "16 steps and CFG 1.0. Prefer this over scripts, curl, or hand-written workflow JSON for supported "
            "Lyra/Anima requests. Returns a prompt_id immediately; without prompt_id the task was not submitted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Concrete English clothing, pose, expression, and scene details; omit style tags."},
                "negative_prompt": {"type": "string", "description": "Optional extra negative concepts.", "default": ""},
                "profile": {"type": "string", "enum": list(ANIMA_PROFILES), "default": "signature-muted"},
                "hair_mode": {"type": "string", "enum": list(ANIMA_HAIR), "default": "soft"},
                "rating": {"type": "string", "enum": ["sfw", "adult"], "default": "sfw"},
                "orientation": {"type": "string", "enum": ["portrait", "landscape", "square"], "default": "portrait"},
                "resolution": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
                "model_version": {"type": "string", "enum": list(ANIMA_MODELS), "default": "v2"},
                "seed": {"type": "integer", "description": "Optional deterministic seed."},
                "saturation": {"type": "number", "description": "Optional signature-muted override from -3.2 to -1.2."},
                "pure_bw": {"type": "boolean", "description": "Remove all color including red eyes.", "default": False},
                "extra_tags": {"type": "string", "description": "Optional extra character tags; adding another character removes solo.", "default": ""},
            },
            "required": ["prompt"],
        },
        fn=_comfyui_anima_draw, risk="write", sandboxed=False, timeout=40,
        max_retries=0, repeat_guard=False, group="image",
    ))
    registry.register(ToolDef(
        name="comfyui_result",
        description=(
            "Check one previously submitted ComfyUI prompt once. Call only after the user asks later; "
            "never poll repeatedly in the same turn. Completed images are downloaded and attached for visual inspection."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt_id": {"type": "string", "description": "Prompt id returned by a ComfyUI draw tool."},
                "engine": {"type": "string", "enum": ["anima", "noobai"], "default": "anima"},
            },
            "required": ["prompt_id"],
        },
        fn=_comfyui_result, risk="read", sandboxed=False, timeout=30,
        max_retries=0, group="image",
    ))
