"""HTTP request lifetime, disconnect cleanup, and consistent error responses."""
import asyncio
import json
import os
import time
import uuid

import anyio
from fastapi.responses import JSONResponse, Response, StreamingResponse

from transport import BridgeError


REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "1200"))
KEEPALIVE_SECONDS = 15
DISCONNECT_POLL_SECONDS = 0.25


async def cancel_job(job):
    if not job.done():
        job.cancel()
    with anyio.move_on_after(5, shield=True):
        await asyncio.gather(job, return_exceptions=True)


async def run_completion(complete, payload):
    try:
        # Includes queueing, uploads, sign-in, generation and all retries.
        async with asyncio.timeout(REQUEST_TIMEOUT):
            return await complete(payload)
    except TimeoutError as exc:
        raise BridgeError("The chat request exceeded its total time limit.",
                          "request_timeout", 504, retryable=True) from exc
    except BridgeError:
        raise
    except Exception as exc:
        print(f"chat failed: {type(exc).__name__}", flush=True)
        raise BridgeError("The bridge could not complete this request; check the bridge log.") from exc


async def disconnected(req):
    while not await req.is_disconnected():
        await asyncio.sleep(DISCONNECT_POLL_SECONDS)


class ManagedStream(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also covers a send failure while the generator is suspended at yield.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


def error_response(error):
    headers = {"Retry-After": error.retry_after} if error.retry_after else None
    return JSONResponse(error.payload(), status_code=error.status, headers=headers)


async def chat_response(req, payload, complete):
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list) or not payload["messages"]:
        return JSONResponse({"error": {"message": "messages must be a nonempty array", "type": "invalid_request_error"}}, status_code=400)
    if (any(not isinstance(m, dict) or not isinstance(m.get("role"), str) for m in payload["messages"])
            or any(payload.get(k) is not None and not isinstance(payload[k], str)
                   for k in ("model", "reasoning_effort"))
            or not isinstance(payload.get("tools") or [], list)):
        return JSONResponse({"error": {"message": "Invalid message, model, reasoning_effort, or tools", "type": "invalid_request_error"}}, status_code=400)
    cid, now = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
    if not payload.get("stream"):
        job = asyncio.create_task(run_completion(complete, payload))
        watcher = asyncio.create_task(disconnected(req))
        try:
            await asyncio.wait({job, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher.done():
                return Response(status_code=499)
            model, text, calls, finish, usage = job.result()
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            return {"id": cid, "object": "chat.completion", "created": now, "model": model,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": usage}
        except BridgeError as exc:
            return error_response(exc)
        finally:
            await cancel_job(watcher)
            await cancel_job(job)

    async def sse():
        job = asyncio.create_task(run_completion(complete, payload))
        try:
            while not job.done():
                yield ": keepalive\n\n"
                await asyncio.wait({job}, timeout=KEEPALIVE_SECONDS)
            try:
                model, text, calls, finish, usage = job.result()
            except BridgeError as exc:
                yield f"data: {json.dumps(exc.payload())}\n\n"
                yield "data: [DONE]\n\n"
                return
            base = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": model}
            deltas = [({"role": "assistant", "content": text}, None)]
            if calls:
                deltas.append(({"tool_calls": [{**c, "index": i} for i, c in enumerate(calls)]}, None))
            deltas.append(({}, finish))
            for delta, fin in deltas:
                chunk = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
                if fin:
                    chunk["usage"] = usage
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            await cancel_job(job)

    return ManagedStream(sse(), media_type="text/event-stream")
