"""Tests for RuntimeClient sibling client and heartbeat transport correctness."""
from __future__ import annotations

import unittest

import httpx

from agp.client._runtime import RuntimeClient, RuntimeIdentity, _NonClosingTransport


class NonClosingTransportTest(unittest.TestCase):
    """Verify _NonClosingTransport wraps correctly and suppresses close()."""

    def test_handle_request_delegates_to_inner(self) -> None:
        calls: list[httpx.Request] = []

        class RecordingTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                calls.append(request)
                return httpx.Response(200, json={"ok": True})

        inner = RecordingTransport()
        wrapper = _NonClosingTransport(inner)

        request = httpx.Request("GET", "http://example.com/test")
        response = wrapper.handle_request(request)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url, request.url)
        self.assertEqual(response.status_code, 200)

    def test_close_does_not_close_inner(self) -> None:
        closed = {"value": False}

        class TrackingTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(200)

            def close(self) -> None:
                closed["value"] = True

        inner = TrackingTransport()
        wrapper = _NonClosingTransport(inner)
        wrapper.close()

        self.assertFalse(closed["value"], "close() should NOT propagate to inner transport")

    def test_inner_still_works_after_wrapper_close(self) -> None:
        class CountingTransport(httpx.BaseTransport):
            def __init__(self) -> None:
                self.call_count = 0

            def handle_request(self, request: httpx.Request) -> httpx.Response:
                self.call_count += 1
                return httpx.Response(200)

        inner = CountingTransport()
        wrapper = _NonClosingTransport(inner)

        # Use wrapper, close it, then verify inner still works
        wrapper.handle_request(httpx.Request("GET", "http://example.com/1"))
        wrapper.close()
        inner.handle_request(httpx.Request("GET", "http://example.com/2"))

        self.assertEqual(inner.call_count, 2)


class SiblingClientTest(unittest.TestCase):
    """Verify create_sibling_client() preserves injected transport."""

    def test_sibling_uses_injected_transport(self) -> None:
        """Sibling client should route requests through the injected transport."""
        requests_seen: list[httpx.Request] = []

        class SpyTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                requests_seen.append(request)
                return httpx.Response(200, json={"data": {"ok": True}})

        injected = httpx.Client(
            transport=SpyTransport(),
            base_url="http://testserver",
        )
        identity = RuntimeIdentity(
            runtime_id="rtm-test",
            hostname="localhost",
            server_url="http://testserver",
        )
        rc = RuntimeClient(identity, client=injected)

        sibling = rc.create_sibling_client(timeout=5.0)
        response = sibling.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(requests_seen), 1)
        self.assertIn("/health", str(requests_seen[0].url))

        sibling.close()
        injected.close()

    def test_sibling_close_does_not_break_parent(self) -> None:
        """Closing the sibling must not prevent the parent from making requests."""

        class AlwaysOkTransport(httpx.BaseTransport):
            def __init__(self) -> None:
                self.call_count = 0

            def handle_request(self, request: httpx.Request) -> httpx.Response:
                self.call_count += 1
                return httpx.Response(200, json={"data": {"ok": True}})

        transport = AlwaysOkTransport()
        injected = httpx.Client(transport=transport, base_url="http://testserver")
        identity = RuntimeIdentity(
            runtime_id="rtm-test",
            hostname="localhost",
            server_url="http://testserver",
        )
        rc = RuntimeClient(identity, client=injected)

        sibling = rc.create_sibling_client()
        sibling.get("/before")
        sibling.close()  # This must NOT break the parent

        # Parent client should still work
        response = rc._client.get("/after")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(transport.call_count, 2)

        injected.close()

    def test_owned_client_creates_independent_sibling(self) -> None:
        """When no client is injected, sibling gets its own transport."""
        identity = RuntimeIdentity(
            runtime_id="rtm-test",
            hostname="localhost",
            server_url="http://127.0.0.1:19999",  # Non-routable, just for construction
        )
        rc = RuntimeClient(identity)

        sibling = rc.create_sibling_client()

        # Should be a different client object with independent transport
        self.assertIsNot(sibling, rc._client)
        # The transport should NOT be wrapped (independent, not shared)
        self.assertNotIsInstance(sibling._transport, _NonClosingTransport)

        sibling.close()
        rc.close()

    def test_sibling_preserves_auth_headers(self) -> None:
        """Sibling client should include the auth token in headers."""
        seen_headers: list[dict] = []

        class HeaderCapture(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                seen_headers.append(dict(request.headers))
                return httpx.Response(200, json={"data": {}})

        injected = httpx.Client(transport=HeaderCapture(), base_url="http://testserver")
        identity = RuntimeIdentity(
            runtime_id="rtm-test",
            hostname="localhost",
            server_url="http://testserver",
            token="secret-token-123",
        )
        rc = RuntimeClient(identity, client=injected)

        sibling = rc.create_sibling_client()
        sibling.get("/test")

        self.assertEqual(len(seen_headers), 1)
        self.assertEqual(seen_headers[0].get("authorization"), "Bearer secret-token-123")

        sibling.close()
        injected.close()


class HeartbeatTransportIntegrationTest(unittest.TestCase):
    """Verify supervisor heartbeat uses the correct transport."""

    def test_heartbeat_thread_uses_client_transport(self) -> None:
        """Heartbeat requests must go through the RuntimeClient's transport,
        not a raw httpx.Client hitting the network."""
        from agp.plugins.inprocess import DefaultAgentAdapter, InProcessTerminalHost
        from agp.runtime._supervisor import RuntimeSupervisor
        from agp.runtime._types import (
            ArtifactPayload,
            ExecutionResult,
        )

        heartbeat_urls: list[str] = []

        class SpyTransport(httpx.BaseTransport):
            """Records all request URLs to prove they went through this transport."""
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                heartbeat_urls.append(str(request.url))
                path = request.url.raw_path.decode()
                if "/runtimes/register" in path:
                    return httpx.Response(200, json={"ok": True, "data": {"runtime_id": "rtm-hb-test"}})
                if "/runs/claim" in path:
                    return httpx.Response(200, json={"ok": True, "data": {
                        "claimed": True,
                        "agent_id": "agt-hb",
                        "job": {"job_id": "job-hb", "status": "running"},
                        "run": {"run_id": "run-hb"},
                        "lease": {"lease_id": "lease-hb", "fencing_token": 1},
                        "message": {"text": "test task", "metadata": {}},
                    }})
                if "/heartbeat" in path:
                    return httpx.Response(200, json={"ok": True, "data": {"interrupt_requested": False}})
                if "/progress" in path:
                    return httpx.Response(200, json={"ok": True, "data": {"status": "ok"}})
                if "/complete" in path:
                    return httpx.Response(200, json={"ok": True, "data": {"job_status": "completed"}})
                if "/agents/up" in path:
                    return httpx.Response(200, json={"ok": True, "data": {"agent_id": "agt-hb", "status": "idle"}})
                # Default: 200 OK
                return httpx.Response(200, json={"ok": True, "data": {}})

        transport = SpyTransport()
        injected = httpx.Client(transport=transport, base_url="http://testserver")
        identity = RuntimeIdentity(
            runtime_id="rtm-hb-test",
            hostname="localhost",
            server_url="http://testserver",
        )
        rc = RuntimeClient(identity, client=injected)

        class FastAdapter(DefaultAgentAdapter):
            @property
            def kind(self) -> str:
                return "fast"

            def execute_run(self, *, host, session, claimed, supervisor):
                # Let at least one heartbeat fire
                from time import sleep
                sleep(0.3)
                return ExecutionResult(
                    artifacts=[ArtifactPayload(role="result", name="result.txt", content="done")],
                )

        host = InProcessTerminalHost()
        supervisor = RuntimeSupervisor(
            rc, host=host, adapter=FastAdapter(),
        )
        payload = supervisor.run_once(
            agent_id="agt-hb",
            heartbeat_interval_seconds=0.1,
            lease_ttl_seconds=30,
        )

        self.assertTrue(payload["claimed"])
        # Heartbeat URLs must go through our SpyTransport
        hb_urls = [u for u in heartbeat_urls if "/heartbeat" in u]
        self.assertGreater(len(hb_urls), 0, "At least one heartbeat should have fired")
        # All heartbeat URLs should be routed through testserver, not a real network
        for url in hb_urls:
            self.assertIn("testserver", url)

        injected.close()
