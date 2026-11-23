# Gemini Web API

**Turn the Gemini web app in your own Chrome into an OpenAI-compatible local API — no official Gemini API key required.**

Gemini Web API drives a real, logged-in Gemini web session over the Chrome DevTools Protocol (CDP). It opens **one Gemini tab and reuses it for every request**, so your API calls stay in the same continuous chat instead of creating a new chat each time.

> This is an unofficial browser-automation project. It is not affiliated with Google and is not the official Gemini API.

---

## Features

- OpenAI-compatible endpoints: `/v1/chat/completions`, `/v1/completions`, `/v1/models`
- Simple endpoint: `POST /api/ask`
- **Persistent Gemini tab** — one chat tab is reused for every request
- Works with your existing Chrome login; no API key and no browser download
- Streaming responses (simulated SSE chunks)
- System prompts and multi-turn history
- Built-in docs and playground at `http://127.0.0.1:8787/`
- Optional API key, rate limit, request queue, and prompt-size limit
- Cross-platform: Windows, Linux, and macOS
- Only two Python dependencies: `websocket-client` and `PyYAML`

---

## Requirements

- Python **3.10+**
- Google Chrome, Chrome Beta, Chromium, Microsoft Edge, or Brave
- A Google account logged in to <https://gemini.google.com>

The project uses your system browser. It does not download a separate browser.

---

## Quick start

### Windows

```bat
cd C:\path\to\gemini-web-api
install.cmd
login.cmd
api.cmd
```

### Linux / macOS

```bash
cd /path/to/gemini-web-api
./install.sh
./login.sh
./api.sh
```

Then open:

| What | URL |
|---|---|
| Docs + playground | <http://127.0.0.1:8787/> |
| Health | <http://127.0.0.1:8787/health> |
| Models | <http://127.0.0.1:8787/v1/models> |
| OpenAPI | <http://127.0.0.1:8787/openapi.json> |

To use another port:

```bash
./api.sh --port 8788
```

On Windows:

```bat
api.cmd --port 8788
```

---

## How the persistent tab works

1. On the first request, the API looks for an already-open Gemini tab.
2. If it finds one, it uses that tab. If not, it opens a Gemini tab once.
3. Every later request reuses the same tab and the same conversation.
4. The tab is never closed automatically by the API.
5. If the tab is closed, the API opens a new one and keeps reusing it.
6. If the DevTools connection drops, the API reconnects to the same tab.

This behavior is controlled by:

```yaml
gemini:
  reuse_tab: true
  tab_url_prefix: "https://gemini.google.com"
  clear_composer: true
  idle_seconds: 120
```

Set `gemini.reuse_tab: false` only if you want the old behavior (a fresh tab for each request).

> **Tip:** Pin or keep one Gemini tab open. The API will pick it up automatically, including an existing conversation URL such as `https://gemini.google.com/app/<conversation-id>`.
>
> To pin one exact conversation, set `gemini.tab_url_prefix` to that conversation URL, for example `https://gemini.google.com/app/6482f077c3932258`.

---

## Install without the scripts

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m gwa login
python -m gwa serve
```

Or install the package:

```bash
pip install -e .
gemini-web-api
```

---

## Browser login

### Use your existing Chrome session (fastest)

If Chrome is already running with remote debugging on port `9222`, the API uses it:

```yaml
browser:
  prefer_cdp: "http://127.0.0.1:9222"
```

The default `config.yaml` already has this. The API looks for an existing Gemini tab in that browser and reuses it.

### Use a dedicated browser profile

```yaml
browser:
  prefer_cdp: ""
  cdp_url: "http://127.0.0.1:9333"
```

Then run the login command:

```bash
./login.sh        # Linux / macOS
login.cmd         # Windows
```

Chrome opens with a separate profile. Sign in to Gemini, then press Enter in the terminal.

> Chrome 136+ disables remote debugging on the default profile for security reasons. This project therefore uses a separate profile by default.

---

## Usage

### Simple request

```bash
curl http://127.0.0.1:8787/api/ask \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello, who are you?"}'
```

### OpenAI-compatible chat

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-web",
    "messages": [
      {"role": "user", "content": "Explain async/await in JavaScript with one example."}
    ]
  }'
```

### Python OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="not-needed",  # use the real key if api.api_key is set
)

resp = client.chat.completions.create(
    model="gemini-web",
    messages=[{"role": "user", "content": "Give me 3 productivity tips."}],
)
print(resp.choices[0].message.content)
```

### Streaming

```python
stream = client.chat.completions.create(
    model="gemini-web",
    messages=[{"role": "user", "content": "Write a short poem."}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

Streaming is **simulated**: the full answer is generated first, then sent in small SSE chunks. Gemini web does not expose true token streaming.

### Multi-turn

Send a normal `messages` array. System, user, and assistant turns are flattened into one prompt for Gemini. The API reuses the same Gemini chat tab, but it does not read or depend on Gemini's own chat history.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI chat completion (`stream` supported) |
| POST | `/v1/completions` | Legacy completion (`prompt`) |
| POST | `/api/ask` | Simple `{"prompt": "..."}` endpoint |
| GET | `/v1/models` | Model list |
| GET | `/health` | Service, browser, tab, queue, and uptime |
| GET | `/openapi.json` | Minimal OpenAPI 3.1 document |
| GET | `/` | Docs and playground |

### Example `/health`

```json
{
  "ok": true,
  "service": "gemini-web-api",
  "version": "1.1.0",
  "model": "gemini-web",
  "browser": {
    "mode": "shared",
    "cdp_url": "http://127.0.0.1:9222",
    "reuse_tab": true,
    "tab_connected": true,
    "tab_id": "85A885672022F260408512463DF965B6",
    "tab_url": "https://gemini.google.com/app/2dd0af10478a59c8",
    "queue_depth": 0,
    "queue_max": 8,
    "busy": false
  }
}
```

`browser.mode`:

- `shared` — using the browser from `browser.prefer_cdp`
- `own` — using the dedicated browser from `browser.cdp_url`
- `not-connected` — no request has connected yet

---

## Configuration

The full reference is in [`config.example.yaml`](config.example.yaml). Important keys:

```yaml
browser:
  engine: chrome-beta
  binary: ""
  prefer_cdp: "http://127.0.0.1:9222"
  cdp_url: "http://127.0.0.1:9333"
  profile_dir: ""
  auto_launch: true

api:
  host: "127.0.0.1"
  port: 8787
  api_key: ""
  model_name: "gemini-web"
  timeout: 180
  max_queue: 8
  rate_limit_per_min: 30
  max_prompt_chars: 60000
  stream_chunk: 28
  stream_delay_ms: 12
  log_prompts: false

gemini:
  url: "https://gemini.google.com/app"
  reuse_tab: true
  tab_url_prefix: "https://gemini.google.com"
  clear_composer: true
  idle_seconds: 120
  response_selector: "message-content"
  ready_seconds: 20
```

### API key

Set `api.api_key` to require authentication on `/v1/*` and `/api/*`:

```yaml
api:
  api_key: "change-me"
```

Send either:

```http
Authorization: Bearer change-me
```

or:

```http
x-api-key: change-me
```

`/`, `/health`, and `/openapi.json` stay public.

### Environment overrides

| Variable | Config key |
|---|---|
| `GWA_HOST` | `api.host` |
| `GWA_PORT` | `api.port` |
| `GWA_API_KEY` | `api.api_key` |
| `GWA_MODEL` | `api.model_name` |
| `GWA_TIMEOUT` | `api.timeout` |
| `GWA_CDP` | `browser.cdp_url` |
| `GWA_PREFER_CDP` | `browser.prefer_cdp` |
| `GWA_PROFILE` | `browser.profile_dir` |
| `GWA_ENGINE` | `browser.engine` |
| `GWA_LOCK_DIR` | `api.lock_dir` |
| `GWA_LOG_PROMPTS` | `api.log_prompts` |
| `GWA_REUSE_TAB` | `gemini.reuse_tab` |

---

## Project layout

```
gemini-web-api/
├── gwa/
│   ├── browser.py    Chrome detection, tab discovery, CDP launch
│   ├── cdp.py        Minimal tab-level DevTools client
│   ├── gemini.py     Gemini composer, submit, and answer extraction
│   ├── server.py     HTTP API + single-worker queue
│   ├── lock.py       Cross-process Gemini lock
│   ├── config.py     YAML config + environment overrides
│   └── cli.py        serve / login / doctor / ask commands
├── web/index.html    Docs and playground
├── tests/            Unit tests
├── install.cmd       Windows installer
├── api.cmd           Windows API launcher
├── login.cmd         Windows login helper
├── ask.cmd           Windows one-shot prompt
├── doctor.cmd        Windows diagnostics
├── install.sh        Linux/macOS installer
├── api.sh            Linux/macOS API launcher
├── login.sh          Linux/macOS login helper
├── ask.sh            Linux/macOS one-shot prompt
└── doctor.sh         Linux/macOS diagnostics
```

---

## How it works

1. `Service` validates the request and puts it in a single-worker queue.
2. `Worker` picks the next job and calls `_ensure_tab`.
3. `_ensure_tab` finds an existing Gemini tab (preferring a conversation URL) or opens one once.
4. A page-level CDP websocket focuses the composer, clears any draft, inserts the prompt, and clicks Send (or presses Enter).
5. The worker waits for a new `message-content` element and for its text to become stable.
6. The answer is returned and the tab stays open for the next request.

Only one prompt runs at a time. A shared file lock also prevents other local tools (for example, another automation project) from sending a prompt to the same Gemini account at the same time.

---

## Development

Run the tests:

```bash
python -m unittest discover -s tests -v
```

Run the API in the foreground:

```bash
python -m gwa serve --no-open
```

Check the environment:

```bash
python -m gwa doctor
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Browser is not running` | Start Chrome with remote debugging, or run the login command |
| `Gemini is not logged in` | Sign in at <https://gemini.google.com>, then retry |
| `Could not find Gemini's prompt box` | Check `logs/screenshots/`; update `gemini.prompt_selectors` if the UI changed |
| `Gemini did not answer in time` | Increase `api.timeout` (for example, 300) |
| First request is slow | A new tab can take 5–10 seconds to initialize; later requests reuse it |
| Chrome shows `Sign in` | Wait a few seconds, or run the login command again |
| Port is busy | Use `--port 8788` |
| `websocket-client` is missing | Run the install script again |
| Non-ASCII prompt fails in Windows `cmd` | Use the playground, a JSON file, or PowerShell |

Logs:

- `logs/api.log` — request and worker log
- `logs/screenshots/` — screenshots from login, timeout, or UI errors

---

## Limitations

- **Unofficial:** Gemini web UI automation. Google can change the UI and break selectors.
- **Slow:** answers can take 10–60 seconds; long answers take longer.
- **Text only:** no images, vision, function calling, or guaranteed JSON mode.
- **Simulated streaming:** the full answer is generated before chunks are sent.
- **Account limits:** Gemini's own rate limits and CAPTCHA still apply.
- **Login required:** the browser profile must stay signed in.
- **Local by default:** the server binds to `127.0.0.1`. Add authentication, HTTPS, and a reverse proxy before exposing it.

---

## Privacy and security

- Prompts are sent to Gemini through your own logged-in browser.
- Prompt logging is off by default (`api.log_prompts: false`).
- The API key is optional and intended for local use.
- Do not commit `config.yaml`, browser profiles, logs, or screenshots.

---

## Contributing

Issues and pull requests are welcome. Please keep changes small and focused, add tests for new behavior, and run the test suite before opening a PR.

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

---

## License

[MIT](LICENSE)

---

**Disclaimer:** This project is for personal, local automation and experimentation. You are responsible for complying with Google's terms of service and for protecting your account. For production or high-volume use, use the official Gemini API.
