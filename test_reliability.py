import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.requests import ClientDisconnect

import request_runtime as runtime
import transport as browser


OK = {"status": 200, "text": "OK"}
RESULT = ("gemini-test", "OK", [], "stop", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = SimpleNamespace(is_closed=lambda: False, evaluate=AsyncMock(return_value=OK))
        self.state = {"page": self.page, "server_token": None}
        self.recover = AsyncMock()
        self.transport = browser.BrowserTransport(self.state, browser.CALL_JS, self.recover)

    async def test_authentication_retried_once(self):
        self.page.evaluate.side_effect = [{"status": 401, "text": ""}, OK]
        self.assertEqual(await self.transport.request({}), OK)
        self.recover.assert_awaited_once_with(False)

    async def test_persistent_auth_failure_stops(self):
        self.page.evaluate.return_value = {"status": 401, "text": ""}
        with self.assertRaises(browser.BridgeError) as error:
            await self.transport.request({})
        self.assertEqual(error.exception.kind, "authentication_error")
        self.assertEqual(self.page.evaluate.await_count, 2)
        self.recover.assert_awaited_once()

    async def test_failed_sign_in_has_authentication_error(self):
        self.page.evaluate.return_value = {"status": 401, "text": ""}
        self.recover.side_effect = RuntimeError("login window closed")
        with self.assertRaises(browser.BridgeError) as error:
            await self.transport.request({})
        self.assertEqual(error.exception.kind, "authentication_error")
        self.assertEqual(self.page.evaluate.await_count, 1)

    async def test_permission_denial_does_not_open_login(self):
        self.page.evaluate.return_value = {"status": 403, "text": '{"error":{"status":"PERMISSION_DENIED"}}'}
        with self.assertRaises(browser.BridgeError) as error:
            await self.transport.request({})
        self.assertEqual(error.exception.status, 403)
        self.recover.assert_not_awaited()

    async def test_403_with_explicit_auth_status_recovers(self):
        self.page.evaluate.side_effect = [{"status": 403, "text": '{"error":{"status":"UNAUTHENTICATED"}}'}, OK]
        self.assertEqual(await self.transport.request({}), OK)
        self.recover.assert_awaited_once_with(False)

    async def test_network_failure_does_not_replay_or_login(self):
        self.page.evaluate.return_value = {"status": 0, "failure": "network"}
        with self.assertRaises(browser.BridgeError) as error:
            await self.transport.request({})
        self.assertEqual(error.exception.kind, "network_error")
        self.assertEqual(self.page.evaluate.await_count, 1)
        self.recover.assert_not_awaited()

    async def test_rate_limit_keeps_retry_after(self):
        self.page.evaluate.return_value = {"status": 429, "retryAfter": "12"}
        with self.assertRaises(browser.BridgeError) as error:
            await self.transport.request({})
        response = runtime.error_response(error.exception)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "12")
        self.recover.assert_not_awaited()

    async def test_cancel_aborts_browser_fetch_and_frees_lane(self):
        started, aborted = asyncio.Event(), asyncio.Event()
        async def evaluate(script, arg):
            if script == browser.ABORT_JS:
                aborted.set()
                return
            started.set()
            await asyncio.Event().wait()
        self.page.evaluate.side_effect = evaluate
        job = asyncio.create_task(self.transport.request({}))
        await started.wait()
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertTrue(aborted.is_set())
        self.assertFalse(self.transport.lock.locked())

    async def test_waiting_request_cancelled_without_sending(self):
        async with self.transport.lock:
            job = asyncio.create_task(self.transport.request({}))
            await asyncio.sleep(0)
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
        self.page.evaluate.assert_not_awaited()

    async def test_chat_deadline_includes_queue_time(self):
        async with self.transport.lock:
            with patch.object(runtime, "REQUEST_TIMEOUT", 0.01):
                with self.assertRaises(browser.BridgeError) as error:
                    await runtime.run_completion(lambda _: self.transport.request({}), {})
        self.assertEqual(error.exception.kind, "request_timeout")
        self.page.evaluate.assert_not_awaited()

    async def test_recovery_cannot_interrupt_another_fetch(self):
        first_started, release_first = asyncio.Event(), asyncio.Event()
        count = 0
        async def evaluate(script, arg):
            nonlocal count
            count += 1
            if count == 1:
                first_started.set()
                await release_first.wait()
                return OK
            return {"status": 401, "text": ""} if count == 2 else OK
        self.page.evaluate.side_effect = evaluate
        first = asyncio.create_task(self.transport.request({}))
        await first_started.wait()
        second = asyncio.create_task(self.transport.request({}))
        await asyncio.sleep(0)
        self.recover.assert_not_awaited()
        self.assertEqual(count, 1)
        release_first.set()
        self.assertEqual(await asyncio.gather(first, second), [OK, OK])
        self.recover.assert_awaited_once()

    async def test_closed_page_recovered_before_sending(self):
        self.page.is_closed = lambda: True
        await self.transport.request({})
        self.recover.assert_awaited_once_with(True)

    async def test_failed_evaluation_is_not_replayed(self):
        self.page.evaluate.side_effect = [RuntimeError("Target closed"), None]
        with self.assertRaises(browser.BridgeError) as error:
            await self.transport.request({})
        self.assertEqual(error.exception.kind, "browser_unavailable")
        self.recover.assert_awaited_once_with(True)
        self.assertEqual(self.page.evaluate.await_count, 2)  # request and abort, no replay

    async def test_python_fetch_deadline_aborts(self):
        async def evaluate(script, arg):
            if script != browser.ABORT_JS:
                await asyncio.Event().wait()
        self.page.evaluate.side_effect = evaluate
        # Patch wait_for rather than waiting the production five-second grace.
        original = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            return await original(awaitable, timeout=0.01)
        with patch.object(browser.asyncio, "wait_for", side_effect=short_wait):
            with self.assertRaises(browser.BridgeError) as error:
                await self.transport.request({})
        self.assertEqual(error.exception.kind, "upstream_timeout")
        self.assertEqual(self.page.evaluate.await_count, 2)
        self.assertFalse(self.transport.lock.locked())


class RequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.req = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        self.payload = {"messages": [{"role": "user", "content": "hello"}]}

    async def test_nonstream_disconnect_cancels_work(self):
        cancelled = asyncio.Event()
        async def complete(payload):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.req.is_disconnected.return_value = True
        response = await runtime.chat_response(self.req, self.payload, complete)
        self.assertEqual(response.status_code, 499)
        self.assertTrue(cancelled.is_set())

    async def test_deadline_returns_structured_error(self):
        async def complete(payload):
            await asyncio.Event().wait()
        with patch.object(runtime, "REQUEST_TIMEOUT", 0.01):
            response = await runtime.chat_response(self.req, self.payload, complete)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(json.loads(response.body)["error"]["type"], "request_timeout")

    async def test_unexpected_exception_is_not_exposed(self):
        response = await runtime.chat_response(self.req, self.payload, AsyncMock(side_effect=ValueError("private data")))
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private data", response.body.decode())

    async def test_keepalive_precedes_completion(self):
        finish = asyncio.Event()
        async def complete(payload):
            await finish.wait()
            return RESULT
        response = await runtime.chat_response(self.req, {**self.payload, "stream": True}, complete)
        iterator = response.body_iterator
        self.assertEqual(await anext(iterator), ": keepalive\n\n")
        finish.set()
        chunks = [chunk async for chunk in iterator]
        self.assertIn('"finish_reason": "stop"', ''.join(chunks))
        self.assertIn('"usage":', ''.join(chunks))

    async def test_stream_close_cancels_work(self):
        cancelled = asyncio.Event()
        async def complete(payload):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        response = await runtime.chat_response(self.req, {**self.payload, "stream": True}, complete)
        await anext(response.body_iterator)
        await asyncio.sleep(0)
        await response.body_iterator.aclose()
        self.assertTrue(cancelled.is_set())

    async def test_stream_error_is_structured_and_terminates(self):
        complete = AsyncMock(side_effect=browser.BridgeError("Rate limited", "rate_limit_error", 429))
        response = await runtime.chat_response(self.req, {**self.payload, "stream": True}, complete)
        chunks = [chunk async for chunk in response.body_iterator]
        self.assertIn('"type": "rate_limit_error"', ''.join(chunks))
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")

    async def test_send_failure_closes_suspended_generator(self):
        cancelled = asyncio.Event()
        async def complete(payload):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        response = await runtime.chat_response(self.req, {**self.payload, "stream": True}, complete)
        async def send(message):
            if message["type"] == "http.response.body":
                await asyncio.sleep(0)  # let the completion start
                raise OSError("client disconnected")
        with self.assertRaises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
        self.assertTrue(cancelled.is_set())

    async def test_asgi_disconnect_cancels_work_inside_anyio_scope(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def complete(payload):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        response = await runtime.chat_response(self.req, {**self.payload, "stream": True}, complete)
        async def receive():
            await started.wait()
            return {"type": "http.disconnect"}
        await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, AsyncMock())
        self.assertTrue(cancelled.is_set())

    async def test_policy_block_has_distinct_error_type(self):
        import bridge
        with patch.object(bridge, "ask", AsyncMock(return_value=bridge.BLOCKED)), patch.object(bridge.Path, "write_text"):
            response = await runtime.chat_response(self.req, self.payload, bridge.complete)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.body)["error"]["type"], "content_filter_error")

    async def test_usage_is_estimated(self):
        import bridge
        with patch.object(bridge, "ask", AsyncMock(return_value="a" * 40)), patch.object(bridge.Path, "write_text"):
            response = await runtime.chat_response(self.req, self.payload, bridge.complete)
        usage = json.loads(response.body)["usage"] if hasattr(response, "body") else response["usage"]
        self.assertEqual(usage["completion_tokens"], 10)
        self.assertGreater(usage["prompt_tokens"], 0)
        self.assertEqual(usage["total_tokens"], usage["prompt_tokens"] + 10)
        self.assertEqual(bridge.est_tokens("한국"), 2)

    async def test_failed_model_lookup_does_not_block_startup(self):
        import bridge
        error = browser.BridgeError("denied", "permission_denied", 403, upstream_status=403)
        with patch.object(bridge, "api", AsyncMock(side_effect=error)):
            await bridge.discover_models()
        self.assertEqual(bridge.state["models"], {})
        self.assertIn(bridge.DEFAULT_MODEL, [m["id"] for m in (await bridge.models())["data"]])

    async def test_invalid_payload_is_rejected(self):
        complete = AsyncMock()
        for payload in (None, {}, {"messages": []}, {"messages": [1]}, {**self.payload, "model": 123},
                        {**self.payload, "tools": {"type": "function"}}):
            response = await runtime.chat_response(self.req, payload, complete)
            self.assertEqual(response.status_code, 400)
        complete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
