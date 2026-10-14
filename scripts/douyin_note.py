#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_NAME = "douyin-learning-note"
VERSION = "1.2.0"
URL_RE = re.compile(r"https?://[^\s]+")


def current_platform() -> str:
    """当前平台：windows / macos / linux。

    DOUYIN_NOTE_OS 可显式覆盖（windows|macos|linux），便于跨平台自检与脚本化验证。
    """
    override = (os.environ.get("DOUYIN_NOTE_OS") or "").strip().lower()
    if override:
        if override in ("win", "windows", "nt", "win32"):
            return "windows"
        if override in ("mac", "macos", "darwin", "osx"):
            return "macos"
        if override in ("linux", "posix", "unix"):
            return "linux"
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    return "linux"


def default_work_root() -> str:
    """临时会话根目录：按平台选择惯用的缓存位置。"""
    plat = current_platform()
    if plat == "windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return str(Path(base) / APP_NAME / "sessions")
    if plat == "macos":
        return f"~/Library/Caches/{APP_NAME}/sessions"
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return str(Path(base).expanduser() / APP_NAME / "sessions")


def default_vault_path() -> str:
    """默认 Obsidian vault：两个平台都是「桌面/Note」。"""
    return str(Path.home() / "Desktop" / "Note")


def default_user_agent() -> str:
    """UA 与真实运行平台保持一致，避免伪造 UA 触发抖音风控。"""
    plat = current_platform()
    if plat == "windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
        )
    if plat == "macos":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )


def default_browser_profile_dir() -> str:
    """固定浏览器 profile：复用 Cookie/指纹，避免每次都被当新访客而触发验证码。"""
    return str(Path(default_work_root()).expanduser().parent / "browser-profile")


UA = default_user_agent()


DEFAULT_CONFIG: Dict[str, Any] = {
    "vault_path": default_vault_path(),
    "note_subdir": "学习/视频笔记",
    "asr_backend": "auto",
    "model": "mlx-community/whisper-large-v3-mlx",
    "faster_model": "large-v3",
    "faster_device": "cpu",
    "faster_compute_type": "int8",
    "faster_vad_filter": True,
    "language": "zh",
    "word_timestamps": False,
    "include_timestamps": True,
    "note_mode": "auto",
    "pypi_index": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "hf_endpoint": "https://hf-mirror.com",
    "browser_channel": "auto",
    "browser_profile_dir": "",
    "user_agent": "",
    "headless": False,
    "proxy_url": "",
    "keep_temp_on_failure": True,
    "work_root": default_work_root(),
    "max_duration_minutes": 0,
    "download_timeout_seconds": 120,
    "transcription_estimate_factor": 3.0,
}


class SkillError(RuntimeError):
    pass


class GeoBlockedError(SkillError):
    pass


class CaptureError(SkillError):
    pass


class TranscriptionError(SkillError):
    pass


class CommitError(SkillError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def config_path() -> Path:
    """共享配置路径：Windows 用 %APPDATA%，macOS/Linux 用 ~/.config。

    DOUYIN_NOTE_CONFIG 可覆盖；Windows 首次运行时若只存在旧的 ~/.config 配置则继续沿用。
    """
    override = os.environ.get("DOUYIN_NOTE_CONFIG")
    if override:
        return Path(override).expanduser()
    if current_platform() == "windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        primary = Path(base) / APP_NAME / "config.json"
        legacy = Path.home() / ".config" / APP_NAME / "config.json"
        if not primary.exists() and legacy.exists():
            return legacy
        return primary
    return Path.home() / ".config" / APP_NAME / "config.json"


def load_config() -> Dict[str, Any]:
    path = config_path()
    if not path.exists():
        atomic_write_json(path, DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    data = read_json(path, {}) or {}
    merged = deep_merge(DEFAULT_CONFIG, data)
    # 允许配置里留空表示「用平台默认值」，避免空字符串被展开成当前目录
    for key in ("vault_path", "work_root", "model", "note_subdir", "asr_backend", "browser_channel"):
        if not merged.get(key):
            merged[key] = DEFAULT_CONFIG[key]
    return merged


def work_root(config: Dict[str, Any]) -> Path:
    return Path(config.get("work_root") or default_work_root()).expanduser()


def apply_network_env(config: Dict[str, Any]) -> None:
    """把代理意图落到进程环境变量。

    Windows 上 Python 的 requests 会自动继承系统/IE 代理设置（常见是 Clash 之类），
    即使配置里没写 proxy_url 也会被套上代理，于是出现「curl 直连正常、Python 连接被重置」。
    这里显式表达意图：配了 proxy_url 就用它，没配就强制直连。
    """
    proxy = (config.get("proxy_url") or "").strip()
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[key] = proxy
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        return
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(key, None)


def session_dir(config: Dict[str, Any], session_id: str) -> Path:
    return work_root(config) / session_id


def read_state(config: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    path = session_dir(config, session_id) / "state.json"
    data = read_json(path)
    if not data:
        raise SkillError(f"session not found: {session_id}")
    return data


def update_state(config: Dict[str, Any], session_id: str, state: str, stage: str,
                 progress: Optional[float] = None, error: Optional[Dict[str, Any]] = None,
                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    path = session_dir(config, session_id) / "state.json"
    data = read_json(path, {}) or {}
    data.update({
        "session_id": session_id,
        "state": state,
        "stage": stage,
        "progress": progress,
        "updated_at": now_iso(),
    })
    if error is not None:
        data["error"] = error
    if extra:
        data.update(extra)
    atomic_write_json(path, data)
    return data


def resolve_session_dir(config: Dict[str, Any], session_id: str) -> Path:
    path = session_dir(config, session_id)
    if not path.exists():
        raise SkillError(f"session not found: {session_id}")
    return path


def extract_url(text: str) -> str:
    match = URL_RE.search(text or "")
    if not match:
        raise SkillError("没有找到抖音链接")
    return match.group(0).rstrip("，。,.；;！!？?、")


def parse_share_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    author = None
    title = None
    match = re.search(r"【(.+?)的作品】", text or "")
    if match:
        author = match.group(1).strip()
    match = re.search(r"【.+?的作品】\s*(.*?)\s*https?://", text or "", re.S)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
    return author, title


def resolve_video_id(url: str) -> Optional[str]:
    match = re.search(r"/(?:video|share/video)/(\d+)", url)
    if match:
        return match.group(1)
    try:
        import requests
        response = requests.get(
            url,
            headers={
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=15,
            allow_redirects=True,
        )
        final = response.url
        match = re.search(r"/(?:video|share/video)/(\d+)", final)
        if match:
            return match.group(1)
        match = re.search(r"[?&]modal_id=(\d+)", final)
        if match:
            return match.group(1)
        match = re.search(r'"aweme_id"\s*:\s*"(\d+)"', response.text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def find_existing_note(vault: Path, source_id: Optional[str]) -> Optional[Path]:
    if not source_id or not vault.exists():
        return None
    needle = re.compile(rf'^source_id:\s*["\']?{re.escape(source_id)}["\']?\s*$', re.M)
    skip_parts = {".obsidian", ".git", ".qoder", "node_modules"}
    for path in vault.rglob("*.md"):
        if any(part in skip_parts for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if needle.search(text):
            return path
    return None


def format_seconds(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds)))
    except Exception:
        return "00:00"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path
    candidates: List[str] = [
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates += [
            str(Path(local_appdata) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"),
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise SkillError("ffmpeg not found; install ffmpeg first")


def browser_candidates() -> List[Tuple[str, Optional[str]]]:
    """按优先级返回 (可执行文件, playwright channel) 候选；channel=None 表示用自带 chromium。"""
    plat = current_platform()
    items: List[Tuple[str, Optional[str]]] = []
    if plat == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        chrome_dirs = [
            Path(program_files) / "Google" / "Chrome" / "Application",
            Path(program_files_x86) / "Google" / "Chrome" / "Application",
        ]
        if local_appdata:
            chrome_dirs.append(Path(local_appdata) / "Google" / "Chrome" / "Application")
        items += [(str(directory / "chrome.exe"), "chrome") for directory in chrome_dirs]
        items += [
            (str(Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"), "msedge"),
            (str(Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe"), "msedge"),
        ]
    elif plat == "macos":
        items += [
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chrome"),
            ("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary", "chrome"),
            ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "msedge"),
            ("/Applications/Chromium.app/Contents/MacOS/Chromium", None),
        ]
    for command, channel in (
        ("google-chrome", "chrome"),
        ("google-chrome-stable", "chrome"),
        ("microsoft-edge", "msedge"),
        ("microsoft-edge-stable", "msedge"),
        ("chromium", None),
        ("chromium-browser", None),
        ("chrome", "chrome"),
        ("msedge", "msedge"),
    ):
        found = shutil.which(command)
        if found:
            items.append((found, channel))
    return items


def find_browser(config: Optional[Dict[str, Any]] = None) -> Dict[str, Optional[str]]:
    """定位可用浏览器，返回 {path, channel, source}。

    browser_channel 支持：auto（默认）/ chrome / msedge / chromium（自带）/ 可执行文件绝对路径。
    """
    requested = str((config or {}).get("browser_channel") or "auto").strip()
    lowered = requested.lower()
    if lowered in ("chromium", "bundled", "none", "playwright"):
        return {"path": None, "channel": None, "source": "bundled-chromium"}
    candidates = browser_candidates()
    if lowered and lowered != "auto":
        for path, channel in candidates:
            if channel == lowered and Path(path).exists():
                return {"path": path, "channel": channel, "source": "config"}
        if Path(requested).exists():
            return {"path": requested, "channel": None, "source": "config-path"}
        for path, channel in candidates:
            if Path(path).exists():
                return {"path": path, "channel": channel, "source": "auto-fallback"}
        return {"path": None, "channel": lowered, "source": "config-forced"}
    for path, channel in candidates:
        if Path(path).exists():
            return {"path": path, "channel": channel, "source": "auto"}
    return {"path": None, "channel": None, "source": "none"}


def find_chrome() -> Optional[str]:
    """兼容旧调用：返回自动探测到的浏览器可执行文件路径。"""
    return find_browser().get("path")


def choose_media_url(requests_list: List[Dict[str, Any]], video: Any) -> Optional[str]:
    urls = []
    for item in requests_list:
        url = item.get("url") or ""
        if not url.startswith("http"):
            continue
        if "douyinvod.com" not in url and "video/tos" not in url:
            continue
        urls.append(url)

    def score(url: str) -> float:
        value = 0.0
        if "media-audio-und-mp4a" in url:
            value += 1000.0
        elif "media-video" in url:
            value += 500.0
        if "mime_type=video_mp4" in url:
            value += 50.0
        match = re.search(r"[?&]br=(\d+)", url)
        if match:
            value += min(int(match.group(1)), 5000) / 10000.0
        return value

    unique = sorted(set(urls), key=score, reverse=True)
    if unique:
        return unique[0]

    try:
        current = video.evaluate("e => e.currentSrc || e.src || ''")
        if current and current.startswith("http"):
            return current
    except Exception:
        pass
    return None


def resolve_asr_backend(config: Dict[str, Any]) -> str:
    """转写后端：mlx（macOS Apple Silicon）/ faster（Windows、Linux、Intel Mac）/ auto。"""
    requested = str(config.get("asr_backend") or "auto").strip().lower()
    if requested in ("mlx", "mlx-whisper", "mlx_whisper"):
        return "mlx"
    if requested in ("faster", "faster-whisper", "faster_whisper"):
        return "faster"
    return "mlx" if current_platform() == "macos" else "faster"


def find_uv() -> Optional[str]:
    uv = os.environ.get("DOUYIN_NOTE_UV") or shutil.which("uv")
    if uv:
        return uv
    candidates = [
        Path.home() / ".local/bin/uv",
        Path.home() / ".cargo/bin/uv",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Microsoft/WinGet/Links/uv.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "uv/uv.exe",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        # pip install --user uv 会装到 %APPDATA%\Python\Python3xx\Scripts\uv.exe
        candidates += sorted(Path(appdata).glob("Python/Python3*/Scripts/uv.exe"))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def worker_python_packages(config: Dict[str, Any]) -> List[str]:
    """worker 需要临时安装的依赖，按平台选择转写后端。"""
    backend = resolve_asr_backend(config)
    return ["playwright", "requests", "mlx-whisper" if backend == "mlx" else "faster-whisper"]


def spawn_worker(config: Dict[str, Any], session_id: str) -> int:
    uv = find_uv()
    if not uv:
        raise SkillError(
            "uv not found; install uv first "
            "(Windows: winget install --id astral-sh.uv -e)"
        )
    script = Path(__file__).resolve()
    path = session_dir(config, session_id)
    cmd = [uv, "run", "--python", "3.12"]
    for package in worker_python_packages(config):
        cmd += ["--with", package]
    cmd += ["--", "python", str(script), "_worker", "--session", session_id]
    env = os.environ.copy()
    env["HF_ENDPOINT"] = config.get("hf_endpoint") or env.get("HF_ENDPOINT") or "https://hf-mirror.com"
    env["UV_DEFAULT_INDEX"] = config.get("pypi_index") or env.get("UV_DEFAULT_INDEX") or "https://pypi.tuna.tsinghua.edu.cn/simple"
    env["PYTHONUNBUFFERED"] = "1"
    log_path = path / "worker.log"
    log = open(log_path, "ab", buffering=0)
    popen_kwargs: Dict[str, Any] = {
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(path),
        "env": env,
        "close_fds": True,
    }
    if os.name == "nt":
        # Windows 没有 setsid：用 DETACHED_PROCESS 脱离控制台，父进程退出后 worker 继续运行
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(cmd, **popen_kwargs)
    return process.pid


def quick_geo_blocked(url: str) -> bool:
    try:
        import requests
        response = requests.get(
            url,
            headers={
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=15,
            allow_redirects=True,
        )
        text = response.text or ""
        return ('"isMainland":false' in text) or ('"country":"hk"' in text) or ('country:"hk"' in text)
    except Exception:
        return False


def start_command(args: argparse.Namespace) -> int:
    config = load_config()
    apply_network_env(config)
    text = args.input
    share_url = extract_url(text)
    share_author, share_title = parse_share_text(text)
    video_id = resolve_video_id(share_url)

    if quick_geo_blocked(share_url):
        print(json.dumps({
            "ok": False,
            "state": "geo_blocked",
            "error": "抖音识别当前网络不是大陆出口。请关闭 VPN 或配置大陆代理后重试。",
        }, ensure_ascii=False, indent=2))
        return 1

    vault = Path(args.vault or config["vault_path"]).expanduser()
    subdir = args.subdir or config["note_subdir"]

    if video_id and not args.force:
        existing = find_existing_note(vault, video_id)
        if existing:
            print(json.dumps({
                "ok": True,
                "state": "duplicate",
                "existing_note": str(existing),
                "message": "这条视频已经有笔记；如需重新整理，请使用 --force。",
            }, ensure_ascii=False, indent=2))
            return 0

    root = work_root(config)
    root.mkdir(parents=True, exist_ok=True)
    base = video_id or ("douyin-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    session_id = base
    counter = 2
    while session_dir(config, session_id).exists():
        session_id = f"{base}-{counter}"
        counter += 1

    path = session_dir(config, session_id)
    path.mkdir(parents=True, exist_ok=True)
    manifest = {
        "session_id": session_id,
        "input": text,
        "share_url": share_url,
        "video_id": video_id,
        "share_author": share_author,
        "share_title": share_title,
        "created_at": now_iso(),
        "overrides": {
            "vault": args.vault,
            "subdir": args.subdir,
            "force": bool(args.force),
            "mode": args.mode,
        },
    }
    atomic_write_json(path / "manifest.json", manifest)
    update_state(config, session_id, "starting", "init", 0.0)

    try:
        pid = spawn_worker(config, session_id)
    except Exception as exc:
        update_state(config, session_id, "failed", "init", None,
                     error={"type": type(exc).__name__, "message": str(exc)})
        print(json.dumps({"ok": False, "error": str(exc), "session_dir": str(path)},
                         ensure_ascii=False, indent=2))
        return 1

    state = read_state(config, session_id)
    print(json.dumps({
        "ok": True,
        "session_id": session_id,
        "session_dir": str(path),
        "state": state.get("state"),
        "worker_pid": pid,
        "message": "后台 worker 已启动。",
    }, ensure_ascii=False, indent=2))
    return 0


def capture_video(config: Dict[str, Any], manifest: Dict[str, Any], path: Path) -> Dict[str, Any]:
    from playwright.sync_api import sync_playwright

    video_id = manifest.get("video_id")
    share_url = manifest["share_url"]
    canonical_url = f"https://www.douyin.com/video/{video_id}" if video_id else share_url
    profile_dir = Path(config.get("browser_profile_dir") or default_browser_profile_dir()).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    media_requests: List[Dict[str, Any]] = []
    metadata: Dict[str, Any] = {}
    success = False
    context = None

    def on_request(request: Any) -> None:
        url = request.url
        if ("douyinvod.com" in url or "video/tos" in url or "mime_type=video_mp4" in url):
            media_requests.append({"url": url, "resource_type": request.resource_type})

    try:
        browser = find_browser(config)
        if browser.get("source") == "none":
            raise CaptureError(
                "未找到可用的 Chrome / Edge / Chromium。请安装任一浏览器，"
                "或在配置里把 browser_channel 设为 chromium 并执行 playwright install chromium。"
            )
        with sync_playwright() as play:
            proxy = {"server": config["proxy_url"]} if config.get("proxy_url") else None
            launch_kwargs: Dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": bool(config.get("headless", False)),
                "locale": "zh-CN",
                "user_agent": config.get("user_agent") or UA,
                "viewport": {"width": 1440, "height": 900},
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                "proxy": proxy,
            }
            if browser.get("channel"):
                launch_kwargs["channel"] = browser["channel"]
            context = play.chromium.launch_persistent_context(**launch_kwargs)
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "window.chrome = window.chrome || {runtime: {}};"
            )
            page = context.new_page()
            page.on("request", on_request)

            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
            is_mainland = page.evaluate(
                "() => (window.EXPOSE_DATA && typeof window.EXPOSE_DATA.isMainland === 'boolean') "
                "? window.EXPOSE_DATA.isMainland : null"
            )
            if is_mainland is False:
                raise GeoBlockedError(
                    "抖音识别当前网络不是大陆出口。请关闭 VPN 或配置大陆代理后重试。"
                )

            page.goto(canonical_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(12000)
            is_mainland = page.evaluate(
                "() => (window.EXPOSE_DATA && typeof window.EXPOSE_DATA.isMainland === 'boolean') "
                "? window.EXPOSE_DATA.isMainland : null"
            )
            if is_mainland is False:
                raise GeoBlockedError(
                    "抖音识别当前网络不是大陆出口。请关闭 VPN 或配置大陆代理后重试。"
                )
            # 抖音的 <video> 可能先以隐藏占位出现，所以等到 attached 再轮询到可解析时长
            page.wait_for_selector("video", state="attached", timeout=45000)
            video = page.locator("video").first
            for _ in range(30):
                try:
                    ready = bool(video.evaluate(
                        "e => e.readyState >= 1 || (Number.isFinite(e.duration) && e.duration > 0)"
                    ))
                except Exception:
                    ready = False
                if ready:
                    break
                page.wait_for_timeout(1000)

            try:
                duration = video.evaluate("e => Number.isFinite(e.duration) ? e.duration : null")
            except Exception:
                duration = None

            if config.get("max_duration_minutes") and duration:
                if duration > float(config["max_duration_minutes"]) * 60:
                    raise CaptureError("视频时长超过配置上限")

            title = ""
            try:
                if page.locator("h1").count():
                    title = page.locator("h1").first.inner_text().strip()
            except Exception:
                title = ""
            if not title:
                title = page.title().replace(" - 抖音", "").strip()
            if not title:
                title = manifest.get("share_title") or "抖音视频"

            description = ""
            try:
                description = page.locator('meta[name="description"]').first.get_attribute("content") or ""
            except Exception:
                description = ""
            author = manifest.get("share_author")
            published = None
            match = re.search(r"-\s*(.+?)于(\d{8})发布", description)
            if match:
                author = match.group(1).strip()
                raw_date = match.group(2)
                published = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"

            metadata = {
                "source": canonical_url,
                "source_share_url": share_url,
                "source_id": video_id,
                "source_title": title,
                "platform": "抖音",
                "author": author,
                "published": published,
                "duration_seconds": duration,
                "duration": format_seconds(duration) if duration else None,
                "captured_at": now_iso(),
            }

            try:
                page.evaluate("() => { const v = document.querySelector('video'); if (v) { v.muted = false; v.play().catch(() => {}); } }")
            except Exception:
                pass
            page.wait_for_timeout(6000)

            for _ in range(5):
                if choose_media_url(media_requests, video):
                    break
                page.wait_for_timeout(3000)

            media_url = choose_media_url(media_requests, video)
            if not media_url:
                raise CaptureError("没有捕获到音视频播放流")

            response = context.request.get(
                media_url,
                headers={"Referer": canonical_url},
                timeout=int(config.get("download_timeout_seconds", 120)) * 1000,
            )
            if not response.ok:
                raise CaptureError(f"下载媒体流失败：HTTP {response.status}")
            media_path = path / "media.bin"
            media_path.write_bytes(response.body())
            success = True
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        # 保留 profile（Cookie/指纹/已通过的验证码），下次运行不必重新过风控

    ffmpeg = find_ffmpeg()
    wav_path = path / "audio.wav"
    media_path = path / "media.bin"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(wav_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except Exception as exc:
        raise CaptureError(f"ffmpeg 抽音频失败：{exc}") from exc
    media_path.unlink(missing_ok=True)
    atomic_write_json(path / "metadata.json", metadata)
    return {"wav_path": str(wav_path), "metadata": metadata}


def transcribe_with_mlx(config: Dict[str, Any], wav_path: str) -> Dict[str, Any]:
    """macOS（Apple Silicon）本机转写，离线、速度最快。"""
    import mlx_whisper

    return mlx_whisper.transcribe(
        str(wav_path),
        path_or_hf_repo=config["model"],
        language=config.get("language", "zh"),
        word_timestamps=bool(config.get("word_timestamps", False)),
        verbose=False,
    )


def load_wav_float32(wav_path: str) -> Any:
    """把 16k 单声道 WAV 读成 float32 数组。

    faster-whisper 默认用 PyAV 解码音频，但新版 PyAV（19.x）移除了它依赖的
    `metadata_errors` 参数，会直接 TypeError。传入 ndarray 可以跳过 PyAV 解码路径，
    也省掉一次重采样（ffmpeg 已经输出 16k 单声道）。
    """
    import wave

    import numpy as np

    with wave.open(str(wav_path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise TranscriptionError(f"WAV 位深不支持：{width * 8}bit（需要 16bit）")
    if rate != 16000:
        raise TranscriptionError(f"WAV 采样率不支持：{rate}Hz（需要 16000Hz）")
    data = np.frombuffer(frames, dtype="<i2")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return (data.astype("float32")) / 32768.0


def _faster_transcribe_once(config: Dict[str, Any], audio: Any, model_ref: str,
                            device: str, compute_type: str, note: str = "") -> Dict[str, Any]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_ref, device=device, compute_type=compute_type)
    segment_iter, info = model.transcribe(
        audio,
        language=config.get("language") or None,
        word_timestamps=bool(config.get("word_timestamps", False)),
        vad_filter=bool(config.get("faster_vad_filter", True)),
        beam_size=5,
    )
    segments = [
        {"start": float(segment.start), "end": float(segment.end), "text": segment.text}
        for segment in segment_iter
    ]
    return {
        "text": "".join(segment["text"] for segment in segments).strip(),
        "language": getattr(info, "language", None),
        "device": f"{device}/{compute_type}" + (f"（{note}）" if note else ""),
        "segments": segments,
    }


def transcribe_with_faster(config: Dict[str, Any], wav_path: str) -> Dict[str, Any]:
    """Windows / Linux 转写：CTranslate2 后端，CPU 或 CUDA 均可。

    device=auto/cuda 在有 NVIDIA 卡但没装 cuBLAS 的机器上会在**转写时**抛
    「Library cublas64_12.dll is not found」（构造模型时不报错），所以回退必须包住整段转写。
    """
    audio = load_wav_float32(wav_path)
    model_ref = str(config.get("faster_model") or "large-v3")
    device = str(config.get("faster_device") or "cpu")
    compute_type = str(config.get("faster_compute_type") or "int8")
    try:
        return _faster_transcribe_once(config, audio, model_ref, device, compute_type)
    except Exception as exc:
        message = str(exc).lower()
        gpu_markers = ("cublas", "cudnn", "cuda", "nvidia", "gpu")
        if device == "cpu" or not any(marker in message for marker in gpu_markers):
            raise
        return _faster_transcribe_once(
            config, audio, model_ref, "cpu", "int8",
            note=f"已从 {device}/{compute_type} 回退：{type(exc).__name__}",
        )


def transcribe_audio(config: Dict[str, Any], wav_path: str, path: Path) -> Dict[str, Any]:
    backend = resolve_asr_backend(config)
    result = transcribe_with_mlx(config, wav_path) if backend == "mlx" else transcribe_with_faster(config, wav_path)
    text = (result.get("text") or "").strip()
    if not text:
        raise TranscriptionError(f"转写结果为空（backend={backend}）")

    transcript_json = path / "transcript.json"
    transcript_txt = path / "transcript.txt"
    segments_md = path / "transcript_segments.md"
    atomic_write_json(transcript_json, dict(result, backend=backend))
    atomic_write_text(transcript_txt, text + "\n")

    lines = []
    for segment in result.get("segments", []):
        segment_text = (segment.get("text") or "").strip()
        if not segment_text:
            continue
        lines.append(f"[{format_seconds(segment.get('start', 0))}] {segment_text}")
    atomic_write_text(segments_md, "\n".join(lines) + "\n")

    try:
        Path(wav_path).unlink(missing_ok=True)
    except Exception:
        pass

    return {
        "backend": backend,
        "language": result.get("language"),
        "text_length": len(text),
        "segment_count": len(result.get("segments", [])),
        "paths": {
            "metadata_json": str(path / "metadata.json"),
            "transcript_txt": str(transcript_txt),
            "transcript_json": str(transcript_json),
            "transcript_segments_md": str(segments_md),
            "note_md": str(path / "note.md"),
        },
    }


def start_progress_thread(config: Dict[str, Any], session_id: str,
                          duration_seconds: Optional[float], stop_event: threading.Event) -> threading.Thread:
    factor = float(config.get("transcription_estimate_factor", 3.0) or 3.0)
    estimated_total = max(60.0, float(duration_seconds or 180.0) * factor)

    def loop() -> None:
        started = time.time()
        while not stop_event.wait(10):
            elapsed = time.time() - started
            progress = min(0.9, 0.15 + 0.75 * (elapsed / estimated_total))
            try:
                update_state(config, session_id, "running", "transcribe", progress)
            except Exception:
                pass

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def worker_command(args: argparse.Namespace) -> int:
    config = load_config()
    apply_network_env(config)
    session_id = args.session
    path = resolve_session_dir(config, session_id)
    manifest = read_json(path / "manifest.json", {}) or {}
    try:
        update_state(config, session_id, "running", "capture", 0.05)
        captured = capture_video(config, manifest, path)
        update_state(config, session_id, "running", "transcribe", 0.15,
                     extra={"metadata": captured["metadata"]})
        stop_event = threading.Event()
        progress_thread = start_progress_thread(
            config,
            session_id,
            captured["metadata"].get("duration_seconds"),
            stop_event,
        )
        try:
            transcribed = transcribe_audio(config, captured["wav_path"], path)
        finally:
            stop_event.set()
            progress_thread.join(timeout=2)
        update_state(config, session_id, "ready", "done", 1.0,
                     extra={
                         "metadata": captured["metadata"],
                         "transcription": {
                             "backend": transcribed.get("backend"), "language": transcribed["language"],
                             "text_length": transcribed["text_length"],
                             "segment_count": transcribed["segment_count"],
                         },
                         "paths": transcribed["paths"],
                     })
        return 0
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        log_path = path / "worker.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + traceback.format_exc() + "\n")
            handle.flush()
        update_state(config, session_id, "failed", "error", None, error=error)
        return 1


def status_command(args: argparse.Namespace) -> int:
    config = load_config()
    path = resolve_session_dir(config, args.session)
    state = read_state(config, args.session)
    result = {"ok": True, "session_dir": str(path)}
    result.update(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def wait_command(args: argparse.Namespace) -> int:
    config = load_config()
    path = resolve_session_dir(config, args.session)
    deadline = time.time() + float(args.timeout)
    terminal = {"ready", "failed", "duplicate", "committed"}
    while True:
        state = read_state(config, args.session)
        if state.get("state") in terminal:
            result = {"ok": state.get("state") != "failed", "session_dir": str(path)}
            result.update(state)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if state.get("state") != "failed" else 1
        if time.time() >= deadline:
            result = {"ok": True, "session_dir": str(path), "timed_out": True}
            result.update(state)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2
        time.sleep(float(args.interval))


def parse_frontmatter_title(text: str) -> Optional[str]:
    match = re.search(r"^---\s*\n(.*?)\n---", text, re.S | re.M)
    if not match:
        return None
    for line in match.group(1).splitlines():
        if line.strip().startswith("title:"):
            value = line.split(":", 1)[1].strip()
            return value.strip('"').strip("'")
    return None


def sanitize_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', "-", title or "视频笔记")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > 120:
        name = name[:120].rstrip()
    return name or "视频笔记"


def add_updated_field(text: str) -> str:
    if re.search(r"^updated:", text, re.M):
        return text
    match = re.search(r"^(---\s*\n)(.*?)(\n---\s*\n)", text, re.S | re.M)
    if not match:
        return text
    return text[:match.end(2)] + f"\nupdated: {date.today().isoformat()}" + text[match.end(2):]


def validate_note(text: str) -> None:
    if not re.search(r"^#\s+", text, re.M):
        raise CommitError("笔记缺少一级标题")
    if "## 思维导图" not in text:
        raise CommitError("笔记缺少“## 思维导图”")
    if "```mermaid" not in text or "mindmap" not in text:
        raise CommitError("思维导图必须是 Mermaid mindmap")
    if "source_id:" not in text:
        raise CommitError("frontmatter 缺少 source_id")
    for forbidden in ("## 完整转写", "## 原始转写", "## 逐字稿"):
        if forbidden in text:
            raise CommitError(f"笔记不应包含“{forbidden}”")
    if "## 问答整理" in text:
        if not re.search(r"^### Q\d+\.", text, re.M):
            raise CommitError("面试题模式缺少 Q 编号")
        for marker in ("**考察点：**", "**回答框架：**", "**口述回答：**", "**补充标准答案：**"):
            if marker not in text:
                raise CommitError(f"面试题模式缺少 {marker}")
    elif "## 核心要点" not in text:
        raise CommitError("笔记既不是面试题模式，也缺少“## 核心要点”")


def commit_command(args: argparse.Namespace) -> int:
    config = load_config()
    session_id = args.session
    path = resolve_session_dir(config, session_id)
    state = read_state(config, session_id)
    if state.get("state") != "ready":
        raise CommitError(f"session 尚未 ready：{state.get('state')}")
    note_path = Path(args.note).expanduser()
    if not note_path.exists():
        raise CommitError(f"note file not found: {note_path}")
    text = note_path.read_text(encoding="utf-8")
    validate_note(text)
    title = args.title or parse_frontmatter_title(text)
    if not title:
        raise CommitError("无法从 note frontmatter 解析 title，请传 --title")

    manifest = read_json(path / "manifest.json", {}) or {}
    overrides = manifest.get("overrides", {}) or {}
    vault = Path(args.vault or overrides.get("vault") or config["vault_path"]).expanduser()
    subdir = args.subdir or overrides.get("subdir") or config["note_subdir"]
    if not vault.exists():
        raise CommitError(f"vault 路径不存在：{vault}")
    target_dir = vault / subdir
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        target_dir.resolve().relative_to(path.resolve())
        raise CommitError("目标目录位于 session 临时目录内，拒绝写入和清理")
    except ValueError:
        pass

    metadata = state.get("metadata", {}) or {}
    source_id = metadata.get("source_id") or manifest.get("video_id")
    force = bool(args.force or overrides.get("force"))
    existing = find_existing_note(vault, source_id) if source_id else None
    if existing and not force:
        shutil.rmtree(path, ignore_errors=True)
        print(json.dumps({
            "ok": True,
            "state": "duplicate",
            "existing_note": str(existing),
            "message": "这条视频已有笔记，未重复创建。",
        }, ensure_ascii=False, indent=2))
        return 0

    if existing and force:
        target = existing
        text = add_updated_field(text)
    else:
        target = target_dir / (sanitize_filename(title) + ".md")
        if target.exists():
            if source_id:
                target = target_dir / f"{sanitize_filename(title)} - {source_id}.md"
            counter = 2
            base = target
            while target.exists():
                target = base.with_name(f"{base.stem} ({counter}){base.suffix}")
                counter += 1

    atomic_write_text(target, text)
    shutil.rmtree(path, ignore_errors=True)
    print(json.dumps({
        "ok": True,
        "state": "committed",
        "note_path": str(target),
        "title": title,
        "cleanup": not path.exists(),
    }, ensure_ascii=False, indent=2))
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    config = load_config()
    apply_network_env(config)
    warnings: List[str] = []
    plat = current_platform()
    backend = resolve_asr_backend(config)
    uv = find_uv()
    if not uv:
        warnings.append("uv not found; install it first (Windows: winget install --id astral-sh.uv -e)")
    ffmpeg = None
    try:
        ffmpeg = find_ffmpeg()
    except Exception as exc:
        warnings.append(str(exc))
    browser = find_browser(config)
    if browser.get("source") == "none":
        warnings.append("no Chrome / Edge / Chromium found; install one or run playwright install chromium")
    if backend == "mlx" and plat != "macos":
        warnings.append(f"asr_backend=mlx is not supported on {plat}; use asr_backend=faster")
    if backend == "faster" and plat == "macos":
        warnings.append("asr_backend=faster works on macOS but is slower than mlx-whisper")
    vault = Path(config["vault_path"]).expanduser()
    if not vault.exists():
        warnings.append(f"vault path not found: {vault}")

    model_ref = config["model"] if backend == "mlx" else str(config.get("faster_model") or "large-v3")
    if "/" not in model_ref:
        model_ref = f"Systran/faster-whisper-{model_ref}"
    hf_cache = os.environ.get("HF_HUB_CACHE")
    if hf_cache:
        model_root = Path(hf_cache).expanduser()
    else:
        hf_home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface"))).expanduser()
        model_root = hf_home / "hub"
    model_dir = model_root / ("models--" + model_ref.replace("/", "--"))
    model_cached = model_dir.exists() and any(
        item.stat().st_size > 2_000_000_000
        for item in model_dir.rglob("blobs/*")
        if item.is_file()
    )

    network: Dict[str, Any] = {}
    try:
        import requests
        status = requests.get(config["pypi_index"], timeout=8).status_code
        network["pypi_index_status"] = status
        network["pypi_index_reachable"] = status < 500
    except Exception as exc:
        network["pypi_index_error"] = str(exc)
        network["pypi_index_reachable"] = False
    try:
        import requests
        status = requests.get(config["hf_endpoint"], timeout=8).status_code
        network["hf_endpoint_status"] = status
        network["hf_endpoint_reachable"] = status < 500
    except Exception as exc:
        network["hf_endpoint_error"] = str(exc)
        network["hf_endpoint_reachable"] = False

    result = {
        "ok": not warnings,
        "platform": plat,
        "python": sys.version.split()[0],
        "asr_backend": backend,
        "proxy_configured": bool(config.get("proxy_url")),
        "config_path": str(config_path()),
        "vault_path": str(vault),
        "vault_exists": vault.exists(),
        "uv": uv,
        "ffmpeg": ffmpeg,
        "browser": browser,
        "chrome": browser.get("path"),
        "model": config["model"],
        "faster_model": config.get("faster_model"),
        "model_ref": model_ref,
        "model_cached": model_cached,
        "model_dir": str(model_dir),
        "network": network,
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not warnings else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start")
    p.add_argument("--input", required=True)
    p.add_argument("--vault")
    p.add_argument("--subdir")
    p.add_argument("--mode", default="auto", choices=["auto", "interview", "general"])
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=start_command)

    p = sub.add_parser("_worker")
    p.add_argument("--session", required=True)
    p.set_defaults(func=worker_command)

    p = sub.add_parser("status")
    p.add_argument("--session", required=True)
    p.set_defaults(func=status_command)

    p = sub.add_parser("wait")
    p.add_argument("--session", required=True)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--interval", type=float, default=5)
    p.set_defaults(func=wait_command)

    p = sub.add_parser("commit")
    p.add_argument("--session", required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--title")
    p.add_argument("--vault")
    p.add_argument("--subdir")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=commit_command)

    p = sub.add_parser("doctor")
    p.set_defaults(func=doctor_command)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except SkillError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted"}, ensure_ascii=False, indent=2))
        return 130
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
