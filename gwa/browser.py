"""Chrome detection, tab creation and CDP browser launching."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

ENGINES = ("chrome-beta", "chrome", "chromium", "edge", "brave")


class LoginRequired(RuntimeError):
    """The browser profile is not logged in to Gemini."""


class ElementMissing(RuntimeError):
    """A Gemini selector did not match (the web UI probably changed)."""


# --------------------------------------------------------------------- paths


def _candidates(engine: str) -> list[str]:
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", r"%LOCALAPPDATA%")
        program = os.environ.get("ProgramFiles", r"C:\Program Files")
        program86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        table = {
            "chrome-beta": [
                rf"{program}\Google\Chrome Beta\Application\chrome.exe",
                rf"{program86}\Google\Chrome Beta\Application\chrome.exe",
                rf"{local}\Google\Chrome Beta\Application\chrome.exe",
            ],
            "chrome": [
                rf"{program}\Google\Chrome\Application\chrome.exe",
                rf"{program86}\Google\Chrome\Application\chrome.exe",
                rf"{local}\Google\Chrome\Application\chrome.exe",
            ],
            "chromium": [rf"{local}\Chromium\Application\chrome.exe"],
            "edge": [
                rf"{program}\Microsoft\Edge\Application\msedge.exe",
                rf"{program86}\Microsoft\Edge\Application\msedge.exe",
            ],
            "brave": [
                rf"{program}\BraveSoftware\Brave-Browser\Application\brave.exe",
                rf"{local}\BraveSoftware\Brave-Browser\Application\brave.exe",
            ],
        }
    elif IS_MAC:
        table = {
            "chrome-beta": [
                "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
            ],
            "chrome": [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ],
            "chromium": [
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ],
            "edge": [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ],
            "brave": [
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            ],
        }
    else:
        table = {
            "chrome-beta": ["google-chrome-beta"],
            "chrome": ["google-chrome", "google-chrome-stable"],
            "chromium": ["chromium", "chromium-browser"],
            "edge": ["microsoft-edge", "microsoft-edge-stable"],
            "brave": ["brave-browser", "brave"],
        }
    return table.get(engine, [])


def resolve_binary(engine: str, configured: str | None = None) -> str | None:
    value = (configured or "").strip()
    if value and value.lower() not in ("auto", "default"):
        path = Path(os.path.expandvars(os.path.expanduser(value)))
        return str(path) if path.exists() else None

    order = [engine] + [e for e in ENGINES if e != engine]
    for candidate_engine in order:
        for candidate in _candidates(candidate_engine):
            path = Path(os.path.expandvars(candidate))
            if path.exists():
                return str(path)
            found = shutil.which(candidate)
            if found:
                return found
    return None


def default_profile_dir(engine: str = "chrome-beta") -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return base / "gemini-web-api" / f"{engine}-profile"
    if IS_MAC:
        base = Path.home() / "Library" / "Application Support"
        return base / "gemini-web-api" / f"{engine}-profile"
    return Path.home() / ".gemini-web-api" / f"{engine}-profile"


def profile_path(cfg, engine: str | None = None) -> Path:
    engine = (engine or str(cfg.get("browser.engine", "chrome-beta"))).lower()
    value = str(cfg.get("browser.profile_dir") or "").strip()
    if value:
        return Path(os.path.expandvars(os.path.expanduser(value)))
    return default_profile_dir(engine)


def cdp_port(cdp_url: str) -> int:
    return int(urllib.parse.urlparse(cdp_url).port or 9222)


def cdp_up(cdp_url: str, timeout: float = 1.5) -> dict | None:
    if not cdp_url:
        return None
    try:
        with urllib.request.urlopen(
            f"{cdp_url.rstrip('/')}/json/version", timeout=timeout
        ) as response:
            return json.loads(response.read())
    except Exception:  # noqa: BLE001
        return None


def _base(cdp_url: str) -> str:
    return cdp_url.rstrip("/")


def _cdp_json(url: str, method: str = "GET", timeout: float = 10.0) -> dict:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def list_targets(cdp_url: str, timeout: float = 5.0) -> list[dict]:
    try:
        data = _cdp_json(f"{_base(cdp_url)}/json/list", timeout=timeout)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def gemini_url_prefix(url: str) -> str:
    """Return ``scheme://host`` for a Gemini URL (no trailing slash)."""
    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://gemini.google.com"


def is_gemini_url(url: str, prefix: str = "https://gemini.google.com") -> bool:
    value = (url or "").strip()
    base = (prefix or "").strip().rstrip("/")
    if not value or not base:
        return False
    return value == base or value.startswith(base + "/")


def find_gemini_tab(
    cdp_url: str,
    prefix: str = "https://gemini.google.com",
    timeout: float = 5.0,
) -> dict | None:
    """Find an already-open Gemini page tab.

    A conversation URL (``/app/<id>``) is preferred over a blank ``/app`` tab,
    so the API continues in the user's existing chat instead of starting a new
    one whenever the browser is already open.
    """
    candidates = [
        item
        for item in list_targets(cdp_url, timeout=timeout)
        if item.get("type") == "page"
        and is_gemini_url(str(item.get("url") or ""), prefix)
    ]
    if not candidates:
        return None

    def score(item: dict) -> tuple[int, int]:
        url = str(item.get("url") or "")
        path = urllib.parse.urlparse(url).path.rstrip("/")
        conversation = bool(re.search(r"/app/[A-Za-z0-9_-]{8,}", path))
        blank_app = path in ("", "/app")
        return (0 if conversation else 1, 0 if blank_app else 1)

    candidates.sort(key=score)
    return candidates[0]


def activate_cdp_tab(cdp_url: str, tab_id: str) -> None:
    """Make the tab the visible/foreground tab.

    Background tabs can be throttled by Chrome; activating a freshly created
    Gemini tab makes its composer initialize faster and more reliably.
    """
    if not tab_id:
        return
    try:
        urllib.request.urlopen(
            f"{_base(cdp_url)}/json/activate/{tab_id}", timeout=5
        ).read()
    except Exception:  # noqa: BLE001
        pass


def open_cdp_tab(
    cdp_url: str,
    url: str,
    wait_seconds: float = 60.0,
    settle_seconds: float = 1.0,
    activate: bool = True,
) -> dict:
    """Open a real Chrome tab via /json/new and wait until its title is ready.

    Chrome's own /json/new endpoint is used instead of a Playwright-created
    page. The returned target includes a page-level ``webSocketDebuggerUrl``
    that :class:`gwa.cdp.Tab` can drive directly.
    """
    query = urllib.parse.quote(url, safe=":/?=&%")
    info = _cdp_json(f"{_base(cdp_url)}/json/new?{query}", method="PUT", timeout=15)
    tab_id = str(info.get("id") or "")
    if not tab_id:
        raise RuntimeError("Chrome could not open a new tab.")

    deadline = time.time() + wait_seconds
    current = info
    stable = 0
    while time.time() < deadline:
        targets = list_targets(cdp_url)
        found = next((item for item in targets if item.get("id") == tab_id), None)
        if found:
            current = found
            page_url = str(found.get("url") or "")
            title = str(found.get("title") or "")
            if page_url not in ("", "about:blank") and title not in ("", "about:blank"):
                stable += 1
                if stable >= 2:
                    if activate:
                        activate_cdp_tab(cdp_url, tab_id)
                    time.sleep(max(0.0, settle_seconds))
                    return found
            else:
                stable = 0
        time.sleep(0.6)

    # A slow page may still be usable; let the caller's ready-check decide.
    if current.get("title") in ("", "about:blank"):
        raise TimeoutError(
            f"The new tab did not load ({wait_seconds:.0f}s): {url}. "
            "Open Gemini in Chrome manually and try again."
        )
    if activate:
        activate_cdp_tab(cdp_url, tab_id)
    return current


def close_cdp_tab(cdp_url: str, tab_id: str) -> None:
    if not tab_id:
        return
    try:
        urllib.request.urlopen(
            f"{_base(cdp_url)}/json/close/{tab_id}", timeout=5
        ).read()
    except Exception:  # noqa: BLE001
        pass


def ensure_browser(
    cdp_url: str,
    engine: str = "chrome-beta",
    binary: str | None = None,
    profile: str | None = None,
    wait_seconds: float = 30.0,
    window_size: str = "1440,900",
) -> str:
    """Start a Chrome-family browser with CDP if it isn't already running."""
    info = cdp_up(cdp_url)
    if info:
        return "already-running"

    exe = resolve_binary(engine, binary)
    if not exe:
        raise RuntimeError(
            f"{engine} was not found. Install Chrome or set "
            "browser.binary in config.yaml to the full browser executable path."
        )

    prof = Path(profile) if profile else default_profile_dir(engine)
    prof.mkdir(parents=True, exist_ok=True)

    args = [
        exe,
        f"--remote-debugging-port={cdp_port(cdp_url)}",
        f"--user-data-dir={prof}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        f"--window-size={window_size}",
    ]

    if not IS_WINDOWS and not IS_MAC and not os.environ.get("DISPLAY"):
        if shutil.which("xvfb-run"):
            args = ["xvfb-run", "-a", "--server-args=-screen 0 1440x900x24"] + args
        else:
            args += ["--headless=new", "--disable-gpu"]

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    log.info("Starting browser: %s", exe)
    subprocess.Popen(args, close_fds=True, **kwargs)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if cdp_up(cdp_url):
            log.info("Browser ready — %s", cdp_url)
            return "launched"
        time.sleep(0.5)

    raise RuntimeError(
        f"A browser is starting with profile {prof} but debug port "
        f"({cdp_port(cdp_url)}) is not opening. Close that browser window and try again."
    )

