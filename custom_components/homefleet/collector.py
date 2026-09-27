"""Build a bounded snapshot, preserving every successfully collected item."""

import asyncio

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, __version__ as HA_VERSION
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration

from .const import COLLECTION_TIMEOUT_SECONDS, CONF_ENTITIES, INTEGRATION_VERSION, SOURCE_TIMEOUT_SECONDS
from .hacs_inventory import collect as collect_hacs
from .report_data import InvalidReportValue, inventory_item, item_error, report_text


async def collect_entities(hass, selected: list[str], result: list[dict], errors: list[str]) -> None:
    """Read existence independently of state; isolate failures per entity."""
    registry = er.async_get(hass)
    config_entries = {entry.entry_id: entry.domain for entry in hass.config_entries.async_entries()}
    seen = set()
    for index, entity_id in enumerate(selected, 1):
        await asyncio.sleep(0)
        try:
            entity_id = report_text(entity_id, 255, "entityId", required=True)
            if entity_id.lower() in seen:
                raise InvalidReportValue("entityId")
            seen.add(entity_id.lower())
            state = hass.states.get(entity_id)
            registered = registry.async_get(entity_id)
            exists = state is not None or registered is not None
            raw = state.state if state is not None else None
            if not exists:
                availability = 4
            elif raw == STATE_UNAVAILABLE:
                availability = 2
            elif raw == STATE_UNKNOWN or (state is None and registered is not None):
                availability = 3
            else:
                availability = 1
            name = state.attributes.get("friendly_name") if state is not None else registered.name if registered else None
            source = (config_entries.get(registered.config_entry_id) or registered.platform) if registered else None
            result.append({
                "entityId": entity_id,
                "name": report_text(name, 255, "name", truncate=True),
                "integrationDomain": report_text(source, 100, "integrationDomain", truncate=True),
                "availability": availability,
                "state": report_text(raw, 255, "state"),
            })
        except Exception as err:
            errors.append(item_error("Entity", index, err))


async def collect_integrations(hass, result: list[dict], errors: list[str]) -> None:
    """Collect configured integrations without internal HA components."""
    domains = {entry.domain for entry in hass.config_entries.async_entries(include_ignore=False)}
    for index, domain in enumerate(sorted(domains), 1):
        await asyncio.sleep(0)
        try:
            manifest = async_get_loaded_integration(hass, domain).manifest
            item = inventory_item(1, manifest.get("name") or domain, manifest.get("version"))
        except IntegrationNotLoaded:
            # Configured integrations may legitimately not be loaded. The domain
            # is still useful inventory and an unavailable version is optional.
            item = inventory_item(1, domain)
        except Exception as err:
            try:
                item = inventory_item(1, domain)
            except Exception:
                errors.append(_integration_error(domain, index, err, included=False))
                continue
            errors.append(_integration_error(domain, index, err, included=True))
        result.append(item)


async def collect_supervisor_info(hass, report: dict, errors: list[str]) -> None:
    """Version fields are independent of the add-on cache and of each other."""
    if "hassio" not in hass.data:
        return
    from homeassistant.components.hassio import get_info

    info = get_info(hass)
    for source, target in (("supervisor", "supervisorVersion"), ("hassos", "osVersion")):
        try:
            report[target] = report_text(info.get(source), 50, target)
        except Exception:
            errors.append(f"Supervisor: neplatné pole {target}")


async def collect_addons(hass, result: list[dict], errors: list[str]) -> None:
    """An invalid add-on must not remove other add-ons or version fields."""
    if "hassio" not in hass.data:
        return
    convert_models = False
    try:
        from homeassistant.components.hassio import get_addons_list
    except ImportError:
        # HA 2026.2 exposes installed add-ons in the Supervisor info helper.
        from homeassistant.components.hassio import get_supervisor_info

        addons = get_supervisor_info(hass)["addons"]
    else:
        try:
            addons = get_addons_list(hass)
        except Exception:
            # HA 2026.9 serializes all cached models eagerly in the public helper.
            # Fall back to that same cache to isolate a single model's to_dict failure.
            from homeassistant.components.hassio.const import DATA_ADDONS_LIST

            addons = hass.data[DATA_ADDONS_LIST]
            convert_models = True
            errors.append("Add-ony: hromadné čítanie zlyhalo; použitá cache po položkách")
    for index, addon in enumerate(addons, 1):
        await asyncio.sleep(0)
        try:
            if convert_models:
                addon = addon.to_dict()
            result.append(inventory_item(3, addon["name"], addon.get("version"), addon.get("state")))
        except Exception as err:
            errors.append(item_error("Add-ony", index, err))


async def collect(hass, entry) -> dict:
    """Bound each source and the whole collection; cancellation still propagates."""
    errors = []
    report = {
        "haVersion": HA_VERSION,
        "supervisorVersion": None,
        "osVersion": None,
        "integrationVersion": INTEGRATION_VERSION,
        "isComplete": True,
        "errorMessage": None,
        "monitoredEntities": [],
        "inventory": [],
    }
    addon_items, hacs_items, integration_items = [], [], []
    try:
        async with asyncio.timeout(COLLECTION_TIMEOUT_SECONDS):
            # TaskGroup cancels and awaits all children on timeout or unload.
            # Source errors are handled individually, so siblings keep running.
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(_collect_source("Entity", collect_entities(hass,
                    entry.options.get(CONF_ENTITIES, entry.data.get(CONF_ENTITIES, [])), report["monitoredEntities"], errors), errors))
                tasks.create_task(_collect_source("Supervisor", collect_supervisor_info(hass, report, errors), errors))
                tasks.create_task(_collect_source("Add-ony", collect_addons(hass, addon_items, errors), errors))
                tasks.create_task(_collect_source("HACS", collect_hacs(hass, hacs_items, errors), errors))
                tasks.create_task(_collect_source("HA integrácie", collect_integrations(hass, integration_items, errors), errors))
    except TimeoutError:
        errors.append("Vypršal časový limit celého zberu")
    # Preserve category order regardless of the order in which tasks finish.
    report["inventory"] = addon_items + hacs_items + integration_items
    if errors:
        report["isComplete"] = False
        # A broken source must not create an arbitrarily large error payload.
        report["errorMessage"] = "; ".join(errors[:20])
        if len(errors) > 20:
            report["errorMessage"] += f"; ďalších chýb: {len(errors) - 20}"
    return report


async def _collect_source(name: str, operation, errors: list[str]) -> None:
    """Keep items already appended even if a source fails or times out."""
    try:
        async with asyncio.timeout(SOURCE_TIMEOUT_SECONDS):
            await operation
    except TimeoutError:
        errors.append(f"{name}: vypršal časový limit zberu")
    except Exception as err:
        errors.append(f"{name}: nepodarilo sa načítať zdroj ({type(err).__name__})")


def _integration_error(domain, index: int, error: Exception, *, included: bool) -> str:
    """Describe a manifest failure without exposing exception text or report data."""
    identifier = domain if isinstance(domain, str) else f"položka {index}"
    field = f", pole {error}" if isinstance(error, InvalidReportValue) else ""
    outcome = "odoslaná ako doména bez verzie" if included else "položka vynechaná"
    return f"HA integrácia [{identifier}]: {type(error).__name__}{field}; {outcome}"
