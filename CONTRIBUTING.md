# Contributing

Thanks for helping improve Gemini Web API. The project is built and maintained
by [johirxofficial](https://github.com/johirxofficial).

## Before you start

- This project is unofficial browser automation. Please do not present it as an official Google or Gemini API.
- Keep changes small, focused, and cross-platform.
- Never commit browser profiles, `config.yaml`, logs, screenshots, or API keys.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Run the API:

```bash
python -m gwa serve --no-open
```

Run the tests:

```bash
python -m unittest discover -s tests -v
```

## Pull requests

1. Fork the repository and create a branch.
2. Add or update tests for behavior changes.
3. Run the test suite and a syntax check:

   ```bash
   python -m compileall -q gwa
   python -m unittest discover -s tests -v
   ```

4. Describe what changed and why.
5. Keep user-facing messages and documentation in English.

## Reporting issues

Please include:

- Operating system and Python version
- Browser name and version
- `python -m gwa doctor` output (remove any private paths if needed)
- The relevant part of `logs/api.log`
- Whether an existing Gemini tab was open

## Code style

- Standard library first; avoid unnecessary dependencies.
- Type hints where practical.
- Keep platform-specific code isolated in `gwa/browser.py`.
- Do not log prompt text unless `api.log_prompts` is enabled.
