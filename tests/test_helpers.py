"""Unit tests for pure helpers that do not need a browser."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gwa.browser import (
    find_gemini_tab,
    find_gemini_tabs,
    gemini_url_prefix,
    is_gemini_url,
)
from gwa.config import Config, load
from gwa.gemini import clean_answer
from gwa.server import (
    APIError,
    Worker,
    completion_prompt,
    estimate_tokens,
    messages_to_prompt,
    split_chunks,
    text_content,
)


class UrlHelperTests(unittest.TestCase):
    def test_gemini_url_prefix(self):
        self.assertEqual(
            gemini_url_prefix("https://gemini.google.com/app/abc123"),
            "https://gemini.google.com",
        )
        self.assertEqual(gemini_url_prefix(""), "https://gemini.google.com")

    def test_is_gemini_url(self):
        self.assertTrue(is_gemini_url("https://gemini.google.com/app"))
        self.assertTrue(
            is_gemini_url("https://gemini.google.com/app/2dd0af10478a59c8")
        )
        self.assertTrue(
            is_gemini_url(
                "https://gemini.google.com/app/2dd0af10478a59c8?usp=sharing"
            )
        )
        self.assertFalse(is_gemini_url("https://gemini.google.com.evil.test/app"))
        self.assertFalse(is_gemini_url("https://example.com/app"))
        self.assertFalse(is_gemini_url(""))

    def test_is_gemini_url_exact_conversation_prefix(self):
        prefix = "https://gemini.google.com/app/2dd0af10478a59c8"
        self.assertTrue(
            is_gemini_url(
                "https://gemini.google.com/app/2dd0af10478a59c8?usp=sharing",
                prefix,
            )
        )
        self.assertFalse(
            is_gemini_url("https://gemini.google.com/app/other-conversation", prefix)
        )

    @mock.patch("gwa.browser.list_targets")
    def test_find_gemini_tab_prefers_conversation(self, list_targets):
        list_targets.return_value = [
            {
                "type": "page",
                "id": "blank",
                "url": "https://gemini.google.com/app",
                "webSocketDebuggerUrl": "ws://blank",
            },
            {
                "type": "page",
                "id": "chat",
                "url": "https://gemini.google.com/app/2dd0af10478a59c8",
                "webSocketDebuggerUrl": "ws://chat",
            },
            {
                "type": "page",
                "id": "other",
                "url": "https://example.com/",
                "webSocketDebuggerUrl": "ws://other",
            },
            {
                "type": "iframe",
                "id": "frame",
                "url": "https://gemini.google.com/app/frame",
                "webSocketDebuggerUrl": "ws://frame",
            },
        ]
        found = find_gemini_tab("http://127.0.0.1:9222")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], "chat")
        tabs = find_gemini_tabs("http://127.0.0.1:9222")
        self.assertEqual([tab["id"] for tab in tabs], ["chat", "blank"])

    @mock.patch("gwa.browser.list_targets", return_value=[])
    def test_find_gemini_tab_returns_none(self, _list_targets):
        self.assertIsNone(find_gemini_tab("http://127.0.0.1:9222"))


class PromptTests(unittest.TestCase):
    def test_text_content_string(self):
        self.assertEqual(text_content("hello", 0), "hello")

    def test_text_content_parts(self):
        value = text_content(
            [{"type": "text", "text": "one"}, {"type": "input_text", "text": "two"}],
            1,
        )
        self.assertEqual(value, "one\ntwo")

    def test_text_content_rejects_images(self):
        with self.assertRaises(APIError) as ctx:
            text_content([{"type": "image_url", "image_url": {"url": "x"}}], 0)
        self.assertEqual(ctx.exception.code, "image_not_supported")

    def test_messages_to_prompt_single_user(self):
        self.assertEqual(
            messages_to_prompt([{"role": "user", "content": "hello"}]),
            "hello",
        )

    def test_messages_to_prompt_multi_turn(self):
        prompt = messages_to_prompt(
            [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": "How are you?"},
            ]
        )
        self.assertIn("System:\nBe brief.", prompt)
        self.assertIn("User:\nHi", prompt)
        self.assertIn("Assistant:\nHello", prompt)
        self.assertIn("User:\nHow are you?", prompt)

    def test_messages_to_prompt_requires_user(self):
        with self.assertRaises(APIError) as ctx:
            messages_to_prompt([{"role": "system", "content": "x"}])
        self.assertEqual(ctx.exception.code, "missing_user_message")

    def test_messages_to_prompt_rejects_empty(self):
        with self.assertRaises(APIError) as ctx:
            messages_to_prompt([])
        self.assertEqual(ctx.exception.code, "invalid_messages")

    def test_completion_prompt(self):
        self.assertEqual(completion_prompt("hello"), "hello")
        self.assertEqual(
            completion_prompt(["one", {"prompt": "two"}, {"text": "three"}]),
            "one\ntwo\nthree",
        )

    def test_clean_answer_removes_prefix(self):
        self.assertEqual(clean_answer("Gemini said: Hello"), "Hello")
        self.assertEqual(clean_answer("  plain  "), "plain")

    def test_split_chunks(self):
        self.assertEqual(split_chunks("abcdef", 2), ["ab", "cd", "ef"])
        self.assertEqual(split_chunks("", 4), [])

    def test_estimate_tokens(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)


class ConfigTests(unittest.TestCase):
    def test_config_get_and_set(self):
        cfg = Config({"api": {"port": 8787}}, Path("config.yaml"))
        self.assertEqual(cfg.port, 8787)
        self.assertEqual(cfg.get("api.missing", "fallback"), "fallback")
        cfg.set("api.port", 9000)
        self.assertEqual(cfg.port, 9000)

    def test_load_and_env_override(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text(
                "api:\n  port: 8787\nbrowser:\n  prefer_cdp: ''\n",
                encoding="utf-8",
            )
            env = {
                "GWA_PORT": "9999",
                "GWA_LOG_PROMPTS": "false",
                "GWA_REUSE_TAB": "false",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                cfg = load(path)
            self.assertEqual(cfg.port, 9999)
            self.assertFalse(cfg.get("api.log_prompts"))
            self.assertFalse(cfg.get("gemini.reuse_tab"))

    def test_worker_stats_reports_reuse_tab(self):
        cfg = Config(
            {
                "browser": {"prefer_cdp": "", "cdp_url": ""},
                "gemini": {"reuse_tab": True},
            },
            Path("config.yaml"),
        )
        stats = Worker(cfg).stats()
        self.assertTrue(stats["reuse_tab"])
        self.assertFalse(stats["tab_connected"])
        self.assertEqual(stats["tab_id"], "")


if __name__ == "__main__":
    unittest.main()
