"""Exercise the real aiohttp transport against a local HTTPS server."""

import asyncio
import json
import ssl
import types
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from aiohttp.test_utils import TestServer
from jsonschema import Draft202012Validator
import trustme

from test_homefleet import ROOT, api, collector, fake_hass


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.received_bytes = []
        self.status = 204
        self.response_body = ""
        self.block = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

        async def receive(request):
            raw_body = await request.read()
            self.received_bytes.append(len(raw_body))
            self.received.append((request.path, dict(request.headers), json.loads(raw_body) if raw_body else None))
            self.started.set()
            if self.block:
                await self.release.wait()
            if self.status == 302:
                return web.Response(status=302, headers={"Location": "/redirect-target"})
            return web.Response(status=self.status, text=self.response_body)

        ca = trustme.CA()
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ca.issue_cert("127.0.0.1").configure_cert(server_context)
        client_context = ssl.create_default_context()
        ca.configure_trust(client_context)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", receive)
        self.server = TestServer(app, scheme="https")
        await self.server.start_server(ssl=server_context)
        self.session = ClientSession(connector=TCPConnector(ssl=client_context))
        self.hass = types.SimpleNamespace(session=self.session)
        self.url = str(self.server.make_url("/"))
        self.key = "a" * 64

    async def asyncTearDown(self):
        self.release.set()
        await self.session.close()
        await self.server.close()

    async def test_204_sends_exact_payload_and_key_only_in_header(self):
        payload = {"isComplete": True, "monitoredEntities": [], "inventory": []}
        await api.request(self.hass, self.url, self.key, "POST", "/api/ha-integration/lifechecks", payload)
        path, headers, body = self.received[0]
        self.assertEqual("/api/ha-integration/lifechecks", path)
        self.assertNotIn(self.key, path)
        self.assertEqual(self.key, headers["X-Integration-Key"])
        self.assertEqual(payload, body)

    async def test_401_is_distinct_from_other_failures_and_never_retried(self):
        for status, exception in ((401, api.InvalidKeyError), (400, api.BackendUnavailableError),
                                  (500, api.BackendUnavailableError)):
            self.status = status
            before = len(self.received)
            with self.subTest(status=status), self.assertRaises(exception):
                await api.request(self.hass, self.url, self.key, "GET", "/api/ha-integration/connection")
            self.assertEqual(before + 1, len(self.received))

    async def test_oversized_unicode_report_sends_one_incomplete_lifecheck_then_fresh_data(self):
        payload = await collector.collect(fake_hass(), types.SimpleNamespace(data={}, options={}))
        payload["errorMessage"] = "HACS: zdroj zlyhal"
        payload["isComplete"] = False
        payload["monitoredEntities"] = [
            {"entityId": f"sensor.test_{index}", "name": "Ž" * 255, "integrationDomain": "esphome",
             "availability": 1, "state": "😀" * 100 + '\\"'} for index in range(2000)]
        payload["inventory"] = [{"type": 1, "name": "ESPHome", "version": "2026.9.0", "state": None}]
        original = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertGreater(len(original), api.MAX_REPORT_BYTES)
        contract = json.loads((ROOT.parent / "App/Frontend/src/api/model/openapi.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator({"$ref": "#/components/schemas/LifecheckReportRequest",
                                         "components": contract["components"]})
        validator.validate(payload)

        with self.assertLogs(api._LOGGER):
            await api.request(self.hass, self.url, self.key, "POST", "/api/ha-integration/lifechecks", payload)
        self.assertEqual(1, len(self.received))
        _, headers, reduced = self.received[0]
        self.assertEqual("application/json", headers["Content-Type"])
        self.assertLessEqual(self.received_bytes[0], api.MAX_REPORT_BYTES)
        self.assertFalse(reduced["isComplete"])
        self.assertIn("prekročil limit", reduced["errorMessage"])
        self.assertIn("2000", reduced["errorMessage"])
        self.assertIn("HACS: zdroj zlyhal", reduced["errorMessage"])
        self.assertEqual([], reduced["monitoredEntities"])
        self.assertEqual([], reduced["inventory"])
        self.assertEqual(payload["haVersion"], reduced["haVersion"])
        validator.validate(reduced)
        self.assertEqual(2000, len(payload["monitoredEntities"]))

        fresh = await collector.collect(fake_hass(), types.SimpleNamespace(data={}, options={}))
        await api.request(self.hass, self.url, self.key, "POST", "/api/ha-integration/lifechecks", fresh)
        self.assertEqual(2, len(self.received))
        self.assertEqual(fresh, self.received[1][2])
        self.assertTrue(self.received[1][2]["isComplete"])

    async def test_report_size_boundary_uses_exact_encoded_bytes(self):
        payload = await collector.collect(fake_hass(), types.SimpleNamespace(data={}, options={}))
        payload["errorMessage"] = ""
        baseline_size = len(api._encode_report(payload))
        payload["errorMessage"] = "x" * (api.MAX_REPORT_BYTES - baseline_size)
        encoded = api._encode_report(payload)
        self.assertEqual(api.MAX_REPORT_BYTES, len(encoded))
        self.assertEqual(payload, json.loads(encoded))
        payload["errorMessage"] += "x"
        with self.assertLogs(api._LOGGER):
            encoded = api._encode_report(payload)
        self.assertLessEqual(len(encoded), api.MAX_REPORT_BYTES)
        self.assertFalse(json.loads(encoded)["isComplete"])

    async def test_error_status_categories_do_not_expose_response_body(self):
        self.response_body = "private response data " + self.key
        for status, reason in ((400, "invalid_report"), (413, "payload_too_large"),
                               (429, "rate_limited"), (500, "server_error"), (403, "unexpected_status")):
            self.status = status
            before = len(self.received)
            with self.subTest(status=status), self.assertRaises(api.BackendResponseError) as caught:
                await api.request(self.hass, self.url, self.key, "POST", "/report", {})
            self.assertEqual(status, caught.exception.status)
            self.assertEqual(reason, caught.exception.reason)
            self.assertNotIn(self.response_body, repr(caught.exception))
            self.assertNotIn(self.key, repr(caught.exception))
            self.assertEqual(before + 1, len(self.received))

    async def test_redirect_does_not_forward_key_or_follow_location(self):
        self.status = 302
        with self.assertRaises(api.BackendResponseError) as caught:
            await api.request(self.hass, self.url, self.key, "GET", "/api/ha-integration/connection")
        self.assertEqual("redirect_rejected", caught.exception.reason)
        self.assertEqual(1, len(self.received))

    async def test_timeout_and_next_fresh_request(self):
        self.block = True
        with patch.object(api, "ClientTimeout", return_value=ClientTimeout(total=0.1)) as timeout:
            with self.assertRaises(api.BackendUnavailableError) as caught:
                await api.request(self.hass, self.url, self.key, "GET", "/slow")
            self.assertEqual("timeout", caught.exception.reason)
            timeout.assert_called_once_with(total=30)
        self.block = False
        self.release.set()
        await api.request(self.hass, self.url, self.key, "GET", "/fresh")
        self.assertEqual(["/slow", "/fresh"], [item[0] for item in self.received])

    async def test_request_cancellation_propagates(self):
        self.block = True
        task = asyncio.create_task(api.request(self.hass, self.url, self.key, "GET", "/slow"))
        await asyncio.wait_for(self.started.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(1, len(self.received))

    async def test_untrusted_certificate_is_rejected(self):
        async with ClientSession() as untrusted:
            with self.assertRaises(api.BackendUnavailableError) as caught:
                await api.request(types.SimpleNamespace(session=untrusted), self.url, self.key, "GET", "/untrusted")
            self.assertEqual("tls_error", caught.exception.reason)
        self.assertEqual([], self.received)

    async def test_connection_failure_is_reported(self):
        await self.server.close()
        with self.assertRaises(api.BackendUnavailableError) as caught:
            await api.request(self.hass, self.url, self.key, "GET", "/closed")
        self.assertEqual("connection_error", caught.exception.reason)
