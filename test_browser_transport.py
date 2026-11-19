"""Real Chromium fetch lifecycle tests against a local HTTP fixture; no login."""
import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import transport as browser
import request_runtime as runtime


class BrowserFetchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.started, self.disconnected = asyncio.Event(), asyncio.Event()
        self.handlers = set()
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        await self.context.add_cookies([{"name": "SAPISID", "value": "fixture", "url": self.base}])
        self.page = await self.context.new_page()
        await self.page.goto(self.base)
        self.recover = AsyncMock()
        self.transport = browser.BrowserTransport({"page": self.page}, browser.CALL_JS, self.recover)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.server.close()
        await self.server.wait_closed()
        for task in self.handlers:
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode()
            for line in headers.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    await reader.readexactly(int(line.split(":", 1)[1]))
            if headers.split(" ")[1] == "/slow":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\nConnection: close\r\n\r\nprefix")
                await writer.drain()
                self.started.set()
                await reader.read()  # fetch.abort() closes the body connection
                self.disconnected.set()
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/html\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)

    def args(self, path):
        return {"url": self.base + path, "body": "test", "contentType": "application/json",
                "extraHeaders": {}, "authuser": "0"}

    async def test_cancel_aborts_response_body_and_next_request_succeeds(self):
        task = asyncio.create_task(self.transport.request(self.args("/slow")))
        await asyncio.wait_for(self.started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(self.disconnected.wait(), 5)
        response = await self.transport.request(self.args("/fast"))
        self.assertEqual(response["text"], "OK")
        self.assertEqual(await self.page.evaluate("globalThis.__bridgeFetches.size"), 0)

    async def test_javascript_deadline_aborts_response_body(self):
        with patch.object(browser, "UPSTREAM_TIMEOUT", 0.2):
            with self.assertRaises(browser.BridgeError) as error:
                await self.transport.request(self.args("/slow"))
        self.assertEqual(error.exception.kind, "upstream_timeout")
        await asyncio.wait_for(self.disconnected.wait(), 5)
        self.assertEqual(await self.page.evaluate("globalThis.__bridgeFetches.size"), 0)
        self.recover.assert_not_awaited()

    async def test_fetch_network_error_does_not_trigger_sign_in(self):
        await self.page.route("**/offline", lambda route: route.abort("connectionrefused"))
        with self.assertRaises(browser.BridgeError) as error:
            await self.transport.request(self.args("/offline"))
        self.assertEqual(error.exception.kind, "network_error")
        self.recover.assert_not_awaited()

    async def test_http_disconnect_aborts_browser_body(self):
        async def complete(payload):
            await self.transport.request(self.args("/slow"))
            self.fail("Disconnected request should never complete")
        req = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        response = await runtime.chat_response(req, {"messages": [{"role": "user", "content": "test"}], "stream": True}, complete)
        async def receive():
            await asyncio.wait_for(self.started.wait(), 5)
            return {"type": "http.disconnect"}
        await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, AsyncMock())
        await asyncio.wait_for(self.disconnected.wait(), 5)
        self.assertFalse(self.transport.lock.locked())


if __name__ == "__main__":
    unittest.main()
