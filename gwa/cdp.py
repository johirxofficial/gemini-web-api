"""Minimal Chrome DevTools Protocol client for one tab.

Gemini's SPA takes several seconds to replace its loading screen with the
composer. Playwright's browser-level ``connect_over_cdp`` can hang while that
is happening, but a direct websocket to the *page* target is responsive.
This module keeps that direct connection intentionally small.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import websocket


class CDPError(RuntimeError):
    """A DevTools command returned an error."""


class Tab:
    def __init__(self, websocket_url: str, timeout: float = 20.0):
        if not websocket_url:
            raise RuntimeError("Could not find the tab's DevTools websocket URL.")
        self.websocket_url = websocket_url
        self.timeout = float(timeout)
        self._id = 0
        self._ws = websocket.create_connection(
            websocket_url,
            timeout=self.timeout,
            enable_multithread=True,
            suppress_origin=True,
        )

    # ------------------------------------------------------------- transport

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict:
        self._id += 1
        message_id = self._id
        self._ws.send(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )

        deadline = time.time() + float(timeout if timeout is not None else self.timeout)
        while time.time() < deadline:
            self._ws.settimeout(max(0.1, deadline - time.time()))
            raw = self._ws.recv()
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("id") != message_id:
                continue  # event or a response belonging to another command
            if "error" in data:
                detail = data["error"]
                raise CDPError(str(detail.get("message") or detail))
            return data.get("result") or {}
        raise TimeoutError(f"DevTools command timed out: {method}")

    def eval(self, expression: str, timeout: float | None = None):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = detail.get("text") or "JavaScript error"
            raise CDPError(str(text))
        return (result.get("result") or {}).get("value")

    # ---------------------------------------------------------------- actions

    def insert_text(self, text: str) -> None:
        self.call("Input.insertText", {"text": text})

    def is_alive(self) -> bool:
        """Cheap check that the page-level DevTools socket still works."""
        try:
            return self.eval("1", timeout=5) == 1
        except Exception:  # noqa: BLE001
            return False

    def press_enter(self) -> None:
        base = {
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        }
        self.call("Input.dispatchKeyEvent", {"type": "keyDown", **base})
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def click_send(self) -> bool:
        return bool(
            self.eval(
                """(() => {
                  const b = [...document.querySelectorAll('button')].find(x => {
                    if (x.disabled) return false;
                    const label = `${x.getAttribute('aria-label') || ''} ${x.innerText || ''}`;
                    return /send message|send|পাঠান/i.test(label);
                  });
                  if (!b) return false;
                  b.click();
                  return true;
                })()"""
            )
        )

    def focus_composer(self, selectors: list[str]) -> bool:
        payload = json.dumps(selectors)
        return bool(
            self.eval(
                f"""(() => {{
                  const selectors = {payload};
                  const seen = new Set();
                  const nodes = [];
                  for (const selector of selectors) {{
                    for (const node of document.querySelectorAll(selector)) {{
                      if (!seen.has(node)) {{ seen.add(node); nodes.push(node); }}
                    }}
                  }}
                  const editor = nodes.find(x => {{
                    if (x.classList && x.classList.contains('ql-clipboard')) return false;
                    if (x.getAttribute && x.getAttribute('aria-hidden') === 'true') return false;
                    return x.offsetWidth || x.offsetHeight || x.getClientRects().length;
                  }});
                  if (!editor) return false;
                  editor.scrollIntoView({{block: 'center'}});
                  editor.focus();
                  try {{
                    const range = document.createRange();
                    range.selectNodeContents(editor);
                    range.collapse(false);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                  }} catch (_) {{}}
                  return true;
                }})()"""
            )
        )

    def clear_composer(self, selectors: list[str]) -> bool:
        """Remove any leftover draft text before inserting a new prompt."""
        if not self.focus_composer(selectors):
            return False
        try:
            return bool(
                self.eval(
                    """(() => {
                      const el = document.activeElement;
                      if (!el || !el.isContentEditable) return false;
                      const range = document.createRange();
                      range.selectNodeContents(el);
                      const selection = window.getSelection();
                      selection.removeAllRanges();
                      selection.addRange(range);
                      document.execCommand('delete', false, null);
                      return true;
                    })()"""
                )
            )
        except Exception:  # noqa: BLE001
            return False

    def dismiss_dialogs(self) -> None:
        self.eval(
            """(() => {
              const labels = new Set(['Got it', 'I agree', 'Accept all', 'No thanks',
                'Not now', 'বুঝেছি', 'সম্মত', 'এখন নয়']);
              for (const button of document.querySelectorAll('button')) {
                if (button.offsetWidth || button.offsetHeight) {
                  const text = (button.innerText || '').trim();
                  if (labels.has(text)) { button.click(); return true; }
                }
              }
              return false;
            })()"""
        )

    def screenshot(self, path: Path) -> bool:
        try:
            result = self.call("Page.captureScreenshot", {"format": "png"}, timeout=10)
            data = result.get("data") or ""
            if not data:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(data))
            return True
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass
