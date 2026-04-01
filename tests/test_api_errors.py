"""API error-handler regressions."""

from __future__ import annotations

import asyncio
import json
import unittest

from fastapi import Request

from agp.api.errors import generic_exception_handler


class ApiErrorsTest(unittest.TestCase):
    def test_generic_exception_handler_masks_client_error_message(self) -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/jobs",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
            "http_version": "1.1",
        }
        request = Request(scope)

        with self.assertLogs("agp.api.errors", level="ERROR") as logs:
            response = asyncio.run(generic_exception_handler(request, RuntimeError("secret db path")))

        self.assertEqual(response.status_code, 500)
        payload = json.loads(response.body)
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(payload["error"]["message"], "internal server error")
        self.assertFalse(payload["error"]["retryable"])
        self.assertNotIn("secret db path", response.body.decode("utf-8"))
        self.assertTrue(any("Unhandled exception while serving GET /jobs" in entry for entry in logs.output))
