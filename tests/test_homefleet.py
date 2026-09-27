"""Portable tests: real validation/HTTP libraries, isolated HA boundary doubles."""

import asyncio
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import voluptuous as vol
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def module(name, **members):
    result = types.ModuleType(name)
    result.__dict__.update(members)
    sys.modules[name] = result
    return result


def install_stubs():
    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            pass

        def _async_current_entries(self):
            return []

        async def async_set_unique_id(self, unique_id):
            self.unique_id = unique_id

        def _abort_if_unique_id_configured(self):
            pass

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        def _get_reconfigure_entry(self):
            return self.config_entry

        def async_update_and_abort(self, entry, **kwargs):
            return {"type": "reconfigure", **kwargs}

    package = module("homeassistant")
    package.__path__ = []
    module("homeassistant.config_entries", ConfigFlow=ConfigFlow, OptionsFlow=ConfigFlow)
    helpers = module("homeassistant.helpers")
    helpers.__path__ = []
    module("homeassistant.helpers.selector", TextSelector=lambda value: str, TextSelectorConfig=lambda **kwargs: kwargs,
           TextSelectorType=types.SimpleNamespace(PASSWORD="password", URL="url"), EntitySelector=lambda value: [str],
           EntitySelectorConfig=lambda **kwargs: kwargs)
    registry = Mock()
    registry.async_get.return_value = None
    module("homeassistant.helpers.entity_registry", async_get=lambda _: registry)
    module("homeassistant.const", STATE_UNAVAILABLE="unavailable", STATE_UNKNOWN="unknown", __version__="2026.9.0")

    class IntegrationNotLoaded(Exception):
        pass

    def get_integration(_, domain):
        return types.SimpleNamespace(manifest={"name": domain.upper(), "version": "1.0"})

    module("homeassistant.loader", IntegrationNotLoaded=IntegrationNotLoaded,
           async_get_loaded_integration=get_integration)
    module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
    module("homeassistant.helpers.event", async_track_time_interval=lambda *args: lambda: None)
    module("homeassistant.helpers.start", async_at_started=lambda *args: lambda: None)
    module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: hass.session)
    integration = module("homefleet")
    integration.__path__ = [str(ROOT / "custom_components" / "homefleet")]
    return registry


REGISTRY = install_stubs()
collector = importlib.import_module("homefleet.collector")
runtime = importlib.import_module("homefleet.__init__")
config_flow = importlib.import_module("homefleet.config_flow")
api = sys.modules["homefleet.api"]


def fake_hass(states=None, components=None, entries=None, data=None):
    states = states or {}
    return types.SimpleNamespace(
        states=types.SimpleNamespace(get=states.get),
        config_entries=types.SimpleNamespace(async_entries=lambda **kwargs: entries or []),
        config=types.SimpleNamespace(components=components or set()),
        data=data or {},
        async_create_task=asyncio.create_task,
    )


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        REGISTRY.async_get.return_value = None
        self.request = AsyncMock()
        self.request_patch = patch.object(runtime, "request", self.request)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)

    async def test_null_state_does_not_mean_missing(self):
        hass = fake_hass({"sensor.null": types.SimpleNamespace(state=None, attributes={}),
                          "sensor.off": types.SimpleNamespace(state="off", attributes={}),
                          "sensor.unavailable": types.SimpleNamespace(state="unavailable", attributes={}),
                          "sensor.unknown": types.SimpleNamespace(state="unknown", attributes={})})
        items, errors = [], []
        await collector.collect_entities(hass, ["sensor.null", "sensor.off", "sensor.unavailable", "sensor.unknown", "sensor.gone"], items, errors)
        self.assertEqual([], errors)
        self.assertEqual([1, 1, 2, 3, 4], [item["availability"] for item in items])
        self.assertIsNone(items[0]["state"])
        self.assertIsNone(items[4]["state"])

    async def test_registry_entity_without_state_is_unknown(self):
        REGISTRY.async_get.return_value = types.SimpleNamespace(name="Exists", config_entry_id="entry")
        hass = fake_hass(entries=[types.SimpleNamespace(entry_id="entry", domain="esphome")])
        items, errors = [], []
        await collector.collect_entities(hass, ["sensor.registered"], items, errors)
        item = items[0]
        self.assertEqual(3, item["availability"])
        self.assertEqual("esphome", item["integrationDomain"])

    async def test_integration_domain_is_reported_once(self):
        hass = fake_hass(components={"esphome", "sensor.esphome"},
                         entries=[types.SimpleNamespace(domain="esphome")])
        items, errors = [], []
        await collector.collect_integrations(hass, items, errors)
        self.assertEqual([], errors)
        self.assertEqual(["ESPHOME"], [item["name"] for item in items])

    async def test_missing_manifest_preserves_other_integrations_and_marks_incomplete(self):
        hass = fake_hass(components={"sensor", "auth"},
                         entries=[types.SimpleNamespace(domain="esphome"), types.SimpleNamespace(domain="z_removed")])
        entry = types.SimpleNamespace(data={"entities": []}, options={})

        def load(_, domain):
            if domain == "z_removed":
                raise FileNotFoundError("missing manifest")
            return types.SimpleNamespace(manifest={"name": "ESPHome"})

        with patch.object(collector, "async_get_loaded_integration", side_effect=load):
            report = await collector.collect(hass, entry)
        self.assertEqual(["ESPHome", "z_removed"], [item["name"] for item in report["inventory"]])
        self.assertIsNone(report["inventory"][1]["version"])
        self.assertFalse(report["isComplete"])
        self.assertIn("HA integrácia [z_removed]", report["errorMessage"])
        self.assertIn("FileNotFoundError", report["errorMessage"])
        self.assertIn("odoslaná ako doména bez verzie", report["errorMessage"])

    async def test_configured_but_unloaded_integration_is_not_an_error(self):
        hass = fake_hass(entries=[types.SimpleNamespace(domain="sleeping", entry_id="entry")])
        with patch.object(collector, "async_get_loaded_integration",
                          side_effect=collector.IntegrationNotLoaded("sleeping")):
            report = await collector.collect(hass, types.SimpleNamespace(data={}, options={}))
        self.assertEqual(["sleeping"], [item["name"] for item in report["inventory"]])
        self.assertIsNone(report["inventory"][0]["version"])
        self.assertTrue(report["isComplete"])
        self.assertIsNone(report["errorMessage"])

    async def test_supervisor_uses_root_info_field_names(self):
        supervisor = types.ModuleType("homeassistant.components.hassio")
        supervisor.get_info = lambda _: {"supervisor": "2026.09.1", "hassos": "17.0"}
        supervisor.get_addons_list = lambda _: [{"name": "Mosquitto", "version": "6.5", "state": "started"}]
        with patch.dict(sys.modules, {"homeassistant.components.hassio": supervisor}):
            report = await collector.collect(fake_hass(data={"hassio": object()}), types.SimpleNamespace(data={}, options={}))
        self.assertEqual("2026.09.1", report["supervisorVersion"])
        self.assertEqual("17.0", report["osVersion"])
        self.assertEqual("started", report["inventory"][0]["state"])

    async def test_optional_sources_and_partial_failure(self):
        hass = fake_hass()
        entry = types.SimpleNamespace(data={"entities": []}, options={})
        self.assertTrue((await collector.collect(hass, entry))["isComplete"])
        hacs = types.SimpleNamespace(repositories=types.SimpleNamespace(list_all=[types.SimpleNamespace(
            data=types.SimpleNamespace(full_name="org/repo", installed_version="2.0", installed=True))]))
        hass.data["hacs"] = hacs
        self.assertEqual("org/repo", (await collector.collect(hass, entry))["inventory"][0]["name"])
        hacs.repositories = None
        report = await collector.collect(hass, entry)
        self.assertFalse(report["isComplete"])
        self.assertIn("HACS", report["errorMessage"])

    async def test_reporter_discards_failure_then_accepts_next_tick(self):
        reporter = runtime.Reporter(fake_hass(), types.SimpleNamespace(
            data={"url": "https://example.test", "integration_key": "a" * 64}, options={}))
        self.request.side_effect = [api.BackendUnavailableError(), None]
        with patch.object(runtime, "collect", side_effect=[{"snapshot": 1}, {"snapshot": 2}]):
            await reporter.send()
            await reporter.send()
        self.assertEqual(2, self.request.await_count)
        self.assertEqual([{"snapshot": 1}, {"snapshot": 2}], [call.args[-1] for call in self.request.await_args_list])

    async def test_stop_cancels_request_and_rejects_already_queued_timer(self):
        reporter = runtime.Reporter(fake_hass(), types.SimpleNamespace(
            data={"url": "https://example.test", "integration_key": "a" * 64}, options={}))
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked(*_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.request.side_effect = blocked
        cancel_timer = Mock()
        cancel_start = Mock()
        reporter._cancel_timer = cancel_timer
        reporter._cancel_start = cancel_start
        reporter._schedule()
        await started.wait()
        await reporter.stop()
        reporter._schedule()
        reporter._started(None)
        await asyncio.sleep(0)
        self.assertTrue(cancelled.is_set())
        cancel_timer.assert_called_once()
        cancel_start.assert_called_once()
        self.assertEqual(1, self.request.await_count)
        self.assertFalse(reporter._tasks)

    async def test_reporter_skips_overlapping_send(self):
        reporter = runtime.Reporter(fake_hass(), types.SimpleNamespace(
            data={"url": "https://example.test", "integration_key": "a" * 64}, options={}))
        gate = asyncio.Event()

        async def blocked(*_):
            await gate.wait()

        self.request.side_effect = blocked
        task = asyncio.create_task(reporter.send())
        await asyncio.sleep(0)
        await reporter.send()
        gate.set()
        await task
        self.assertEqual(1, self.request.await_count)

    async def test_setup_renames_existing_entry_without_changing_domain(self):
        hass = fake_hass()
        hass.data = {}
        hass.config_entries.async_update_entry = Mock(side_effect=lambda entry, **changes: setattr(entry, "title", changes["title"]))
        entry = types.SimpleNamespace(entry_id="entry", title="HomeFleet", data={}, options={},
                                      add_update_listener=Mock(return_value=lambda: None), async_on_unload=Mock())
        with patch.object(runtime.Reporter, "start"):
            self.assertTrue(await runtime.async_setup_entry(hass, entry))
        self.assertEqual("BlackLabs Watchdog", entry.title)
        hass.config_entries.async_update_entry.assert_called_once_with(entry, title="BlackLabs Watchdog")
        self.assertIn(entry.entry_id, hass.data["homefleet"])

    async def test_reporting_starts_after_ha_started_and_uses_five_minutes(self):
        hass = fake_hass()
        entry = types.SimpleNamespace(
            data={"url": "https://example.test", "integration_key": "a" * 64}, options={},
            async_on_unload=lambda _: None)
        reporter = runtime.Reporter(hass, entry)
        callbacks = []
        intervals = []
        with patch.object(runtime, "async_at_started", side_effect=lambda _, cb: callbacks.append(cb) or (lambda: None)), \
             patch.object(runtime, "async_track_time_interval", side_effect=lambda _, cb, interval: intervals.append(interval) or (lambda: None)):
            reporter.start()
            self.assertEqual(0, self.request.await_count)
            callbacks[0](hass)
            await asyncio.gather(*reporter._tasks)
            await reporter.stop()
        self.assertEqual(5 * 60, intervals[0].total_seconds())
        self.assertEqual(1, self.request.await_count)


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = AsyncMock()
        request_patch = patch.object(config_flow, "request", self.request)
        request_patch.start()
        self.addCleanup(request_patch.stop)

    async def test_connection_then_monitoring_and_options(self):
        flow = config_flow.HomeFleetConfigFlow()
        flow.hass = fake_hass()
        connection = {"url": "https://example.test/", "integration_key": "a" * 64}
        result = await flow.async_step_user(connection)
        self.assertEqual("monitoring", result["step_id"])
        result = await flow.async_step_monitoring({"entities": ["sensor.one"], "interval": 5})
        self.assertEqual("create_entry", result["type"])
        self.assertEqual("BlackLabs Watchdog", result["title"])
        self.assertEqual("https://example.test", result["data"]["url"])
        self.assertEqual(["sensor.one"], result["data"]["entities"])
        self.assertEqual("GET", self.request.await_args.args[3])
        options = config_flow.HomeFleetOptionsFlow()
        options.config_entry = types.SimpleNamespace(data=result["data"], options={})
        self.assertEqual("init", (await options.async_step_init())["step_id"])
        self.assertEqual("create_entry", (await options.async_step_init({"entities": [], "interval": 10}))["type"])

    async def test_invalid_key_and_reconfigure(self):
        flow = config_flow.HomeFleetConfigFlow()
        flow.hass = fake_hass()
        self.assertEqual("invalid_key", await config_flow.validate_connection(flow.hass,
            {"url": "https://example.test", "integration_key": "bad"}))
        self.assertEqual("invalid_url", await config_flow.validate_connection(flow.hass,
            {"url": "http://example.test", "integration_key": "a" * 64}))
        flow.config_entry = types.SimpleNamespace(data={"url": "https://old.test", "integration_key": "b" * 64,
                                                     "entities": [], "interval": 5})
        result = await flow.async_step_reconfigure({"url": "https://new.test", "integration_key": "a" * 64})
        self.assertEqual("reconfigure", result["type"])
        self.assertEqual("https://new.test", result["data"]["url"])

    async def test_invalid_urls_return_form_error(self):
        for url in ("https://[bad", "https://example.test:abc", "https://example.test:65536",
                    "https://example.test:0", "https://user:pass@example.test", "https://exa mple.test"):
            with self.subTest(url=url):
                self.assertEqual("invalid_url", await config_flow.validate_connection(fake_hass(),
                    {"url": url, "integration_key": "a" * 64}))
        self.request.assert_not_called()
        self.assertEqual("https://[::1]:443/base", config_flow.valid_url("https://[::1]:443/base/"))

    async def test_two_open_flows_cannot_create_two_entries(self):
        entries = []
        flows = [config_flow.HomeFleetConfigFlow(), config_flow.HomeFleetConfigFlow()]
        for flow in flows:
            flow.hass = fake_hass()
            flow._async_current_entries = lambda: entries
            await flow.async_step_user({"url": "https://example.test", "integration_key": "a" * 64})
        first = await flows[0].async_step_monitoring({"entities": [], "interval": 5})
        entries.append(first)
        second = await flows[1].async_step_monitoring({"entities": [], "interval": 5})
        self.assertEqual("already_configured", second["reason"])

    def test_monitoring_schema_runs_real_validation(self):
        schema = config_flow.monitoring_schema({})
        self.assertEqual({"entities": [], "interval": 5}, schema({}))
        self.assertEqual(1, schema({"interval": 1})["interval"])
        self.assertEqual(config_flow.MAX_INTERVAL, schema({"interval": config_flow.MAX_INTERVAL})["interval"])
        for interval in (0, -1, "bad", config_flow.MAX_INTERVAL + 1, 10**15):
            with self.subTest(interval=interval), self.assertRaises(vol.Invalid):
                schema({"interval": interval})


class ContractTests(unittest.TestCase):
    def test_manifest_and_backend_contract(self):
        manifest = json.loads((ROOT / "custom_components/homefleet/manifest.json").read_text(encoding="utf-8"))
        hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
        contract = json.loads((ROOT.parent / "App/Frontend/src/api/model/openapi.json").read_text(encoding="utf-8"))
        self.assertEqual("homefleet", manifest["domain"])
        self.assertEqual("BlackLabs Watchdog", manifest["name"])
        self.assertEqual("0.1.2", manifest["version"])
        self.assertTrue(hacs["hide_default_branch"])
        self.assertTrue(manifest["single_config_entry"])
        self.assertIn("post", contract["paths"]["/api/ha-integration/lifechecks"])
        self.assertIn("get", contract["paths"]["/api/ha-integration/connection"])
        schemas = contract["components"]["schemas"]
        self.assertEqual([1, 2, 3, 4], schemas["EntityAvailability"]["enum"])
        self.assertEqual([1, 2, 3], schemas["InventoryType"]["enum"])
        self.assertIn("state", schemas["MonitoredEntityReportDto"]["properties"])
        for path, method in (("connection", "get"), ("lifechecks", "post")):
            operation = contract["paths"][f"/api/ha-integration/{path}"][method]
            self.assertIn("204", operation["responses"])
            self.assertNotIn("200", operation["responses"])
            self.assertEqual([{"IntegrationKey": []}], operation["security"])
        scheme = contract["components"]["securitySchemes"]["IntegrationKey"]
        self.assertEqual(("apiKey", "header", "X-Integration-Key"), (scheme["type"], scheme["in"], scheme["name"]))

    def test_collected_payload_validates_against_generated_openapi(self):
        manifest = json.loads((ROOT / "custom_components/homefleet/manifest.json").read_text(encoding="utf-8"))
        contract = json.loads((ROOT.parent / "App/Frontend/src/api/model/openapi.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator({"$ref": "#/components/schemas/LifecheckReportRequest",
                                         "components": contract["components"]})
        REGISTRY.async_get.return_value = None
        hass = fake_hass({"sensor.null": types.SimpleNamespace(state=None, attributes={})}, components={"esphome"})
        entry = types.SimpleNamespace(data={"entities": ["sensor.null", "sensor.missing"]}, options={})
        report = asyncio.run(collector.collect(hass, entry))
        self.assertEqual(manifest["version"], report["integrationVersion"])
        validator.validate(report)
        for changed in ({"inventory": None}, {"monitoredEntities": None}, {"haVersion": "x" * 51},
                        {"inventory": [{"type": 99, "name": "bad"}]},
                        {"monitoredEntities": [{"entityId": "sensor.test", "availability": "Available"}]},
                        {"monitoredEntities": [{"entityId": "sensor.test", "availability": 1, "state": "x" * 256}]}):
            with self.subTest(changed=changed):
                self.assertFalse(validator.is_valid({**report, **changed}))


if __name__ == "__main__":
    unittest.main()
