"""OpenAI-compatible local endpoint backed by the Gemini Enterprise web app.

Runs the real API call inside a logged-in Chromium page, so the browser supplies
your session cookies. No credentials are stored: when a sign-in is needed, a browser
window opens for you to log in, and closes again once you're signed in.
"""
import base64
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
# Org-specific IDs. Defaults are Purdue's; other orgs: see README ("Using another organization").
CONFIG_ID = os.environ.get("GE_CONFIG_ID", "3d214ebb-d244-423a-a021-61b49b8b5f09")
ENGINE = os.environ.get("GE_ENGINE", "collections/default_collection/engines/purdue-prod-geminienterprise-app")
WEB_KEY = os.environ.get("GE_WEB_KEY", "AIzaSyD9imQMsvN72Z2__FTNHdWKoyNaq1q2TI4")  # the site's public browser key, not a credential
TIME_ZONE = os.environ.get("GE_TIME_ZONE", "America/Indianapolis")
ENDPOINT = f"https://us-discoveryengine.clients6.google.com/v1alpha/locations/us/widgetStreamAssist?key={WEB_KEY}"
HOME = f"https://vertexaisearch.cloud.google.com/us/home/cid/{CONFIG_ID}"
AUTHUSER = os.environ.get("AUTHUSER", "0")
PORT = int(os.environ.get("PORT", "8766"))
DEFAULT_MODEL = os.environ.get("MODEL", "gemini-4-hydrogen")
HEADLESS = os.environ.get("HEADLESS") == "1"
LOGIN_TIMEOUT = int(os.environ.get("LOGIN_TIMEOUT", "600"))  # seconds to wait for you to sign in

PROJECT = os.environ.get("GE_PROJECT", "projects/399990213106/locations/us")
API = "https://us-discoveryengine.clients6.google.com"

# body: a string (sent as-is) or bodyB64: base64 bytes. Returns status, text, and the resumable-upload URL header.
CALL_JS = """
async ({url, body, bodyB64, contentType, extraHeaders, authuser, serverToken}) => {
  const m = document.cookie.match(/(?:^|; )SAPISID=([^;]+)/);
  if (!m) return {status: 401, text: 'no SAPISID cookie (signed out)'};
  const ts = Math.floor(Date.now() / 1000);
  const buf = await crypto.subtle.digest('SHA-1',
      new TextEncoder().encode(`${ts} ${m[1]} ${location.origin}`));
  const h = ts + '_' + [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
  const headers = {
    'Content-Type': contentType,
    'Authorization': `SAPISIDHASH ${h} SAPISID1PHASH ${h} SAPISID3PHASH ${h}`,
    'X-Goog-AuthUser': authuser,
    'X-Goog-Api-Client': 'gemini-enterprise-classic/1.0.0',
    ...extraHeaders,
  };
  if (serverToken) headers['X-Server-Token'] = serverToken;
  const payload = bodyB64 != null ? Uint8Array.from(atob(bodyB64), c => c.charCodeAt(0)) : body;
  try {
    const r = await fetch(url, {method: 'POST', headers, body: payload, credentials: 'include'});
    return {status: r.status, text: await r.text(), uploadUrl: r.headers.get('x-goog-upload-url')};
  } catch (e) {
    return {status: 401, text: 'fetch failed (likely redirected to sign-in): ' + e};
  }
}
"""

state = {"page": None, "server_token": None, "login_lock": asyncio.Lock()}


# ---------- sign-in (manual, in a visible browser window) ----------

READY = '[role="textbox"], textarea, [contenteditable="true"]'
APP_HOST = "vertexaisearch.cloud.google.com"


async def vis(page, sel):
    try:
        return await page.locator(sel).first.is_visible()
    except Exception:
        return False


async def signed_in(page):
    return urlparse(page.url).hostname == APP_HOST and await vis(page, READY)


def sniff(req):  # reuse the app's X-Server-Token if it sends one
    tok = req.headers.get("x-server-token")
    if tok:
        state["server_token"] = tok


async def open_browser(headless):
    """(Re)launch the persistent browser profile, visible or headless."""
    if state.get("ctx"):
        await state["ctx"].close()
    ctx = await state["pw"].chromium.launch_persistent_context(
        str(HERE / "profile"), headless=headless, viewport=None,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"])
    ctx.on("request", sniff)
    state["ctx"] = ctx
    state["page"] = ctx.pages[0] if ctx.pages else await ctx.new_page()


async def wait_ready(page, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if await signed_in(page):
            return True
        await page.wait_for_timeout(500)
    return False


async def sign_in():
    """Make sure the app is signed in; if not, show a browser window until the user logs in."""
    async with state["login_lock"]:
        await state["page"].goto(HOME, wait_until="domcontentloaded")
        if await wait_ready(state["page"], 15):
            return
        print("sign-in needed: opening a browser window - log in there", flush=True)
        if HEADLESS:
            await open_browser(headless=False)
            await state["page"].goto(HOME, wait_until="domcontentloaded")
        page = state["page"]
        await page.bring_to_front()
        try:
            end = time.time() + LOGIN_TIMEOUT
            while time.time() < end:
                if page.is_closed():
                    raise RuntimeError("the sign-in window was closed before you finished logging in")
                if await signed_in(page):
                    break
                await page.wait_for_timeout(1000)
            else:
                raise RuntimeError(f"timed out after {LOGIN_TIMEOUT}s waiting for you to sign in")
            print("signed in", flush=True)
        finally:
            if HEADLESS:  # close the visible window and go back to headless, signed in or not
                await open_browser(headless=True)
                await state["page"].goto(HOME, wait_until="domcontentloaded")
        if not await wait_ready(state["page"], 30):
            raise RuntimeError("signed in, but the app didn't load afterwards; try again")


# ---------- request translation ----------

TOOL_RULES = """You are connected to tools that run on the user's computer. To use one, output a block exactly like:
<tool_call>{"name": "TOOL_NAME", "arguments": {...}}</tool_call>
Rules:
- The JSON must be valid and on one line; "arguments" must match the tool's parameter schema.
- You may output several <tool_call> blocks in one reply (they run in order). Put any short explanation BEFORE them.
- After your last <tool_call>, STOP. Never write <tool_result> blocks yourself; real results come back in the next turn.
- If no tool is needed, just answer normally with no <tool_call> block.
Available tools (JSON schema):
"""


MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
            "application/pdf": "pdf", "text/plain": "txt"}


def render(messages, tools):
    """Turn an OpenAI chat request (tool calls/results, images, files) into (prompt, attachments).

    attachments is a list of (filename, base64_bytes) to upload; the prompt mentions each by name.
    """
    attachments = []

    def attach(data_uri, name=None):
        m = re.match(r"data:([^;,]+)?(?:;[^,]*)?;base64,(.*)", data_uri or "", re.S)
        if not m:
            return f"[Attachment not sent: only inline base64 data is supported ({(data_uri or '')[:60]})]"
        mime = m.group(1) or "application/octet-stream"
        name = name or f"attachment{len(attachments) + 1}.{MIME_EXT.get(mime, 'bin')}"
        attachments.append((name, m.group(2)))
        return f"[Attached file: {name}]"

    def text(c):
        if not isinstance(c, list):
            return c or ""
        out = []
        for p in c:
            t = p.get("type")
            if t == "text":
                out.append(p.get("text", ""))
            elif t == "image_url":
                out.append(attach((p.get("image_url") or {}).get("url")))
            elif t == "file":
                f = p.get("file") or {}
                out.append(attach(f.get("file_data"), f.get("filename")))
        return "\n".join(out)

    if len(messages) == 1 and not tools:
        return text(messages[0]["content"]), attachments
    turns = []
    for m in messages:
        if m["role"] == "tool":
            turns.append(f'<tool_result id="{m.get("tool_call_id", "")}">\n{text(m.get("content"))}\n</tool_result>')
            continue
        body = text(m.get("content"))
        for tc in m.get("tool_calls") or []:
            f = tc["function"]
            try:
                args = json.loads(f.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = f.get("arguments")
            body += "\n<tool_call>" + json.dumps({"name": f["name"], "arguments": args}) + "</tool_call>"
        turns.append(f"<{m['role']}>\n{body.strip()}\n</{m['role']}>")
    head = ("The conversation so far is below. Reply as the assistant to the last message. "
            "Output only your reply, without role tags.")
    if attachments:
        head += ("\n\nFiles are uploaded to you directly with this message: "
                 + ", ".join(n for n, _ in attachments)
                 + ". Wherever the conversation says [Attached file: NAME], read that file with your own built-in "
                   "file access (not the user's tools).")
    tail = ""
    if tools:
        specs = [t["function"] for t in tools if t.get("type") == "function"]
        head += "\n\n" + TOOL_RULES + json.dumps(specs)
        tail = "\n\n(Reminder: act through <tool_call> blocks, then stop and wait for results.)"
    return head + "\n\n" + "\n\n".join(turns) + tail, attachments


CALL_RE = re.compile(r"<tool_call>\s*(?:```(?:json)?\s*)?(\{.*?\})\s*(?:```\s*)?</tool_call>", re.S)


BLOCKED = "blocked by your organization's content policy"


def split_calls(answer):
    """Pull <tool_call> blocks out of the reply as OpenAI tool_calls.

    Returns (text, calls, problem); problem describes a malformed/truncated call, else ''.
    """
    calls, problem = [], ""
    for m in CALL_RE.finditer(answer):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            problem = f"invalid JSON in a <tool_call> ({e.msg} at char {e.pos}): {m.group(1)[:200]}"
            continue
        if not isinstance(obj, dict) or "name" not in obj:
            problem = "a <tool_call> is missing \"name\""
            continue
        args = obj.get("arguments", obj.get("parameters", {}))
        calls.append({"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                      "function": {"name": obj["name"],
                                   "arguments": args if isinstance(args, str) else json.dumps(args)}})
    if answer.count("<tool_call>") > len(CALL_RE.findall(answer)):
        problem = "a <tool_call> block was not closed with </tool_call> or is not a single JSON object"
    if "<tool_call>" not in answer:
        return answer, [], problem
    # Keep only text before the first call; drop anything the model invented after it.
    return answer[:answer.index("<tool_call>")].strip(), calls, problem


def extract(raw):
    """Collect answer text from the streamed response, skipping 'thought' parts."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = [json.loads(l.strip().strip(",[]")) for l in raw.splitlines() if l.strip().strip(",[]")]
    out, reasons = [], []

    def walk(x):
        if isinstance(x, dict):
            c = x.get("content")
            if isinstance(c, dict) and isinstance(c.get("text"), str) and not c.get("thought"):
                out.append(c["text"])
            if isinstance(x.get("modelArmorViolation"), str):
                reasons.append(x["modelArmorViolation"])
            for k, v in x.items():
                if k != "content":
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    # Drop the web app's display-only "[[section:Title]]" markers.
    text = re.sub(r"\[\[section:[^\]\n]*\]\]\n?", "", "".join(out))
    if reasons:  # say which filter fired, so a blocked stop isn't a mystery
        text += "\n(Filter reason: " + "; ".join(dict.fromkeys(reasons)) + ")"
    return text


async def api(url, body=None, b64=None, content_type="application/json", headers=None):
    """POST from inside the logged-in page; signs in again once if the session has expired."""
    args = {"url": url, "body": body, "bodyB64": b64, "contentType": content_type,
            "extraHeaders": headers or {}, "authuser": AUTHUSER}
    for attempt in range(2):
        r = await state["page"].evaluate(CALL_JS, {**args, "serverToken": state["server_token"]})
        if r["status"] in (401, 403) and attempt == 0:
            print(f"session expired (HTTP {r['status']}); signing in again", flush=True)
            await sign_in()
            continue
        return r


def widget(method, request_key, request):
    return (f"{API}/v1alpha/locations/us/{method}?key={WEB_KEY}",
            json.dumps({"configId": CONFIG_ID, "additionalParams": {"token": "-", "origin": "ORIGIN_UNSPECIFIED"},
                        request_key: request}))


async def upload_files(attachments):
    """Create a session and upload each (name, base64) into it. Returns (session_name, file_ids)."""
    r = await api(*widget("widgetCreateSession", "createSessionRequest", {"session": {"name": "-", "displayName": ""}}))
    if r["status"] != 200:
        raise RuntimeError(f"creating a session for uploads failed: HTTP {r['status']}: {r['text'][:300]}")
    session = json.loads(r["text"])["session"]["name"]
    url = f"{API}/upload/v1alpha/{PROJECT}/{session}:uploadFile?key={WEB_KEY}"
    ids = []
    for name, b64 in attachments:
        size = len(base64.b64decode(b64))
        start = await api(url, "", content_type="application/x-www-form-urlencoded;charset=UTF-8", headers={
            "x-goog-upload-protocol": "resumable", "x-goog-upload-command": "start",
            "x-goog-upload-file-name": name, "x-goog-upload-header-content-length": str(size)})
        if start["status"] != 200 or not start.get("uploadUrl"):
            raise RuntimeError(f"upload of {name} failed to start: HTTP {start['status']}: {start['text'][:300]}")
        done = await api(start["uploadUrl"], b64=b64, content_type="application/x-www-form-urlencoded;charset=utf-8",
                         headers={"x-goog-upload-command": "upload, finalize", "x-goog-upload-offset": "0",
                                  "x-goog-upload-file-name": name})
        if done["status"] != 200:
            raise RuntimeError(f"upload of {name} failed: HTTP {done['status']}: {done['text'][:300]}")
        ids.append(json.loads(done["text"])["fileId"])
        print(f"uploaded {name} ({size} bytes)", flush=True)
    return session, ids


async def ask(prompt, model, attachments=(), effort=None):
    session, file_ids = (await upload_files(attachments)) if attachments else (f"{ENGINE}/sessions/-", [])
    body = {
        "configId": CONFIG_ID,
        "additionalParams": {"token": "-", "origin": "ORIGIN_UNSPECIFIED"},
        "streamAssistRequest": {
            "session": session,
            "query": {"parts": [{"text": prompt}]},
            "filter": "",
            "fileIds": file_ids,
            "answerGenerationMode": "NORMAL",
            "agentsSpec": {},
            "toolsSpec": {"toolRegistry": "default_tool_registry"},
            "languageCode": "en-US",
            "userMetadata": {"timeZone": TIME_ZONE},
            "assistSkippingMode": "REQUEST_ASSIST",
            "assistGenerationConfig": {"modelId": model},
        },
    }
    if effort:  # MINIMAL / LOW / MEDIUM / HIGH (what the web app sends for its effort picker)
        body["streamAssistRequest"]["generationSpec"] = {"thinkingConfig": {"thinkingLevel": effort}}
    r = await api(ENDPOINT, json.dumps(body))
    (HERE / "last_response.txt").write_text(f"HTTP {r['status']}\n{r['text']}", encoding="utf-8")
    if r["status"] != 200:
        raise RuntimeError(f"Gemini Enterprise returned HTTP {r['status']}: {r['text'][:500]}")
    answer = extract(r["text"])
    if not answer:
        raise RuntimeError("Got a response but found no answer text; see last_response.txt")
    return answer


@asynccontextmanager
async def lifespan(app):
    async with async_playwright() as p:
        state["pw"] = p
        await open_browser(headless=HEADLESS)
        await sign_in()
        print(f"Bridge on http://127.0.0.1:{PORT}/v1", flush=True)
        yield
        await state["ctx"].close()


app = FastAPI(lifespan=lifespan)


@app.get("/v1/models")
async def models():
    ids = dict.fromkeys([DEFAULT_MODEL, "gemini-4-hydrogen", "gemini-4-carbon", "claude-opus-5-5", "claude-sonnet-5-5"])
    return {"object": "list", "data": [{"id": i, "object": "model", "owned_by": "gemini-enterprise"} for i in ids]}


def trim_history(messages, level):
    """Fallback when the org content filter blocks a request: resend less history.

    level 0: everything; 1: system + the current turn (last user message onward);
    2: system + only the last user message. Earlier context is replaced by a short note.
    """
    if level == 0:
        return messages
    system = [m for m in messages if m["role"] in ("system", "developer")]
    users = [i for i, m in enumerate(messages) if m["role"] == "user"]
    last = users[-1] if users else 0
    tail = messages[last:] if level == 1 else messages[last:last + 1]
    note = {"role": "user", "content": "(Earlier parts of this conversation were removed because the "
                                       "organization's content filter blocked them. Continue from here.)"}
    return system + [note] + tail


@app.post("/v1/chat/completions")
async def chat(req: Request):
    payload = await req.json()
    model = payload.get("model") or DEFAULT_MODEL
    if not model.startswith(("gemini-", "claude-")):  # tools often send placeholder names like "gpt-4"
        model = DEFAULT_MODEL
    effort = (payload.get("reasoning_effort") or "").lower()
    m = re.match(r"(.+)-(minimal|low|medium|high)$", model)
    if m:  # also accept effort as a model-name suffix, e.g. claude-opus-5-5-high
        model, effort = m.group(1), effort or m.group(2)
    effort = effort.upper() if effort in ("minimal", "low", "medium", "high") else None
    if effort:
        print(f"{model} effort={effort}", flush=True)
    tools = payload.get("tools") or []
    # Never send back earlier replies that were just the content-filter notice.
    messages = [m for m in payload["messages"]
                if not (m.get("role") == "assistant" and BLOCKED in str(m.get("content") or ""))]
    try:
        # Blocked by the org content filter -> retry with less history (a blocked item in the
        # history would otherwise poison every later request). Malformed tool call -> ask again.
        level, note = 0, ""
        for attempt in range(5):
            prompt, attachments = render(trim_history(messages, level), tools)
            answer = await ask(prompt + note, model, attachments, effort)
            text, calls, problem = split_calls(answer) if tools else (answer, [], "")
            if BLOCKED in answer:
                (HERE / "blocked_prompt.txt").write_text(prompt, encoding="utf-8")  # for diagnosing the filter
                # Level 2 drops this turn's tool results; only do that when there are none, or the
                # model forgets what it just found and loops on the same tool calls.
                if level == 2 or (level == 1 and messages[-1]["role"] != "user"):
                    break
                level, note = level + 1, ""
                print(f"blocked by content filter{answer[answer.find('(Filter reason'):][:150] if '(Filter reason' in answer else ''}; "
                      f"retrying with less history (level {level})", flush=True)
                continue
            if not problem or attempt == 4:
                break
            print(f"retrying ({problem[:120]})", flush=True)
            note = (f"\n\n(Note: {problem}. Send your full reply again, with every <tool_call> as one valid "
                    "JSON object on one line; arrays must use [ ].)")
    except RuntimeError as e:
        return JSONResponse({"error": {"message": str(e), "type": "upstream_error"}}, status_code=502)
    if BLOCKED in text and not calls:
        text = text.strip() + ("\n\n(The task stopped here because your organization's content filter blocked the reply, "
                               "even with earlier conversation removed. Say 'continue' to retry, or rephrase.)")
    finish = "tool_calls" if calls else "stop"

    cid, now = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
    if not payload.get("stream"):
        msg = {"role": "assistant", "content": text or None}
        if calls:
            msg["tool_calls"] = calls
        return {"id": cid, "object": "chat.completion", "created": now, "model": model,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    def sse():
        base = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": model}
        deltas = [({"role": "assistant", "content": text}, None)]
        if calls:
            deltas.append(({"tool_calls": [{**c, "index": i} for i, c in enumerate(calls)]}, None))
        deltas.append(({}, finish))
        for delta, fin in deltas:
            yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': fin}]})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
