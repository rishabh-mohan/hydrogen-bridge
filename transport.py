"""Serialize browser calls and abort their fetches when Python stops waiting."""
import asyncio
import json
import os
import uuid

import anyio


UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "600"))


CALL_JS = """
async ({url, body, bodyB64, contentType, extraHeaders, authuser, serverToken, requestId, timeoutMs}) => {
  const controller = new AbortController();
  const requests = globalThis.__bridgeFetches ??= new Map();
  requests.set(requestId, controller);
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  try {
    const m = document.cookie.match(/(?:^|; )SAPISID=([^;]+)/);
    if (!m) return {status: 401, text: 'signed out'};
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
    controller.signal.throwIfAborted();
    const r = await fetch(url, {method: 'POST', headers, body: payload,
                              credentials: 'include', signal: controller.signal});
    return {status: r.status, text: await r.text(),
            uploadUrl: r.headers.get('x-goog-upload-url'), retryAfter: r.headers.get('retry-after')};
  } catch (e) {
    return {status: 0, text: '', failure: timedOut ? 'timeout' :
            controller.signal.aborted ? 'cancelled' : 'network'};
  } finally {
    clearTimeout(timer);
    requests.delete(requestId);
  }
}
"""


class BridgeError(RuntimeError):
    def __init__(self, message, kind="upstream_error", status=502, *, retryable=False,
                 upstream_status=None, retry_after=None):
        super().__init__(message)
        self.kind, self.status = kind, status
        self.retryable, self.upstream_status = retryable, upstream_status
        self.retry_after = retry_after

    def payload(self):
        error = {"message": str(self), "type": self.kind, "retryable": self.retryable}
        if self.upstream_status is not None:
            error["upstream_status"] = self.upstream_status
        return {"error": error}


def http_error(response):
    status = response["status"]
    if status == 401:
        kind, code, message, retryable = "authentication_error", 401, "The Google session is not signed in.", False
    elif status == 403:
        kind, code, message, retryable = "permission_denied", 403, "Google denied access to this request.", False
    elif status == 429:
        kind, code, message, retryable = "rate_limit_error", 429, "Google rate-limited this request. Try again later.", True
    elif status in (408, 504):
        kind, code, message, retryable = "upstream_timeout", 504, "Google timed out processing this request.", True
    else:
        kind, code, message, retryable = "upstream_error", 502, f"Google returned HTTP {status}.", status >= 500
    return BridgeError(message, kind, code, retryable=retryable, upstream_status=status,
                       retry_after=response.get("retryAfter") if status == 429 else None)


def needs_login(response):
    if response["status"] == 401:
        return True
    if response["status"] != 403:
        return False
    # Permission-denied 403s should never open a sign-in window.
    try:
        return json.loads(response["text"]).get("error", {}).get("status") == "UNAUTHENTICATED"
    except (ValueError, TypeError, AttributeError):
        return False


ABORT_JS = """id => { globalThis.__bridgeFetches?.get(id)?.abort(); }"""


class BrowserTransport:
    def __init__(self, state, call_js, recover):
        self.state, self.call_js, self.recover = state, call_js, recover
        # Recovery and fetches use one lane: navigation cannot interrupt another call.
        self.lock = asyncio.Lock()

    async def _recover(self, browser_gone):
        try:
            await self.recover(browser_gone)
        except BridgeError:
            raise
        except Exception as exc:
            print(f"browser recovery failed: {type(exc).__name__}", flush=True)
            raise BridgeError("Browser recovery failed; check the bridge log." if browser_gone
                              else "Sign-in could not complete; check the bridge log.",
                              "browser_unavailable" if browser_gone else "authentication_error",
                              503 if browser_gone else 401, retryable=browser_gone) from exc

    async def _abort(self, page, request_id):
        # Starlette may cancel inside an AnyIO cancel scope. Shield cleanup so the
        # browser fetch is stopped before the shared lane is released.
        with anyio.move_on_after(3, shield=True):
            try:
                await page.evaluate(ABORT_JS, request_id)
            except Exception as exc:
                print(f"fetch cleanup failed: {type(exc).__name__}", flush=True)

    async def request(self, args):
        async with self.lock:
            page = self.state.get("page")
            if page is None or page.is_closed():
                await self._recover(True)
            for attempt in range(2):
                page = self.state["page"]
                request_id = uuid.uuid4().hex
                try:
                    response = await asyncio.wait_for(page.evaluate(self.call_js, {
                        **args, "serverToken": self.state.get("server_token"),
                        "requestId": request_id, "timeoutMs": int(UPSTREAM_TIMEOUT * 1000),
                    }), timeout=UPSTREAM_TIMEOUT + 5)
                except asyncio.CancelledError:
                    await self._abort(page, request_id)
                    raise
                except TimeoutError as exc:
                    await self._abort(page, request_id)
                    raise BridgeError("The upstream request exceeded its time limit.",
                                      "upstream_timeout", 504, retryable=True) from exc
                except Exception as exc:
                    await self._abort(page, request_id)
                    gone = page.is_closed() or "closed" in str(exc).lower()
                    if gone:
                        await self._recover(True)
                    # Once sent, a failed POST has unknown outcome. Do not replay it.
                    raise BridgeError("The browser became unavailable." if gone else "The browser request failed.",
                                      "browser_unavailable" if gone else "browser_error",
                                      503 if gone else 502, retryable=True) from exc
                if response.get("failure"):
                    if response["failure"] == "cancelled":
                        raise BridgeError("The browser request was cancelled.", "request_cancelled", 499)
                    kind = "upstream_timeout" if response["failure"] == "timeout" else "network_error"
                    raise BridgeError("The upstream request timed out." if kind == "upstream_timeout"
                                      else "The browser could not reach Google.",
                                      kind, 504 if kind == "upstream_timeout" else 502, retryable=True)
                if needs_login(response):
                    if attempt == 0:
                        await self._recover(False)
                        continue
                    raise BridgeError("Google still requires sign-in after session recovery.",
                                      "authentication_error", 401, upstream_status=response["status"])
                if response["status"] >= 400:
                    raise http_error(response)
                return response
