"""Ask Gemini through the logged-in Gemini web UI using a direct CDP tab.

The tab is opened once and then reused for every request. Prompts are sent
into the conversation that is already visible in that tab, so the API keeps a
single ongoing Gemini chat instead of creating a fresh chat each time.

Gemini's SPA shows a placeholder for a few seconds before the real composer
appears; we wait for that composer, submit the prompt, then wait for a stable
answer.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .browser import ElementMissing, LoginRequired
from .cdp import CDPError, Tab
from .config import SCREENSHOTS
from .lock import gemini_lock

log = logging.getLogger(__name__)

DEFAULT_PROMPT_SELECTORS = [
    'rich-textarea div[contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
    'div.ql-editor[contenteditable="true"]',
]

UI_ERRORS = (
    "something went wrong",
    "an error occurred while generating",
    "failed to generate",
    "try again later",
    "i'm not able to help with that",
)


def clean_answer(text: str) -> str:
    value = (text or "").strip()
    for prefix in ("Gemini said", "Gemini বলেছে", "Gemini"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip(" :\n\u200b")
            break
    return value.strip()


class GeminiWeb:
    def __init__(self, tab: Tab, cfg):
        self.tab = tab
        self.cfg = cfg
        self.url = str(cfg.get("gemini.url", "https://gemini.google.com/app"))
        raw_selectors = cfg.get("gemini.prompt_selectors") or DEFAULT_PROMPT_SELECTORS
        if isinstance(raw_selectors, str):
            raw_selectors = [raw_selectors]
        self.prompt_selectors = [str(item) for item in raw_selectors]
        self.response_selector = str(
            cfg.get("gemini.response_selector", "message-content")
        )
        self.ready_seconds = max(
            12.0, float(cfg.get("gemini.ready_seconds", 20))
        )
        self.clear_composer = bool(cfg.get("gemini.clear_composer", True))
        self.idle_seconds = max(
            10.0, float(cfg.get("gemini.idle_seconds", 120))
        )
        self.lock_dir = str(cfg.get("api.lock_dir", "") or "")

    # --------------------------------------------------------------- state

    def _state(self) -> dict:
        selectors = json.dumps(self.prompt_selectors)
        response_selector = json.dumps(self.response_selector)
        raw = self.tab.eval(
            f"""(() => {{
              const selectors = {selectors};
              const responseSelector = {response_selector};
              const seen = new Set();
              const editors = [];
              for (const selector of selectors) {{
                for (const node of document.querySelectorAll(selector)) {{
                  if (!seen.has(node)) {{ seen.add(node); editors.push(node); }}
                }}
              }}
              const visible = editors.filter(
                x => x.offsetWidth || x.offsetHeight || x.getClientRects().length
              );
              const mcs = [...document.querySelectorAll(responseSelector)];
              const stop = [...document.querySelectorAll('button')].some(b => {{
                const label = `${{b.getAttribute('aria-label') || ''}} ${{b.innerText || ''}}`;
                return /stop response|stop generating|থামান/i.test(label);
              }});
              return JSON.stringify({{
                url: location.href,
                title: document.title,
                body: (document.body ? document.body.innerText : '').slice(0, 2500),
                editors: visible.map(e => ({{
                  cls: e.className || '',
                  text: (e.innerText || '').slice(0, 300),
                }})),
                editor_text: visible.length ? (visible[0].innerText || '') : '',
                mc: mcs.length,
                last: mcs.length ? (mcs[mcs.length - 1].innerText || '') : '',
                stop: stop,
              }});
            }})()"""
        )
        if not isinstance(raw, str):
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    # ---------------------------------------------------------------- ready

    def wait_ready(self, timeout: float | None = None) -> dict:
        started = time.time()
        limit = float(timeout if timeout is not None else self.ready_seconds)
        deadline = started + limit
        dialogs_done = False
        state: dict = {}

        while time.time() < deadline:
            state = self._state()
            if state.get("editors"):
                return state
            if not dialogs_done and time.time() - started > 2.5:
                try:
                    self.tab.dismiss_dialogs()
                except Exception:  # noqa: BLE001
                    pass
                dialogs_done = True
            time.sleep(0.6)

        state = self._state()
        url = str(state.get("url") or "")
        body = str(state.get("body") or "").lower()
        if (
            "accounts.google.com" in url
            or "signin" in url
            or "sign in to save activity" in body
            or ("sign in" in body and "ask gemini" in body)
        ):
            self._shot("gemini-login")
            raise LoginRequired(
                "Gemini is not logged in. Open Gemini in Chrome and sign in once "
                "(or run the login command), then try again."
            )

        self._shot("gemini-no-box")
        detail = str(state.get("body") or "").replace("\n", " ")[:180]
        raise ElementMissing(
            "Could not find Gemini's prompt box (the UI changed or the page did not load). "
            f"Current page: {detail or 'empty'}. "
            "Check logs/screenshots/ or update gemini.prompt_selectors in config.yaml."
        )

    # ---------------------------------------------------------------- submit

    def _wait_idle(self, state: dict | None = None, timeout: float | None = None) -> dict:
        """Wait until Gemini is not generating a previous answer."""
        limit = self.idle_seconds if timeout is None else max(1.0, float(timeout))
        deadline = time.time() + limit
        current = state or self._state()
        while current.get("stop") and time.time() < deadline:
            time.sleep(0.7)
            try:
                current = self._state()
            except Exception:  # noqa: BLE001
                break
        if current.get("stop"):
            raise TimeoutError(
                "Gemini is still generating a previous answer. "
                f"It did not finish within {int(limit)} seconds; try again shortly."
            )
        return current

    def _composer_has_text(self, prompt: str) -> bool:
        try:
            state = self._state()
            text = str(state.get("editor_text") or "").strip()
            if not text:
                return False
            needle = prompt.strip().splitlines()[0][:40]
            return needle in text
        except Exception:  # noqa: BLE001
            return False

    def _submit(self, prompt: str, before: int) -> bool:
        if self.clear_composer:
            self.tab.clear_composer(self.prompt_selectors)
            time.sleep(0.15)

        if not self.tab.focus_composer(self.prompt_selectors):
            raise ElementMissing("Could not focus the Gemini composer.")

        self.tab.insert_text(prompt)
        time.sleep(0.45)

        if not self._composer_has_text(prompt):
            # A few editor builds ignore Input.insertText until a real input
            # event is dispatched. execCommand still produces that event.
            self.tab.eval(
                "(() => { const t = "
                + json.dumps(prompt)
                + "; if (document.activeElement) { document.execCommand('insertText', false, t); return true; } return false; })()"
            )
            time.sleep(0.3)

        clicked = self.tab.click_send()
        if not clicked:
            self.tab.press_enter()
        log.info("Sent Gemini prompt (%s chars)", len(prompt))

        # Give the SPA a moment to move the composer text into the transcript.
        time.sleep(0.8)
        state = self._state()
        return int(state.get("mc") or 0) > before or clicked

    # ---------------------------------------------------------------- answer

    def _verify(self, text: str) -> str:
        value = clean_answer(text)
        if not value:
            raise RuntimeError("Gemini returned an empty answer.")
        lowered = value.lower()
        if any(marker in lowered for marker in UI_ERRORS) and len(value) < 400:
            self._shot("gemini-ui-error")
            raise RuntimeError(f"Gemini UI error: {value[:180]}")
        return value

    def _wait_answer(
        self,
        before: int,
        timeout: float,
        prompt: str,
        before_text: str = "",
    ) -> str:
        deadline = time.time() + timeout
        started = False
        last = ""
        best = ""
        stable = 0
        state: dict = {}
        baseline = clean_answer(before_text)

        while time.time() < deadline:
            try:
                state = self._state()
            except (CDPError, TimeoutError, RuntimeError):
                state = {}
            count = int(state.get("mc") or 0)
            text = clean_answer(str(state.get("last") or ""))

            if (
                not started
                and text
                and text.strip() != prompt.strip()
                and (count > before or text != baseline)
            ):
                # A reused conversation already has older answers; a new one is
                # either an extra message-content node or different last text.
                started = True

            if started and text:
                if text.strip() == prompt.strip():
                    # Some builds also render the user turn as message-content.
                    time.sleep(0.4)
                    continue
                best = text
                if text == last:
                    stable += 1
                else:
                    stable = 0
                    last = text
                if stable >= 3 and not state.get("stop"):
                    return self._verify(text)

            time.sleep(0.5)

        if best and not state.get("stop"):
            log.warning("Gemini answer did not finish in time; returning partial text")
            return self._verify(best)

        self._shot("gemini-timeout")
        snippet = str(state.get("body") or "").replace("\n", " ")[-220:]
        raise TimeoutError(
            f"Gemini did not answer within {int(timeout)} seconds. "
            f"Last part of the page: {snippet or 'empty'}"
        )

    def _shot(self, name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name)[:60]
        path = SCREENSHOTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}.png"
        return str(path) if self.tab.screenshot(Path(path)) else ""

    # ------------------------------------------------------------------- ask

    def ask(
        self,
        prompt: str,
        timeout: float | None = None,
        lock: bool = True,
    ) -> str:
        if not (prompt or "").strip():
            raise ValueError("Prompt is empty.")
        timeout = float(
            timeout if timeout is not None else self.cfg.get("api.timeout", 180)
        )
        if lock:
            with gemini_lock(
                timeout=timeout + 90, configured_dir=self.lock_dir
            ):
                return self.ask(prompt, timeout=timeout, lock=False)

        started = time.time()
        state = self.wait_ready()
        state = self._wait_idle(state, timeout=min(self.idle_seconds, timeout))
        before = int(state.get("mc") or 0)
        before_text = str(state.get("last") or "")
        self._submit(prompt, before)
        answer = self._wait_answer(before, timeout, prompt, before_text=before_text)
        log.info(
            "Gemini answer received — %.1fs, %s chars",
            time.time() - started,
            len(answer),
        )
        return answer
