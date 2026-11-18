"""OpenAI-compatible local endpoint backed by the Gemini Enterprise web app.

Runs the real API call inside a logged-in Chromium page, so the browser supplies
your session cookies. No credentials are stored: when a sign-in is needed, a browser
window opens for you to log in, and closes again once you're signed in.
"""
import base64
import json
import os
import re
import sys
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
HOST = os.environ.get("HOST", "127.0.0.1")
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
        # Only Linux needs DISPLAY/WAYLAND_DISPLAY for a window; Windows and macOS can always open one.
        if HEADLESS and sys.platform.startswith("linux") \
                and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            print("sign-in needed, but there is no display here to open a login window", flush=True)
            raise RuntimeError("The Google session expired and this machine has no display for signing in. "
                               "Sign in on a machine with a display, copy its profile/ folder here, "
                               "then restart the bridge.")
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

TOOL_RULES = """You can use the tools listed below; they run on the user's computer. To use a tool, write a fenced code block with the language tag `tool`, containing one JSON object with the tool name and its input. Example:
```tool
{"tool": "TOOL_NAME", "input": {...}}
```
Notes:
- One JSON object per block, valid JSON, and "input" must match the tool's input schema.
- Every reply is a slow round trip, so BATCH: when you need several independent things (reading several files,
  several searches, a glob plus the reads you already know you need), put ALL their tool blocks in ONE reply
  (they run in order). Only wait for results when the next call truly depends on them. Put any short
  explanation before the blocks.
- After your last block, stop. Results come back in the next message; never write them yourself.
- If no tool is needed, just answer normally with no tool block.
{handoff}
Available tools (JSON schema):
"""
NO_HANDOFF = ("- These tools are the only way to touch the user's files, shell or machine. Do not hand off to other "
              "agents or use built-in code execution (such as file_and_coding_agent); it runs remotely and cannot "
              "see the user's files. To run code, use the user's shell tool.")
# With uploads, the built-in file agent is the only thing that can open them, so allow it for those files.
UPLOAD_HANDOFF = ("- These tools are the only way to touch the user's files, shell or machine. The one exception is "
                  "the files uploaded with this message: open those with your built-in file access. Do not use "
                  "built-in code execution for anything else; to run code, use the user's shell tool.")


MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
            "application/pdf": "pdf", "text/plain": "txt"}


def fence(body):
    """Wrap text in a code fence longer than any backtick run inside it."""
    longest = max((len(r) for r in re.findall(r"`{3,}", body or "")), default=2)
    f = "`" * (longest + 1)
    return f"{f}\n{body}\n{f}"


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
    call_names = {}
    for m in messages:
        if m["role"] == "tool":
            out = text(m.get("content"))
            name = call_names.get(m.get("tool_call_id"), "tool")
            turns.append(f"<tool_output>\nOutput of the {name} call:\n{fence(out)}\n</tool_output>")
            continue
        body = text(m.get("content"))
        for tc in m.get("tool_calls") or []:
            f = tc["function"]
            call_names[tc.get("id")] = f["name"]
            try:
                args = json.loads(f.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = f.get("arguments")
            body += "\n```tool\n" + json.dumps({"tool": f["name"], "input": args}) + "\n```"
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
        head += "\n\n" + TOOL_RULES.replace("{handoff}", UPLOAD_HANDOFF if attachments else NO_HANDOFF) + json.dumps(specs)
        tail = "\n\n(Use ```tool blocks for any actions, then stop and wait for the results.)"
    return head + "\n\n" + "\n\n".join(turns) + tail, attachments


CALL_RE = re.compile(r"<tool_call>\s*(?:```(?:json)?\s*)?(\{.*?\})\s*(?:```\s*)?</tool_call>", re.S)
# A raw newline cannot occur inside a JSON string, so the first "\n```" after the opening fence ends the block.
# A ```tool opener, wherever it sits (models sometimes glue it onto the end of a sentence).
TOOL_OPEN_RE = re.compile(r"`{3,}[ \t]*tool\b")
TOOL_CLOSE_RE = re.compile(r"\s*`{3,}")
_json = json.JSONDecoder()


BLOCKED = "blocked by your organization's content policy"


def split_calls(answer):
    """Pull tool blocks (```tool fences, or legacy <tool_call> tags) out of the reply as OpenAI tool_calls.

    Each ```tool opener is followed by exactly one JSON object, decoded with a real JSON parser,
    so backticks inside strings (e.g. Markdown file contents) can't end a block early, and a
    missing or misplaced closing fence doesn't lose the call.
    Returns (text, calls, problem); problem describes a malformed/truncated call, else ''.
    """
    found, problem = [], ""  # (start, obj)
    for m in TOOL_OPEN_RE.finditer(answer):
        if any(start <= m.start() < end for start, end, _ in found):
            continue  # an opener inside an earlier call's JSON string
        i = m.end()
        while i < len(answer) and answer[i] in " \t\r\n":
            i += 1
        try:
            obj, end = _json.raw_decode(answer, i)
        except json.JSONDecodeError as e:
            problem = f"invalid JSON in a tool block ({e.msg} at char {e.pos - i}): {answer[i:i + 200]}"
            found.append((m.start(), m.end(), None))
            continue
        close = TOOL_CLOSE_RE.match(answer, end)
        found.append((m.start(), close.end() if close else end, obj))
    for m in CALL_RE.finditer(answer):
        if not any(start <= m.start() < end for start, end, _ in found):
            try:
                found.append((m.start(), m.end(), json.loads(m.group(1))))
            except json.JSONDecodeError as e:
                problem = f"invalid JSON in a tool block ({e.msg} at char {e.pos}): {m.group(1)[:200]}"
                found.append((m.start(), m.end(), None))
    if answer.count("<tool_call>") > len(CALL_RE.findall(answer)):
        problem = "a <tool_call> block was not closed or is not a single JSON object"
    found.sort(key=lambda f: f[0])
    calls = []
    for _, _, obj in found:
        if obj is None:
            continue
        name = obj.get("tool", obj.get("name")) if isinstance(obj, dict) else None
        if not name:
            problem = 'a tool block is missing "tool"'
            continue
        args = obj.get("input", obj.get("arguments", obj.get("parameters", {})))
        calls.append({"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                      "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}})
    if not found:
        return answer, [], problem
    # Keep only text before the first call; drop anything the model invented after it.
    return answer[:found[0][0]].strip(), calls, problem


def extract(raw):
    """Collect answer text from the streamed response, skipping 'thought' parts.

    Gemini Enterprise's root agent sometimes hands the turn to a built-in sub-agent
    (transfer_to_agent, e.g. file_and_coding_agent). Its "I'll hand this off..." chatter
    is dropped; the sub-agent's answer is kept. If the sub-agent wrote no text, its code
    output stands in, and failing that the pre-handoff text is kept.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = [json.loads(l.strip().strip(",[]")) for l in raw.splitlines() if l.strip().strip(",[]")]
    chunks = data if isinstance(data, list) else [data]
    out, before, code_out, reasons, handoffs = [], [], [], [], set()

    def walk(x):
        if isinstance(x, dict):
            c = x.get("content")
            if isinstance(c, dict):
                if isinstance(c.get("text"), str) and not c.get("thought"):
                    out.append(c["text"])
                r = c.get("codeExecutionResult")
                if isinstance(r, dict) and isinstance(r.get("output"), str):
                    code_out.append(r["output"])
            fc = x.get("functionCall")
            # The final summary chunk repeats earlier calls, so count each handoff once.
            if isinstance(fc, dict) and fc.get("functionName") == "transfer_to_agent" \
                    and fc.get("functionId") not in handoffs:
                handoffs.add(fc.get("functionId"))
                print(f"upstream handed off to {(fc.get('args') or {}).get('agent_name', '?')}", flush=True)
                before.extend(out)
                out.clear()
                code_out.clear()
            if isinstance(x.get("modelArmorViolation"), str):
                reasons.append(x["modelArmorViolation"])
            for k, v in x.items():
                if k != "content":
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for chunk in chunks:
        walk(chunk)
    parts = out or ([o.strip() for o in code_out[-1:]] if code_out else before)
    # Drop the web app's display-only "[[section:Title]]" markers.
    text = re.sub(r"\[\[section:[^\]\n]*\]\]\n?", "", "".join(parts))
    if reasons:  # say which filter fired, so a blocked stop isn't a mystery
        text += "\n(Filter reason: " + "; ".join(dict.fromkeys(reasons)) + ")"
    return text


async def api(url, body=None, b64=None, content_type="application/json", headers=None):
    """POST from inside the logged-in page; signs in again once if the session has expired."""
    args = {"url": url, "body": body, "bodyB64": b64, "contentType": content_type,
            "extraHeaders": headers or {}, "authuser": AUTHUSER}
    for attempt in range(2):
        try:
            r = await state["page"].evaluate(CALL_JS, {**args, "serverToken": state["server_token"]})
        except Exception as e:
            if attempt or "closed" not in str(e).lower():
                raise
            print(f"browser was gone ({type(e).__name__}); relaunching it", flush=True)
            await open_browser(headless=HEADLESS)
            await state["page"].goto(HOME, wait_until="domcontentloaded")
            await wait_ready(state["page"], 30)
            continue
        if r["status"] in (401, 403) and attempt == 0:
            print(f"session expired (HTTP {r['status']}); signing in again", flush=True)
            await sign_in()
            continue
        return r


async def discover_models():
    """Ask Gemini Enterprise which models this org's web app offers ({id: display name})."""
    body = {"configId": CONFIG_ID, "additionalParams": {"token": "-", "origin": "ORIGIN_UNSPECIFIED"},
            "widgetConfigId": CONFIG_ID, "modelInfoView": "END_USER_WEB"}
    r = await api(f"{API}/v1alpha/locations/us/lookupWidgetConfig?key={WEB_KEY}", json.dumps(body))

    def find(x):
        if isinstance(x, dict):
            if isinstance(x.get("resolvedModels"), list):
                return x["resolvedModels"]
            x = list(x.values())
        if isinstance(x, list):
            for v in x:
                hit = find(v)
                if hit:
                    return hit
        return None

    try:
        found = find(json.loads(r["text"])) if r["status"] == 200 else None
    except json.JSONDecodeError:
        found = None
    state["models"] = {m["modelId"]: m.get("displayName", "") for m in found or [] if m.get("modelId")}
    print("models:", ", ".join(state["models"]) or f"(lookup failed: HTTP {r['status']}; using defaults)", flush=True)


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
        await discover_models()
        print(f"Bridge on http://127.0.0.1:{PORT}/v1", flush=True)
        yield
        await state["ctx"].close()


app = FastAPI(lifespan=lifespan)


@app.get("/v1/models")
async def models():
    found = state.get("models") or {}
    ids = dict.fromkeys([DEFAULT_MODEL, *found] if found else
                        [DEFAULT_MODEL, "gemini-4-hydrogen", "gemini-4-carbon", "claude-opus-5-5", "claude-sonnet-5-5"])
    return {"object": "list", "data": [{"id": i, "object": "model", "owned_by": "gemini-enterprise",
                                        **({"name": found[i]} if found.get(i) else {})} for i in ids]}


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
    m = re.match(r"(.+)-(minimal|low|medium|high|xhigh|max)$", model)
    if m:  # also accept effort as a model-name suffix, e.g. gemini-4-hydrogen-high
        model, effort = m.group(1), effort or m.group(2)
    effort = {"xhigh": "high", "max": "high"}.get(effort, effort)
    if model.startswith("gemini-") and effort == "minimal":
        effort = "low"  # Gemini rejects MINIMAL with HTTP 500; LOW is its lowest level
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
        level, note, reasks = 0, "", 0
        for attempt in range(7):
            prompt, attachments = render(trim_history(messages, level), tools)
            answer = await ask(prompt + note, model, attachments, effort)
            text, calls, problem = split_calls(answer) if tools else (answer, [], "")
            if BLOCKED in answer:
                (HERE / "blocked_prompt.txt").write_text(prompt, encoding="utf-8")  # for diagnosing the filter
                # The filter judged the model's reply, not the prompt: replies vary, so just ask again
                # with the full history (up to twice) before giving up context.
                if "response violated" in answer and reasks < 2:
                    reasks += 1
                    print(f"reply blocked by content filter; asking again ({reasks}/2)", flush=True)
                    continue
                # Level 2 drops this turn's tool results; only do that when there are none, or the
                # model forgets what it just found and loops on the same tool calls.
                if level == 2 or (level == 1 and messages[-1]["role"] != "user"):
                    break
                level, note = level + 1, ""
                print(f"blocked by content filter{answer[answer.find('(Filter reason'):][:150] if '(Filter reason' in answer else ''}; "
                      f"retrying with less history (level {level})", flush=True)
                continue
            if not problem or attempt == 6:
                break
            print(f"retrying ({problem[:120]})", flush=True)
            note = (f"\n\n(Note: {problem}. Send your full reply again, with every ```tool block holding one "
                    "valid JSON object on one line; arrays must use [ ].)")
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
    uvicorn.run(app, host=HOST, port=PORT)
