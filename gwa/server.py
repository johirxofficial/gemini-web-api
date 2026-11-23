"""OpenAI-compatible HTTP API + browser-backed worker.

Endpoints
---------
GET  /                     docs & playground
GET  /health               service/browser status
GET  /v1/models            OpenAI model list
POST /v1/chat/completions  OpenAI chat completions (stream supported)
POST /v1/completions       legacy OpenAI completions
POST /api/ask              simple {"prompt": "..."} endpoint
GET  /openapi.json         small OpenAPI description
"""
from __future__ import annotations

import hmac
import json
import logging
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .browser import (
    ElementMissing,
    LoginRequired,
    activate_cdp_tab,
    cdp_up,
    close_cdp_tab,
    ensure_browser,
    find_gemini_tabs,
    gemini_url_prefix,
    is_gemini_url,
    list_targets,
    open_cdp_tab,
    profile_path,
)
from .cdp import Tab
from .config import ROOT, Config, ensure_dirs
from .gemini import GeminiWeb
from .lock import gemini_lock

log = logging.getLogger(__name__)

MAX_BODY = 2 * 1024 * 1024
DOCS_FILE = ROOT / "web" / "index.html"
_DOCS = {"mtime": 0.0, "html": ""}


class APIError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        type: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type
        self.code = code
        self.param = param

    def body(self) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": self.type,
                "param": self.param,
                "code": self.code,
            }
        }


# ------------------------------------------------------------------- worker


class Worker:
    """Processes prompts one at a time in a single persistent Gemini tab.

    The first job finds an already-open Gemini conversation tab (or opens one
    if none exists). Every later job reuses that same tab, so the API keeps
    one ongoing Gemini chat instead of creating a new chat per request.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.max_queue = max(1, int(cfg.get("api.max_queue", 8)))
        self.jobs: queue.Queue = queue.Queue(maxsize=self.max_queue)
        self.thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._busy = False
        self._started_at = 0.0
        self._last_error = ""
        self._requests = 0
        self._cdp_mode = "not-connected"
        self._cdp_url = ""
        self._stopped = False

        self._reuse_tab = bool(cfg.get("gemini.reuse_tab", True))
        self._gemini_url = str(
            cfg.get("gemini.url", "https://gemini.google.com/app")
        )
        configured_prefix = str(cfg.get("gemini.tab_url_prefix", "") or "").strip()
        self._tab_prefix = configured_prefix or gemini_url_prefix(self._gemini_url)

        self._tab: Tab | None = None
        self._tab_id = ""
        self._tab_url = ""
        self._tab_cdp = ""

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._guard:
            if self.thread and self.thread.is_alive():
                return
            self._stopped = False
            self.thread = threading.Thread(
                target=self._loop, name="gemini-worker", daemon=True
            )
            self.thread.start()

    def stop(self) -> None:
        with self._guard:
            self._stopped = True
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass
        self._release_tab()

    # ---------------------------------------------------------------- submit

    def submit(self, prompt: str, timeout: float) -> Future:
        self.start()
        future: Future = Future()
        try:
            self.jobs.put_nowait((prompt, timeout, future))
        except queue.Full as exc:
            raise APIError(
                429,
                "Request queue is full. Please retry in a moment.",
                type="rate_limit_error",
                code="queue_full",
            ) from exc
        return future

    # ----------------------------------------------------------------- stats

    def stats(self) -> dict:
        prefer = str(self.cfg.get("browser.prefer_cdp", "") or "")
        own = str(self.cfg.get("browser.cdp_url", "http://127.0.0.1:9333"))
        return {
            "mode": self._cdp_mode,
            "cdp_url": self._cdp_url or (prefer or own),
            "prefer_cdp": prefer,
            "prefer_up": bool(cdp_up(prefer)) if prefer else False,
            "own_cdp": own,
            "own_up": bool(cdp_up(own)) if own else False,
            "profile": str(profile_path(self.cfg)),
            "queue_depth": self.jobs.qsize(),
            "queue_max": self.max_queue,
            "busy": self._busy,
            "requests": self._requests,
            "last_error": self._last_error,
            "reuse_tab": self._reuse_tab,
            "tab_connected": bool(self._tab),
            "tab_id": self._tab_id,
            "tab_url": self._tab_url,
        }

    # -------------------------------------------------------------- internals

    def _choose_cdp(self) -> str:
        prefer = str(self.cfg.get("browser.prefer_cdp", "") or "")
        if prefer and cdp_up(prefer):
            self._cdp_mode = "shared"
            self._cdp_url = prefer
            return prefer

        own = str(self.cfg.get("browser.cdp_url", "http://127.0.0.1:9333"))
        if not cdp_up(own):
            if not bool(self.cfg.get("browser.auto_launch", True)):
                raise APIError(
                    503,
                    f"Browser is not running ({own}) and auto_launch is off. "
                    "Run the login command to start the browser.",
                    type="service_unavailable",
                    code="browser_unavailable",
                )
            ensure_browser(
                own,
                engine=str(self.cfg.get("browser.engine", "chrome-beta")),
                binary=str(self.cfg.get("browser.binary", "") or ""),
                profile=str(profile_path(self.cfg)),
                wait_seconds=float(self.cfg.get("browser.connect_timeout", 30)),
                window_size=str(self.cfg.get("browser.window_size", "1440,900")),
            )
        self._cdp_mode = "own"
        self._cdp_url = own
        return own

    def _release_tab(self) -> None:
        """Close the DevTools socket but leave the Chrome tab open."""
        tab, self._tab = self._tab, None
        self._tab_id = ""
        self._tab_url = ""
        self._tab_cdp = ""
        if tab is not None:
            tab.close()

    def _tab_is_current(self, cdp: str) -> bool:
        if not self._tab or not self._tab_id or self._tab_cdp != cdp:
            return False
        target = next(
            (item for item in list_targets(cdp) if item.get("id") == self._tab_id),
            None,
        )
        if target is None:
            return False
        url = str(target.get("url") or "")
        if not is_gemini_url(url, self._tab_prefix):
            return False
        if not self._tab.is_alive():
            return False
        self._tab_url = url
        return True

    def _connect_target(self, cdp: str, target: dict) -> Tab:
        tab_id = str(target.get("id") or "")
        websocket_url = str(target.get("webSocketDebuggerUrl") or "")
        if not tab_id or not websocket_url:
            raise RuntimeError("Chrome tab is missing its DevTools websocket URL.")
        tab = Tab(
            websocket_url,
            timeout=float(self.cfg.get("browser.connect_timeout", 30)),
        )
        activate_cdp_tab(cdp, tab_id)
        self._tab = tab
        self._tab_id = tab_id
        self._tab_url = str(target.get("url") or "")
        self._tab_cdp = cdp
        log.info("Using Gemini tab %s — %s", tab_id, self._tab_url)
        return tab

    def _ensure_tab(self, cdp: str) -> Tab:
        if self._tab_is_current(cdp):
            activate_cdp_tab(cdp, self._tab_id)
            return self._tab  # type: ignore[return-value]

        self._release_tab()
        for target in find_gemini_tabs(cdp, prefix=self._tab_prefix):
            try:
                return self._connect_target(cdp, target)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Could not attach to Gemini tab %s: %s",
                    target.get("id"),
                    exc,
                )

        log.info(
            "No usable Gemini tab found; opening one. It will be reused from now on."
        )
        target = open_cdp_tab(
            cdp,
            self._gemini_url,
            wait_seconds=float(self.cfg.get("gemini.ready_seconds", 20)) * 4 + 30,
        )
        return self._connect_target(cdp, target)

    def _run_fresh(self, cdp: str, prompt: str, timeout: float) -> str:
        """Legacy mode: one request = one freshly opened Gemini tab."""
        target = open_cdp_tab(
            cdp,
            self._gemini_url,
            wait_seconds=float(self.cfg.get("gemini.ready_seconds", 20)) * 4 + 30,
        )
        tab_id = str(target.get("id") or "")
        websocket_url = str(target.get("webSocketDebuggerUrl") or "")
        try:
            tab = Tab(
                websocket_url,
                timeout=float(self.cfg.get("browser.connect_timeout", 30)),
            )
            try:
                gem = GeminiWeb(tab, self.cfg)
                return gem.ask(prompt, timeout=timeout, lock=False)
            finally:
                tab.close()
        finally:
            close_cdp_tab(cdp, tab_id)

    def _run(self, prompt: str, timeout: float) -> str:
        """Run one prompt in the persistent tab (or a fresh tab if disabled)."""
        cdp = self._choose_cdp()
        lock_dir = str(self.cfg.get("api.lock_dir", "") or "")

        with gemini_lock(timeout=timeout + 90, configured_dir=lock_dir):
            if not self._reuse_tab:
                return self._run_fresh(cdp, prompt, timeout)

            tab = self._ensure_tab(cdp)
            gem = GeminiWeb(tab, self.cfg)
            try:
                if bool(self.cfg.get("api.log_prompts", False)):
                    log.info("Prompt: %s", prompt[:500])
                return gem.ask(prompt, timeout=timeout, lock=False)
            except BaseException:
                # Reconnect on the next job. The Chrome tab itself stays open,
                # so the same Gemini conversation is reused.
                self._release_tab()
                raise

    def _loop(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                self.jobs.task_done()
                break
            prompt, timeout, future = job
            if future.set_running_or_notify_cancel() is False:
                self.jobs.task_done()
                continue

            self._busy = True
            self._started_at = time.time()
            self._requests += 1
            try:
                answer = self._run(prompt, timeout)
                future.set_result(answer)
            except BaseException as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Worker error: %s", self._last_error)
                try:
                    future.set_exception(exc)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._busy = False
                self._started_at = 0.0
                self.jobs.task_done()


# ------------------------------------------------------------------ service


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.worker = Worker(cfg)
        self.started = time.time()
        self._rate_guard = threading.Lock()
        self._hits: list[float] = []
        self._requests = 0

    def start(self) -> None:
        self.worker.start()

    def stop(self) -> None:
        self.worker.stop()

    # ------------------------------------------------------------------ auth

    def check_auth(self, authorization: str, api_key_header: str) -> None:
        wanted = str(self.cfg.get("api.api_key", "") or "")
        if not wanted:
            return
        supplied = ""
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied:
            supplied = (api_key_header or "").strip()
        if not supplied or not hmac.compare_digest(supplied, wanted):
            raise APIError(
                401,
                "Invalid API key. Send an Authorization: Bearer <key> header.",
                type="authentication_error",
                code="invalid_api_key",
            )

    # ------------------------------------------------------------ rate limit

    def check_rate(self) -> None:
        limit = int(self.cfg.get("api.rate_limit_per_min", 30) or 0)
        if limit <= 0:
            return
        now = time.time()
        with self._rate_guard:
            self._hits = [hit for hit in self._hits if now - hit < 60]
            if len(self._hits) >= limit:
                retry = max(1, int(60 - (now - self._hits[0])))
                raise APIError(
                    429,
                    f"Rate limit exceeded ({limit}/minute). Retry in {retry} seconds.",
                    type="rate_limit_error",
                    code="rate_limit_exceeded",
                )
            self._hits.append(now)

    # ------------------------------------------------------------------ ask

    def _map_error(self, exc: BaseException) -> APIError:
        if isinstance(exc, APIError):
            return exc
        if isinstance(exc, FutureTimeout):
            return APIError(
                504,
                "The request timed out. Gemini may be slow right now; please retry.",
                type="timeout_error",
                code="request_timeout",
            )
        if isinstance(exc, TimeoutError):
            return APIError(
                504,
                str(exc) or "Gemini did not answer in time.",
                type="timeout_error",
                code="gemini_timeout",
            )
        if isinstance(exc, LoginRequired):
            return APIError(
                503,
                str(exc),
                type="service_unavailable",
                code="gemini_login_required",
            )
        if isinstance(exc, ElementMissing):
            return APIError(
                502,
                str(exc),
                type="api_error",
                code="gemini_ui_changed",
            )
        if isinstance(exc, RuntimeError):
            return APIError(502, str(exc), type="api_error", code="gemini_error")
        return APIError(500, f"Internal error: {exc}", type="api_error", code="internal_error")

    def submit(self, prompt: str, timeout: float | None = None) -> tuple[Future, float]:
        self.check_rate()
        self._requests += 1
        limit = int(self.cfg.get("api.max_prompt_chars", 60000))
        if len(prompt) > limit:
            raise APIError(
                413,
                f"Prompt is too large ({len(prompt)} chars; limit {limit}).",
                code="prompt_too_large",
                param="prompt",
            )
        default_timeout = float(self.cfg.get("api.timeout", 180))
        try:
            seconds = float(timeout if timeout is not None else default_timeout)
        except (TypeError, ValueError):
            seconds = default_timeout
        seconds = max(5.0, min(seconds, 600.0))
        return self.worker.submit(prompt, seconds), seconds

    def answer(self, prompt: str, timeout: float | None = None) -> tuple[str, float]:
        future, seconds = self.submit(prompt, timeout)
        try:
            # The worker may still be queued; allow queue time on top of Gemini.
            return future.result(timeout=seconds + 300), seconds
        except BaseException as exc:
            if not future.done():
                future.cancel()
            raise self._map_error(exc) from exc

    def health(self) -> dict:
        browser = self.worker.stats()
        return {
            "ok": True,
            "service": "gemini-web-api",
            "version": __version__,
            "model": self.cfg.model,
            "uptime_seconds": round(time.time() - self.started, 1),
            "requests": self._requests,
            "browser": browser,
            "limits": {
                "timeout": int(self.cfg.get("api.timeout", 180)),
                "max_queue": int(self.cfg.get("api.max_queue", 8)),
                "rate_limit_per_min": int(
                    self.cfg.get("api.rate_limit_per_min", 30)
                ),
                "max_prompt_chars": int(
                    self.cfg.get("api.max_prompt_chars", 60000)
                ),
            },
        }

    def models(self) -> dict:
        return {
            "object": "list",
            "data": [
                {
                    "id": self.cfg.model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "google-web",
                    "permission": [],
                    "root": self.cfg.model,
                    "parent": None,
                }
            ],
        }

    def openapi(self) -> dict:
        base = f"http://{self.cfg.host}:{self.cfg.port}"
        return {
            "openapi": "3.1.0",
            "info": {
                "title": "Gemini Web API",
                "version": __version__,
                "description": (
                    "OpenAI-compatible local API backed by a logged-in Gemini web "
                    "session. Answers are produced by browser automation."
                ),
            },
            "servers": [{"url": base}],
            "paths": {
                "/v1/chat/completions": {
                    "post": {
                        "summary": "Create a chat completion",
                        "security": [{"bearerAuth": []}],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["messages"],
                                        "properties": {
                                            "model": {"type": "string"},
                                            "messages": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "role": {
                                                            "type": "string",
                                                            "enum": [
                                                                "system",
                                                                "user",
                                                                "assistant",
                                                            ],
                                                        },
                                                        "content": {
                                                            "oneOf": [
                                                                {"type": "string"},
                                                                {
                                                                    "type": "array",
                                                                    "items": {
                                                                        "type": "object"
                                                                    },
                                                                },
                                                            ]
                                                        },
                                                    },
                                                },
                                            },
                                            "stream": {"type": "boolean"},
                                            "timeout": {"type": "number"},
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {"200": {"description": "Completion or SSE stream"}},
                    }
                },
                "/v1/completions": {
                    "post": {
                        "summary": "Create a text completion",
                        "security": [{"bearerAuth": []}],
                        "responses": {"200": {"description": "Completion or SSE stream"}},
                    }
                },
                "/api/ask": {
                    "post": {
                        "summary": "Simple prompt endpoint",
                        "security": [{"bearerAuth": []}],
                        "responses": {"200": {"description": "Answer"}},
                    }
                },
                "/v1/models": {
                    "get": {
                        "summary": "List models",
                        "security": [{"bearerAuth": []}],
                        "responses": {"200": {"description": "Model list"}},
                    }
                },
                "/health": {
                    "get": {
                        "summary": "Health and browser status",
                        "responses": {"200": {"description": "Status"}},
                    }
                },
            },
            "components": {
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer"}
                }
            },
        }


# ------------------------------------------------------------------- prompt


def text_content(content: Any, index: int = 0) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                raise APIError(
                    400,
                    f"messages[{index}].content contains an unknown part.",
                    code="invalid_content",
                    param=f"messages[{index}].content",
                )
            kind = str(part.get("type") or "text")
            if kind in ("text", "input_text"):
                parts.append(str(part.get("text") or ""))
            elif kind in ("image_url", "input_image", "image"):
                raise APIError(
                    400,
                    "Image input is not supported by the Gemini web API. Send text only.",
                    code="image_not_supported",
                    param=f"messages[{index}].content",
                )
            else:
                raise APIError(
                    400,
                    f"Content part type '{kind}' is not supported.",
                    code="invalid_content",
                    param=f"messages[{index}].content",
                )
        return "\n".join(parts)
    raise APIError(
        400,
        f"messages[{index}].content must be a string or an array.",
        code="invalid_content",
        param=f"messages[{index}].content",
    )


def messages_to_prompt(messages: Any) -> str:
    if not isinstance(messages, list) or not messages:
        raise APIError(
            400,
            "`messages` must be a non-empty array.",
            code="invalid_messages",
            param="messages",
        )

    cleaned: list[tuple[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise APIError(
                400,
                f"messages[{index}] must be an object.",
                code="invalid_messages",
                param=f"messages[{index}]",
            )
        role = str(message.get("role") or "user").lower()
        if role not in ("system", "developer", "user", "assistant", "tool"):
            raise APIError(
                400,
                f"Role '{role}' is not supported.",
                code="invalid_role",
                param=f"messages[{index}].role",
            )
        content = text_content(message.get("content"), index).strip()
        if not content:
            continue
        if role == "developer":
            role = "system"
        cleaned.append((role, content))

    if not cleaned:
        raise APIError(400, "All messages are empty.", code="empty_messages", param="messages")
    if not any(role == "user" for role, _ in cleaned):
        raise APIError(
            400,
            "At least one `user` message is required.",
            code="missing_user_message",
            param="messages",
        )

    if len(cleaned) == 1 and cleaned[0][0] == "user":
        return cleaned[0][1]

    label = {
        "system": "System",
        "user": "User",
        "assistant": "Assistant",
        "tool": "Tool result",
    }
    lines = [
        (
            "Continue the conversation below. Use the earlier turns as context and "
            "answer only the final user message. Do not repeat or summarize the "
            "conversation. Reply only with the answer."
        ),
        "",
    ]
    for role, content in cleaned:
        lines.append(f"{label.get(role, role.title())}:")
        lines.append(content)
        lines.append("")
    lines.append("Answer:")
    return "\n".join(lines)


def completion_prompt(prompt: Any) -> str:
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        values = []
        for index, item in enumerate(prompt):
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                if item.get("prompt") is not None:
                    values.append(str(item.get("prompt")))
                elif item.get("text") is not None:
                    values.append(str(item.get("text")))
                else:
                    raise APIError(
                        400,
                        f"prompt[{index}] must be a string or {{prompt: ...}}.",
                        code="invalid_prompt",
                        param="prompt",
                    )
            else:
                raise APIError(
                    400,
                    "`prompt` must be a string or a string array.",
                    code="invalid_prompt",
                    param="prompt",
                )
        return "\n".join(values)
    raise APIError(
        400, "`prompt` must be a string or a string array.", code="invalid_prompt", param="prompt"
    )


def estimate_tokens(text: str) -> int:
    return max(0, (len(text) + 3) // 4)


def split_chunks(text: str, size: int) -> list[str]:
    size = max(1, size)
    return [text[index:index + size] for index in range(0, len(text), size)]


# ------------------------------------------------------------------ handler


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"GeminiWebAPI/{__version__}"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # --------------------------------------------------------------- helpers

    @property
    def service(self) -> Service:
        return self.server.service  # type: ignore[attr-defined]

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, x-api-key",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        if status >= 400 or self.command == "POST":
            log.info("%s %s → %s", self.command, self.path, status)

    def _error(self, exc: APIError) -> None:
        self._json(exc.status, exc.body())

    def _html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _empty(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise APIError(
                413,
                f"Request body is too large (max {MAX_BODY} bytes).",
                code="payload_too_large",
            )
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(
                400,
                f"Invalid JSON: {exc}",
                code="invalid_json",
            ) from exc
        if not isinstance(data, dict):
            raise APIError(400, "JSON body must be an object.", code="invalid_json")
        return data

    def _auth(self) -> None:
        self.service.check_auth(
            self.headers.get("Authorization", ""),
            self.headers.get("x-api-key", ""),
        )

    def _path(self) -> str:
        path = urlparse(self.path).path
        if len(path) > 1:
            path = path.rstrip("/")
        return path

    # ------------------------------------------------------------------ docs

    @staticmethod
    def _docs() -> str:
        if not DOCS_FILE.exists():
            return (
                "<h1>Gemini Web API</h1><p>web/index.html was not found.</p>"
                '<p><a href="/health">/health</a></p>'
            )
        try:
            modified = DOCS_FILE.stat().st_mtime
            if _DOCS["mtime"] != modified:
                _DOCS["html"] = DOCS_FILE.read_text(encoding="utf-8")
                _DOCS["mtime"] = modified
            return _DOCS["html"]
        except Exception:  # noqa: BLE001
            return "<h1>Gemini Web API</h1><p>Could not read the docs.</p>"

    # -------------------------------------------------------------------- GET

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            path = self._path()
            if path in ("/", "/index.html", "/docs"):
                self._html(self._docs())
            elif path in ("/health", "/api/health"):
                self._json(200, self.service.health())
            elif path == "/v1/models":
                self._auth()
                self._json(200, self.service.models())
            elif path == "/openapi.json":
                self._json(200, self.service.openapi())
            elif path == "/favicon.ico":
                self._empty()
            else:
                raise APIError(404, f"No route: {path}", code="not_found")
        except APIError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("GET failed")
            self._error(APIError(500, f"Internal error: {exc}", type="api_error"))

    # ------------------------------------------------------------------- POST

    def do_POST(self) -> None:
        try:
            path = self._path()
            self._auth()
            body = self._read_json()
            if path == "/v1/chat/completions":
                self._chat(body)
            elif path == "/v1/completions":
                self._completion(body)
            elif path == "/api/ask":
                self._ask(body)
            else:
                raise APIError(404, f"No route: {path}", code="not_found")
        except APIError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("POST failed")
            self._error(APIError(500, f"Internal error: {exc}", type="api_error"))

    # --------------------------------------------------------------- endpoints

    def _model(self, body: dict) -> str:
        value = body.get("model")
        return str(value) if value else self.service.cfg.model

    def _timeout(self, body: dict) -> float | None:
        raw = body.get("timeout")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise APIError(
                400, "`timeout` must be a number.", code="invalid_timeout", param="timeout"
            )

    def _chat(self, body: dict) -> None:
        if body.get("n") not in (None, 1):
            raise APIError(
                400, "Only n=1 is supported.", code="unsupported_parameter", param="n"
            )
        prompt = messages_to_prompt(body.get("messages"))
        timeout = self._timeout(body)
        model = self._model(body)
        if self._streaming(body):
            self._stream_chat(prompt, timeout, model)
            return

        started = time.time()
        answer, seconds = self.service.answer(prompt, timeout)
        created = int(time.time())
        prompt_tokens = estimate_tokens(prompt)
        answer_tokens = estimate_tokens(answer)
        self._json(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": created,
                "model": model,
                "system_fingerprint": "fp_gemini_web",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "logprobs": None,
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": answer_tokens,
                    "total_tokens": prompt_tokens + answer_tokens,
                },
                "gwa": {
                    "elapsed_seconds": round(time.time() - started, 2),
                    "timeout_seconds": seconds,
                },
            },
        )

    def _completion(self, body: dict) -> None:
        if body.get("n") not in (None, 1):
            raise APIError(
                400, "Only n=1 is supported.", code="unsupported_parameter", param="n"
            )
        prompt = completion_prompt(body.get("prompt"))
        if not prompt.strip():
            raise APIError(400, "`prompt` is empty.", code="empty_prompt", param="prompt")
        timeout = self._timeout(body)
        model = self._model(body)
        if self._streaming(body):
            self._stream_completion(prompt, timeout, model)
            return

        started = time.time()
        answer, seconds = self.service.answer(prompt, timeout)
        prompt_tokens = estimate_tokens(prompt)
        answer_tokens = estimate_tokens(answer)
        self._json(
            200,
            {
                "id": f"cmpl-{uuid.uuid4().hex}",
                "object": "text_completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "text": answer,
                        "index": 0,
                        "logprobs": None,
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": answer_tokens,
                    "total_tokens": prompt_tokens + answer_tokens,
                },
                "gwa": {
                    "elapsed_seconds": round(time.time() - started, 2),
                    "timeout_seconds": seconds,
                },
            },
        )

    def _ask(self, body: dict) -> None:
        prompt = body.get("prompt")
        if prompt is None and body.get("q") is not None:
            prompt = body.get("q")
        if not isinstance(prompt, str) or not prompt.strip():
            raise APIError(
                400,
                "`prompt` (string) is required.",
                code="empty_prompt",
                param="prompt",
            )
        timeout = self._timeout(body)
        started = time.time()
        answer, seconds = self.service.answer(prompt, timeout)
        self._json(
            200,
            {
                "ok": True,
                "model": self.service.cfg.model,
                "answer": answer,
                "chars": len(answer),
                "elapsed_seconds": round(time.time() - started, 2),
                "timeout_seconds": seconds,
            },
        )

    @staticmethod
    def _streaming(body: dict) -> bool:
        value = body.get("stream")
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)

    # --------------------------------------------------------------- streaming

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        self.close_connection = True
        self._emit(b": gemini-web-api\n\n")
        self._emit(b": Gemini web is working...\n\n")

    def _emit(self, data: bytes) -> None:
        self.wfile.write(data)
        self.wfile.flush()

    def _sse(self, payload: dict) -> None:
        self._emit(
            b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"
        )

    def _sse_error(self, exc: APIError) -> None:
        try:
            self._sse(exc.body())
            self._emit(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_chat(self, prompt: str, timeout: float | None, model: str) -> None:
        try:
            future, seconds = self.service.submit(prompt, timeout)
        except APIError as exc:
            self._error(exc)
            return

        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        self._sse_start()
        base = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "system_fingerprint": "fp_gemini_web",
        }

        def chunk(delta: dict, finish: str | None = None) -> None:
            self._sse(
                {
                    **base,
                    "choices": [
                        {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}
                    ],
                }
            )

        try:
            chunk({"role": "assistant", "content": ""})
            try:
                answer = future.result(timeout=seconds + 300)
            except BaseException as exc:  # noqa: BLE001
                self._sse_error(self.service._map_error(exc))
                return

            size = int(self.service.cfg.get("api.stream_chunk", 28) or 28)
            delay = max(0.0, float(self.service.cfg.get("api.stream_delay_ms", 12) or 0) / 1000)
            for piece in split_chunks(answer, size):
                chunk({"content": piece})
                if delay:
                    time.sleep(delay)
            chunk({}, "stop")
            self._emit(b"data: [DONE]\n\n")
            log.info("POST %s → 200 (stream, %s chars)", self.path, len(answer))
        except (BrokenPipeError, ConnectionResetError):
            log.info("Client closed the stream")
        except Exception as exc:
            log.exception("Streaming failed")
            self._sse_error(APIError(500, f"Stream error: {exc}", type="api_error"))

    def _stream_completion(self, prompt: str, timeout: float | None, model: str) -> None:
        try:
            future, seconds = self.service.submit(prompt, timeout)
        except APIError as exc:
            self._error(exc)
            return

        completion_id = f"cmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        self._sse_start()
        base = {
            "id": completion_id,
            "object": "text_completion",
            "created": created,
            "model": model,
        }

        def chunk(text: str, finish: str | None = None) -> None:
            self._sse(
                {
                    **base,
                    "choices": [
                        {
                            "text": text,
                            "index": 0,
                            "logprobs": None,
                            "finish_reason": finish,
                        }
                    ],
                }
            )

        try:
            chunk("")
            try:
                answer = future.result(timeout=seconds + 300)
            except BaseException as exc:  # noqa: BLE001
                self._sse_error(self.service._map_error(exc))
                return

            size = int(self.service.cfg.get("api.stream_chunk", 28) or 28)
            delay = max(0.0, float(self.service.cfg.get("api.stream_delay_ms", 12) or 0) / 1000)
            for piece in split_chunks(answer, size):
                chunk(piece)
                if delay:
                    time.sleep(delay)
            chunk("", "stop")
            self._emit(b"data: [DONE]\n\n")
            log.info("POST %s → 200 (stream, %s chars)", self.path, len(answer))
        except (BrokenPipeError, ConnectionResetError):
            log.info("Client closed the stream")
        except Exception as exc:
            log.exception("Streaming failed")
            self._sse_error(APIError(500, f"Stream error: {exc}", type="api_error"))


class APIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service: Service):
        super().__init__(address, Handler)
        self.service = service

    def server_close(self) -> None:
        try:
            self.service.stop()
        finally:
            super().server_close()


def build_service(cfg: Config) -> Service:
    ensure_dirs()
    return Service(cfg)
