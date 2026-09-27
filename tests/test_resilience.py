"""Fault injection for partial snapshots, UTF-16 limits and recovery."""

import asyncio
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_homefleet import REGISTRY, api, collector, fake_hass, runtime


def entry(entities=None):
    return types.SimpleNamespace(data={"entities": entities or [], "url": "https://example.test",
                                      "integration_key": "a" * 64}, options={})


def state(value="ok", name=None):
    return types.SimpleNamespace(state=value, attributes={"friendly_name": name})


def repository(name):
    return types.SimpleNamespace(data=types.SimpleNamespace(full_name=name, installed_version="1.0", installed=True))


def supervisor(info=None, addons=None):
    module = types.ModuleType("homeassistant.components.hassio")
    module.get_info = lambda _: info if info is not None else {"supervisor": "2026.09.1", "hassos": "17.0"}
    module.get_addons_list = lambda _: addons or []
    return module


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        REGISTRY.async_get.return_value = None

    async def test_entity_failure_does_not_drop_earlier_or_later_entities(self):
        selected = ["sensor.first", "sensor.broken", "sensor.last"]
        hass = fake_hass({entity_id: state() for entity_id in selected})

        def lookup(entity_id):
            if entity_id == "sensor.broken":
                raise RuntimeError("sensitive source data")
            return None

        with patch.object(REGISTRY, "async_get", side_effect=lookup):
            report = await collector.collect(hass, entry(selected))
        self.assertEqual(["sensor.first", "sensor.last"], [item["entityId"] for item in report["monitoredEntities"]])
        self.assertFalse(report["isComplete"])
        self.assertNotIn("sensitive source data", report["errorMessage"])

    async def test_hacs_bad_item_does_not_drop_other_repositories(self):
        hacs = types.SimpleNamespace(repositories=types.SimpleNamespace(
            list_all=[repository("org/first"), repository(None), repository("org/last")]))
        report = await collector.collect(fake_hass(data={"hacs": hacs}), entry())
        self.assertEqual(["org/first", "org/last"], [item["name"] for item in report["inventory"]])
        self.assertFalse(report["isComplete"])

    async def test_hacs_installed_filter_failure_is_isolated_per_repository(self):
        class Repositories:
            @property
            def list_downloaded(self):
                raise AssertionError("Bulk filter must not be used")

            @property
            def list_all(self):
                return [repository("org/first"), types.SimpleNamespace(data=None),
                        types.SimpleNamespace(data=types.SimpleNamespace(installed=False)), repository("org/last")]

        hacs = types.SimpleNamespace(repositories=Repositories())
        report = await collector.collect(fake_hass(data={"hacs": hacs}), entry())
        self.assertEqual(["org/first", "org/last"], [item["name"] for item in report["inventory"]])
        self.assertFalse(report["isComplete"])
        self.assertIn("HACS: položka 2", report["errorMessage"])
        self.assertNotIn("položka 3", report["errorMessage"])

    async def test_addon_bulk_serialization_failure_recovers_healthy_models(self):
        models = [Mock(to_dict=Mock(return_value={"name": "first"})),
                  Mock(to_dict=Mock(side_effect=ValueError("private model data"))),
                  Mock(to_dict=Mock(return_value={"name": "last", "state": "started"}))]
        module = supervisor()
        # Match HA's eager public helper, not an already serialized list.
        module.get_addons_list = lambda hass: [addon.to_dict() for addon in hass.data["hassio_addons_list"]]
        constants = types.ModuleType("homeassistant.components.hassio.const")
        constants.DATA_ADDONS_LIST = "hassio_addons_list"
        with patch.dict(sys.modules, {"homeassistant.components.hassio": module,
                                      "homeassistant.components.hassio.const": constants}):
            report = await collector.collect(fake_hass(data={"hassio": object(), "hassio_addons_list": models}), entry())
        self.assertEqual(["first", "last"], [item["name"] for item in report["inventory"]])
        self.assertEqual("started", report["inventory"][1]["state"])
        self.assertEqual("2026.09.1", report["supervisorVersion"])
        self.assertFalse(report["isComplete"])
        self.assertIn("položka 2", report["errorMessage"])
        self.assertNotIn("private model data", report["errorMessage"])

    async def test_addon_failure_keeps_versions_and_other_addons(self):
        module = supervisor(addons=[{"name": "first"}, {}, {"name": "last"}])
        with patch.dict(sys.modules, {"homeassistant.components.hassio": module}):
            report = await collector.collect(fake_hass(data={"hassio": object()}), entry())
        self.assertEqual("2026.09.1", report["supervisorVersion"])
        self.assertEqual("17.0", report["osVersion"])
        self.assertEqual(["first", "last"], [item["name"] for item in report["inventory"]])
        self.assertFalse(report["isComplete"])

    async def test_addon_cache_failure_keeps_versions(self):
        module = supervisor()
        module.get_addons_list = lambda _: (_ for _ in ()).throw(RuntimeError("not ready"))
        with patch.dict(sys.modules, {"homeassistant.components.hassio": module}):
            report = await collector.collect(fake_hass(data={"hassio": object()}), entry())
        self.assertEqual("2026.09.1", report["supervisorVersion"])
        self.assertEqual("17.0", report["osVersion"])
        self.assertFalse(report["isComplete"])

    async def test_version_failure_keeps_addons_and_other_version(self):
        module = supervisor(info={"supervisor": "x" * 51, "hassos": "17.0"}, addons=[{"name": "valid"}])
        with patch.dict(sys.modules, {"homeassistant.components.hassio": module}):
            report = await collector.collect(fake_hass(data={"hassio": object()}), entry())
        self.assertIsNone(report["supervisorVersion"])
        self.assertEqual("17.0", report["osVersion"])
        self.assertEqual("valid", report["inventory"][0]["name"])
        self.assertFalse(report["isComplete"])

    async def test_iterator_failure_keeps_already_collected_items(self):
        def repositories():
            yield repository("org/valid")
            raise RuntimeError("broken iterator")

        hacs = types.SimpleNamespace(repositories=types.SimpleNamespace(list_all=repositories()))
        report = await collector.collect(fake_hass(data={"hacs": hacs}, components={"esphome"}), entry())
        self.assertEqual(["org/valid", "ESPHOME"], [item["name"] for item in report["inventory"]])
        self.assertFalse(report["isComplete"])

    async def test_utf16_raw_state_is_never_truncated_or_sent_over_limit(self):
        valid = "\U0001f600" * 127 + "x"
        invalid = "x" * 254 + "\U0001f600"
        states = {"sensor.valid": state(valid), "sensor.invalid": state(invalid),
                  "sensor.surrogate": state("\ud800"), "sensor.null": state(None)}
        report = await collector.collect(fake_hass(states), entry(list(states)))
        rows = report["monitoredEntities"]
        self.assertEqual(["sensor.valid", "sensor.null"], [row["entityId"] for row in rows])
        self.assertEqual(valid, rows[0]["state"])
        self.assertEqual(255, len(rows[0]["state"].encode("utf-16-le")) // 2)
        self.assertIsNone(rows[1]["state"])
        self.assertEqual(1, rows[1]["availability"])
        self.assertFalse(report["isComplete"])
        json.dumps(report, allow_nan=False)

    async def test_metadata_truncation_respects_utf16_and_json(self):
        name = "x" * 254 + "\U0001f600" + "tail"
        report = await collector.collect(fake_hass({"sensor.one": state(name=name)}), entry(["sensor.one"]))
        self.assertEqual("x" * 254, report["monitoredEntities"][0]["name"])
        json.dumps(report, ensure_ascii=False).encode("utf-8")

    async def test_duplicate_entity_and_invalid_state_type_are_isolated(self):
        report = await collector.collect(fake_hass({"sensor.one": state(), "sensor.nan": state(float("nan"))}),
                                         entry(["sensor.one", "sensor.one", "sensor.nan"]))
        self.assertEqual(["sensor.one"], [row["entityId"] for row in report["monitoredEntities"]])
        self.assertFalse(report["isComplete"])
        json.dumps(report, allow_nan=False)

    async def test_source_timeout_keeps_partial_data_and_continues_next_source(self):
        cancelled = asyncio.Event()

        async def blocked(hass, items, errors):
            items.append({"type": 2, "name": "org/partial", "version": None, "state": None})
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(collector, "collect_hacs", side_effect=blocked), \
             patch.object(collector, "SOURCE_TIMEOUT_SECONDS", .01):
            report = await asyncio.wait_for(collector.collect(fake_hass(components={"esphome"}), entry()), timeout=1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(["org/partial", "ESPHOME"], [item["name"] for item in report["inventory"]])
        self.assertFalse(report["isComplete"])
        self.assertIn("HACS", report["errorMessage"])

    async def test_partial_report_is_sent_and_next_tick_recovers_from_source_timeout(self):
        states = {"sensor.one": state("first")}
        reporter = runtime.Reporter(fake_hass(states), entry(["sensor.one"]))

        async def blocked(*_):
            await asyncio.Event().wait()

        with patch.object(runtime, "request", new_callable=AsyncMock) as send:
            with patch.object(collector, "collect_hacs", side_effect=blocked), \
                 patch.object(collector, "SOURCE_TIMEOUT_SECONDS", .01):
                await asyncio.wait_for(reporter.send(), timeout=1)
            states["sensor.one"] = state("second")
            await reporter.send()
        first, second = [call.args[-1] for call in send.await_args_list]
        self.assertFalse(first["isComplete"])
        self.assertTrue(second["isComplete"])
        self.assertEqual("first", first["monitoredEntities"][0]["state"])
        self.assertEqual("second", second["monitoredEntities"][0]["state"])
        self.assertFalse(reporter._lock.locked())

    async def test_two_slow_sources_do_not_starve_healthy_source(self):
        started, cancelled = set(), set()

        async def blocked(name, hass, items, errors):
            started.add(name)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(name)

        async def addons(*args):
            await blocked("addons", *args)

        async def hacs(*args):
            await blocked("hacs", *args)

        with patch.object(collector, "collect_addons", side_effect=addons), \
             patch.object(collector, "collect_hacs", side_effect=hacs), \
             patch.object(collector, "SOURCE_TIMEOUT_SECONDS", .1), \
             patch.object(collector, "COLLECTION_TIMEOUT_SECONDS", .15):
            report = await asyncio.wait_for(collector.collect(fake_hass(components={"esphome"}), entry()), timeout=1)
        self.assertEqual({"addons", "hacs"}, started)
        self.assertEqual(started, cancelled)
        self.assertEqual(["ESPHOME"], [item["name"] for item in report["inventory"]])
        self.assertFalse(report["isComplete"])
        self.assertIn("Add-ony", report["errorMessage"])
        self.assertIn("HACS", report["errorMessage"])

    async def test_collection_cancellation_awaits_all_source_tasks(self):
        started, cancelled = set(), set()
        both_started = asyncio.Event()

        async def blocked(name, hass, items, errors):
            started.add(name)
            if len(started) == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(name)

        async def addons(*args):
            await blocked("addons", *args)

        async def hacs(*args):
            await blocked("hacs", *args)

        with patch.object(collector, "collect_addons", side_effect=addons), \
             patch.object(collector, "collect_hacs", side_effect=hacs):
            task = asyncio.create_task(collector.collect(fake_hass(), entry()))
            try:
                await asyncio.wait_for(both_started.wait(), timeout=1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual({"addons", "hacs"}, cancelled)

    async def test_legacy_invalid_interval_falls_back_and_still_sends_first_report(self):
        for interval in (10**15, 0, -1, None, "bad", True):
            configured = entry()
            configured.options["interval"] = interval
            reporter = runtime.Reporter(fake_hass(), configured)
            with self.subTest(interval=interval), patch.object(runtime, "async_track_time_interval") as timer, \
                 patch.object(runtime, "request", new_callable=AsyncMock) as send, self.assertLogs(runtime._LOGGER):
                reporter._started(None)
                await asyncio.gather(*reporter._tasks)
                self.assertEqual(300, timer.call_args.args[2].total_seconds())
                send.assert_awaited_once()
                await reporter.stop()

    async def test_ignored_discovery_is_not_an_installed_integration(self):
        hass = fake_hass(components={"esphome"})
        ignored = types.SimpleNamespace(entry_id="ignored", domain="ignored_discovery")
        hass.config_entries.async_entries = lambda **kwargs: [] if kwargs.get("include_ignore") is False else [ignored]
        report = await collector.collect(hass, entry())
        self.assertEqual(["ESPHOME"], [item["name"] for item in report["inventory"]])
        self.assertTrue(report["isComplete"])

    async def test_total_collection_timeout_keeps_partial_report(self):
        async def blocked(hass, items, errors):
            items.append({"type": 2, "name": "org/partial", "version": None, "state": None})
            await asyncio.Event().wait()

        with patch.object(collector, "collect_hacs", side_effect=blocked), \
             patch.object(collector, "COLLECTION_TIMEOUT_SECONDS", .01):
            report = await asyncio.wait_for(collector.collect(fake_hass({"sensor.one": state()}), entry(["sensor.one"])), timeout=1)
        self.assertEqual(1, len(report["monitoredEntities"]))
        self.assertEqual("org/partial", report["inventory"][0]["name"])
        self.assertFalse(report["isComplete"])
        self.assertIn("celého zberu", report["errorMessage"])

    async def test_report_deadline_unlocks_and_next_tick_collects_fresh_data(self):
        reporter = runtime.Reporter(fake_hass(), entry())

        async def blocked(*_):
            await asyncio.Event().wait()

        with patch.object(runtime, "REPORT_TIMEOUT_SECONDS", .01), \
             patch.object(runtime, "collect", side_effect=blocked), \
             patch.object(runtime, "request", new_callable=AsyncMock) as send:
            await asyncio.wait_for(reporter.send(), timeout=1)
            send.assert_not_called()
        self.assertFalse(reporter._lock.locked())
        with patch.object(runtime, "collect", return_value={"snapshot": "fresh"}), \
             patch.object(runtime, "request", new_callable=AsyncMock) as send:
            await reporter.send()
        self.assertEqual({"snapshot": "fresh"}, send.await_args.args[-1])

    async def test_unload_cancels_collection_without_posting_partial_snapshot(self):
        reporter = runtime.Reporter(fake_hass(), entry())
        started = asyncio.Event()

        async def blocked(*_):
            started.set()
            await asyncio.Event().wait()

        with patch.object(collector, "collect_hacs", side_effect=blocked), \
             patch.object(runtime, "request", new_callable=AsyncMock) as send:
            reporter._schedule()
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(reporter.stop(), timeout=1)
            send.assert_not_called()
        self.assertFalse(reporter._lock.locked())

    async def test_http_and_unexpected_errors_log_only_safe_diagnostics(self):
        reporter = runtime.Reporter(fake_hass(), entry())
        for error in (api.BackendResponseError(400), api.BackendResponseError(413), api.BackendResponseError(429),
                      api.BackendResponseError(500), RuntimeError("secret payload " + "a" * 64)):
            with self.subTest(error=type(error).__name__), patch.object(runtime, "collect", return_value={}), \
                 patch.object(runtime, "request", side_effect=error), self.assertLogs(runtime._LOGGER) as logs:
                await reporter.send()
            output = " ".join(logs.output)
            self.assertNotIn("secret payload", output)
            self.assertNotIn("a" * 64, output)
            self.assertFalse(reporter._lock.locked())
            if isinstance(error, api.BackendResponseError):
                self.assertIn(f"HTTP {error.status}", output)
                self.assertIn(error.reason, output)
