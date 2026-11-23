# Changelog

All notable changes are documented here.

## [1.1.0] - 2026-11-23

### Added

- Persistent Gemini tab: one tab and one ongoing chat are reused for every
  request.
- Automatic discovery of an existing Gemini tab, preferring a conversation URL.
- `gemini.reuse_tab`, `gemini.tab_url_prefix`, `gemini.clear_composer`, and
  `gemini.idle_seconds` options.
- Tab status in `/health` (`tab_connected`, `tab_id`, `tab_url`).
- Linux and macOS launcher scripts.
- macOS browser detection and profile paths.
- Unit tests, GitHub Actions CI, issue templates, and security policy.

### Changed

- `login` now reuses an existing Gemini tab instead of always opening a new one.
- The composer is cleared before inserting a new prompt.
- The worker waits for any previous Gemini answer to finish before submitting.
- Answer detection now handles reused conversations where older answers are
  already present.
- User-facing messages, comments, and documentation are in English.
- Version bumped to 1.1.0.

### Credits

- Developer and maintainer: **johirxofficial**
  (<https://github.com/johirxofficial>)

### Fixed

- Boolean environment overrides (`GWA_LOG_PROMPTS`, `GWA_REUSE_TAB`) now parse
  correctly.
- The API now releases the DevTools socket cleanly on shutdown.
- Prompt logging is honored when `api.log_prompts` is enabled.
