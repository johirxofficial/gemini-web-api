"""Command line entry points: serve / login / doctor / ask."""
from __future__ import annotations

import argparse
import logging
import platform
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

from . import __version__
from .browser import (
    LoginRequired,
    activate_cdp_tab,
    cdp_up,
    find_gemini_tab,
    gemini_url_prefix,
    list_targets,
    open_cdp_tab,
    profile_path,
    resolve_binary,
)
from .cdp import Tab
from .config import LOGS, ROOT, ensure_dirs, load
from .gemini import GeminiWeb
from .lock import lock_dir

log = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    ensure_dirs()
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)

    file_handler = RotatingFileHandler(
        LOGS / "api.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file_handler)


def _banner(cfg) -> str:
    host = cfg.host
    port = cfg.port
    docs = f"http://{host}:{port}/"
    prefer = str(cfg.get("browser.prefer_cdp", "") or "")
    own = str(cfg.get("browser.cdp_url", "") or "")
    prefer_state = "up" if prefer and cdp_up(prefer) else "down"
    own_state = "up" if own and cdp_up(own) else "down"
    return "\n".join(
        [
            "",
            "  Gemini Web API  v" + __version__,
            "  ─────────────────────────────────────────────────────",
            f"  Docs      {docs}",
            f"  Health    {docs}health",
            f"  API       {docs}v1/chat/completions",
            f"  Model     {cfg.model}",
            f"  Browser   shared {prefer or '-'} ({prefer_state}) | own {own or '-'} ({own_state})",
            f"  Config    {cfg.path}",
            "  ─────────────────────────────────────────────────────",
            "  Press Ctrl+C to stop",
            "",
        ]
    )


def cmd_serve(args, cfg) -> int:
    if getattr(args, "host", None):
        cfg.set("api.host", args.host)
    if getattr(args, "port", None):
        cfg.set("api.port", int(args.port))
    setup_logging(getattr(args, "verbose", False))

    from .server import APIServer, Service

    service = Service(cfg)
    try:
        httpd = APIServer((cfg.host, cfg.port), service)
    except OSError as exc:
        print(f"Could not open port {cfg.port}: {exc}")
        print("Try another port: api.cmd --port 8788  (Linux/macOS: ./api.sh --port 8788)")
        return 1

    service.start()
    print(_banner(cfg), flush=True)
    if not getattr(args, "no_open", False):
        url = f"http://{cfg.host}:{cfg.port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nShutting down…", flush=True)
    finally:
        httpd.server_close()
    return 0


def cmd_login(args, cfg) -> int:
    setup_logging(getattr(args, "verbose", False))
    from .server import Worker

    try:
        worker = Worker(cfg)
        cdp = worker._choose_cdp()  # type: ignore[attr-defined]
        gemini_url = str(cfg.get("gemini.url", "https://gemini.google.com/app"))
        prefix = str(cfg.get("gemini.tab_url_prefix", "") or "").strip()
        prefix = prefix or gemini_url_prefix(gemini_url)

        target = find_gemini_tab(cdp, prefix=prefix)
        reused = target is not None
        if target is None:
            target = open_cdp_tab(cdp, gemini_url, wait_seconds=60)
        tab_id = str(target.get("id") or "")
        activate_cdp_tab(cdp, tab_id)

        print(f"Browser : {cdp}")
        print(f"Profile : {profile_path(cfg)}")
        if reused:
            print(f"Using existing Gemini tab: {target.get('url')}")
        else:
            print("Opened a Gemini tab. It will be reused for every API request.")
        print("Sign in with your Google account in that tab if needed.")
        print("When you are done, return here and press Enter.\n")

        try:
            input("Login complete? Press Enter… ")
        except EOFError:
            print("(non-interactive — waiting 30 seconds)")
            time.sleep(30)

        current = next(
            (item for item in list_targets(cdp) if item.get("id") == tab_id),
            target,
        )
        websocket_url = str(current.get("webSocketDebuggerUrl") or "")
        tab = Tab(websocket_url, timeout=30)
        try:
            GeminiWeb(tab, cfg).wait_ready(timeout=35)
        finally:
            tab.close()

        print("\n✅ Login looks good — the Gemini composer is available.")
        print("Start the API with `api.cmd` on Windows or `./api.sh` on Linux/macOS.")
        return 0
    except LoginRequired as exc:
        print(f"\n⚠️ {exc}")
        print("Finish signing in to Gemini in Chrome, then run the login command again.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ Login setup failed: {exc}")
        return 1


def cmd_doctor(args, cfg) -> int:
    setup_logging(getattr(args, "verbose", False))
    engine = str(cfg.get("browser.engine", "chrome-beta"))
    binary = resolve_binary(engine, str(cfg.get("browser.binary", "") or ""))
    prefer = str(cfg.get("browser.prefer_cdp", "") or "")
    own = str(cfg.get("browser.cdp_url", "http://127.0.0.1:9333"))
    prefer_info = cdp_up(prefer) if prefer else None
    own_info = cdp_up(own) if own else None
    key = cfg.api_key
    masked = (key[:3] + "…" + key[-2:]) if len(key) > 6 else ("set" if key else "none")

    print("Gemini Web API — doctor")
    print("-" * 58)
    print(f"Version        : {__version__}")
    print(f"Python         : {sys.version.split()[0]}")
    print(f"Platform       : {platform.platform()}")
    print(f"Project        : {ROOT}")
    print(f"Config         : {cfg.path}")
    print(f"Model          : {cfg.model}")
    print(f"API address    : http://{cfg.host}:{cfg.port}")
    print(f"API key        : {masked}")
    print(f"Timeout        : {cfg.timeout}s")
    print(f"Engine         : {engine}")
    print(f"Binary         : {binary or 'NOT FOUND'}")
    print(f"Profile        : {profile_path(cfg)}")
    print(f"Lock dir       : {lock_dir(str(cfg.get('api.lock_dir', '') or ''))}")
    print(f"Shared CDP     : {prefer or '-'}  →  {'UP' if prefer_info else 'down'}")
    print(f"Own CDP        : {own or '-'}  →  {'UP' if own_info else 'down'}")
    if prefer_info:
        print(f"  shared build : {prefer_info.get('Browser', '?')}")
    if own_info:
        print(f"  own build    : {own_info.get('Browser', '?')}")

    active_cdp = prefer if prefer_info else (own if own_info else "")
    gemini_tab = None
    if active_cdp:
        prefix = str(cfg.get("gemini.tab_url_prefix", "") or "").strip()
        prefix = prefix or gemini_url_prefix(
            str(cfg.get("gemini.url", "https://gemini.google.com/app"))
        )
        gemini_tab = find_gemini_tab(active_cdp, prefix=prefix)
    print(f"Gemini tab     : {gemini_tab.get('url') if gemini_tab else 'none (will open one)'}")
    print(f"Reuse tab      : {'yes' if bool(cfg.get('gemini.reuse_tab', True)) else 'no'}")

    print(f"Docs file      : {'OK' if (ROOT / 'web' / 'index.html').exists() else 'MISSING'}")
    print("-" * 58)
    if not binary:
        print("⚠️  No Chrome-family browser found. Set browser.binary in config.yaml.")
    if not prefer_info and not own_info:
        print("⚠️  No CDP browser is running. Run the login command first.")
    elif prefer_info:
        print("✅ Shared browser is running. The API will use it first.")
    else:
        print("✅ Dedicated browser is running.")
    print("Verify login with /health or `ask.cmd \"hello\"` (Linux/macOS: ./ask.sh \"hello\").")
    return 0


def cmd_ask(args, cfg) -> int:
    setup_logging(getattr(args, "verbose", False))
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print('Prompt is empty. Try: ask.cmd "your question" (Linux/macOS: ./ask.sh "your question")')
        return 2
    from .server import Service

    service = Service(cfg)
    service.start()
    started = time.time()
    try:
        answer, _ = service.answer(prompt)
        print("\n" + answer)
        print(f"\n— {time.time() - started:.1f}s")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ {exc}")
        return 1
    finally:
        service.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gwa",
        description="Gemini Web API — OpenAI-compatible local API",
    )
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug log")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the API server (default)")
    serve.add_argument("--host", help="127.0.0.1")
    serve.add_argument("--port", type=int, help="8787")
    serve.add_argument("--no-open", action="store_true", help="do not open the docs in a browser")
    serve.set_defaults(func=cmd_serve, no_open=False, host=None, port=None)

    login = sub.add_parser("login", help="open a browser tab and check Gemini login")
    login.set_defaults(func=cmd_login)

    doctor = sub.add_parser("doctor", help="check browser, config, and Gemini tab")
    doctor.set_defaults(func=cmd_doctor)

    ask = sub.add_parser("ask", help="send one prompt")
    ask.add_argument("prompt", nargs="+", help="question / prompt")
    ask.set_defaults(func=cmd_ask)

    # No subcommand → serve.
    parser.set_defaults(func=cmd_serve, no_open=False, host=None, port=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"Config load failed: {exc}")
        return 2

    func = getattr(args, "func", cmd_serve)
    try:
        return int(func(args, cfg) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger(__name__).exception("Fatal error")
        print(f"\n❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
