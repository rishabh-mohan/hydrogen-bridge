# hydrogen-bridge

Use your Gemini Enterprise web account as a coding agent in your terminal, no API key needed.

It runs a small local server that looks like the OpenAI API to other tools. Each request is sent through a Chromium window that is logged in to the Gemini Enterprise web app. [OpenCode](https://opencode.ai) connects to that server, so you get an agent like Claude Code that can read and edit your files.

Set up for **Purdue's** Gemini Enterprise out of the box. Other organizations can point it at their own setup; see [below](#using-another-organization).

> **Unofficial.** This drives the web app the way a browser does. It is not a supported Google API, and it may break when the site changes. Your organization's acceptable-use policy and Google's terms still apply, so use it at your own risk.

## What you get

- Models: Gemini 4 Hydrogen / Carbon / Argon, Gemini 3.8 / 3.7 / 3.5 Flash, Gemini 3.1 Pro, Gemini 2.5 Pro, Claude Opus 5.5 and Claude Sonnet 5.5. The bridge asks Google which models your org offers at startup (`/v1/models`). Switch with `/models` in OpenCode.
- Effort levels (OpenCode variants): low / medium / high, plus minimal for Claude.
- Tool use (read, edit, run commands) emulated through the chat.
- Image and PDF attachments.
- **No stored passwords.** When you need to sign in, a browser window opens. Log in normally (SSO, 2FA) and the window closes on its own. The session is kept in `profile/` on your machine.

## Setup (Windows)

1. Install [Python 3.11+](https://www.python.org/) and [Node.js](https://nodejs.org/).
2. In this folder:
   ```powershell
   pip install -r requirements.txt
   python -m playwright install chromium
   npm install -g --allow-scripts=opencode-ai opencode-ai
   ```
3. Optionally, add this folder to your PATH so `hydrogen-manual` works from anywhere.

## Usage

```powershell
hydrogen-manual                         # OpenCode in the current folder
hydrogen-manual C:\path\to\project      # OpenCode in that folder
hydrogen-manual -Model claude-opus-5-5  # start on a different model
hydrogen-manual -ShowBrowser            # keep the bridge's browser visible
hydrogen-manual -BridgeOnly             # just the server, for other OpenAI-compatible tools
```

The first run opens a browser window. Sign in to Gemini Enterprise there, and it closes once you're in. After that it runs headless until your session expires.

With `-BridgeOnly`, any OpenAI-compatible client can use base URL `http://127.0.0.1:8766/v1` with any API key.

To use the bridge from another machine (for example, run it on a headless server once you have a signed-in `profile/`), set `HOST` to the address it should listen on (default `127.0.0.1`). The bridge accepts any API key, so only bind it to a private network such as Tailscale, never a public interface.

If you are signed in to several Google accounts, set `AUTHUSER` (for example `$env:AUTHUSER = "1"`) to the account index from the web app's URL (`/u/1/`).

## Request reliability

Browser calls run one at a time, including session recovery, so signing in cannot
interrupt another request. Requests waiting their turn can be cancelled too.
Disconnecting your client cancels queued work, stops retries, and aborts an active
browser fetch. Google may already have started processing that request; aborting
the connection cannot guarantee that remote computation stops.

Set `UPSTREAM_TIMEOUT` to limit each browser fetch (default 600 seconds), and
`REQUEST_TIMEOUT` to limit the whole chat request, including queueing, uploads,
sign-in and retries (default 1200 seconds). Streaming clients receive keepalive
comments while waiting; answer text still arrives once generation finishes.

Errors include a specific `error.type`, a `retryable` hint, and an upstream HTTP
status when available. Network failures, rate limits, permission denials and
timeouts do not trigger sign-in. A 401 or explicit `UNAUTHENTICATED` response gets
one session-recovery attempt. Other failed POSTs are not automatically replayed,
because the server may have received them. Rate-limit HTTP responses preserve
`Retry-After`. After streaming headers have been sent, failures are delivered as
an SSE error object followed by `[DONE]`, rather than changing the HTTP status.

Google does not report token usage, so the `usage` in each response is a rough
estimate from the prompt and reply length (attachments are not counted). It lets
clients such as OpenCode know when to compact a long conversation.

Run the tests without a Google account:

```powershell
python -m playwright install chromium
python -m unittest discover -v
```

## Content filter

Organizations can run Google's Model Armor on prompts *and* replies. When the model's reply is blocked, the bridge asks again (up to twice), since replies vary. If it is still blocked, the bridge retries with less history. If that fails too, it returns a `content_filter_error` (HTTP 403, or an SSE error if streaming has started). Rephrase to try again. This is distinct from a Google permission denial and does not trigger sign-in.

## Using another organization

Open your Gemini Enterprise web app with your browser's dev tools open (Network tab), send a message, and find the `widgetStreamAssist` request. Then set these environment variables before running:

| Variable | Where to find it | Example |
|---|---|---|
| `GE_CONFIG_ID` | the `cid/...` part of the web app URL | `3d214ebb-...` |
| `GE_ENGINE` | `collections/.../engines/...` in the request body | `collections/default_collection/engines/your-app` |
| `GE_PROJECT` | `projects/<number>/locations/us` in the request body | `projects/123456789/locations/us` |
| `GE_WEB_KEY` | the `key=` parameter of the request URL (public site key) | `AIza...` |
| `GE_TIME_ZONE` | your time zone (optional) | `America/New_York` |

## Files

- `bridge.py`: the server
- `transport.py` / `request_runtime.py`: browser calls, cancellation and errors
- `run.ps1` / `hydrogen-manual.cmd`: the launcher
- `opencode.json`: OpenCode provider and model settings

`profile/`, the logs and the saved responses contain your session and your prompts. They are gitignored. **Don't share them.**
