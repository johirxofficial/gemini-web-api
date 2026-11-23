"""Cross-process Gemini lock.

fb-post-agent and this API may use the same logged-in Chrome/Gemini account.
If so, two prompts at the same time would make a process read the other one's
answer. The lock file is therefore deliberately compatible with
fb-post-agent's ``~/.fb-agent/locks/gemini.lock``.

When this project runs completely alone, callers can set
``GWA_LOCK_DIR`` / ``api.lock_dir`` to any directory.
"""
from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


def lock_dir(configured: str = "") -> Path:
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    env = os.environ.get("GWA_LOCK_DIR")
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    shared = Path.home() / ".fb-agent" / "locks"
    if shared.exists():
        return shared
    return Path.home() / ".gemini-web-api" / "locks"


def _path(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.lock"


def _open(folder: Path, name: str):
    fh = open(_path(folder, name), "a+b")
    fh.seek(0)
    if fh.read(1) == b"":
        fh.seek(0)
        fh.write(b"\0")
        fh.flush()
    fh.seek(0)
    return fh


def _acquire(fh) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return True  # unknown platform: best effort


def _unlock(fh) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def gemini_lock(timeout: float = 240.0, configured_dir: str = ""):
    """Serialize Gemini prompts across all local agent processes."""
    folder = lock_dir(configured_dir)
    fh = _open(folder, "gemini")
    deadline = time.time() + timeout
    while not _acquire(fh):
        if time.time() > deadline:
            fh.close()
            raise TimeoutError(
                "Gemini is busy with another job. Please try again shortly."
            )
        time.sleep(0.5)
    try:
        yield
    finally:
        _unlock(fh)
        fh.close()
