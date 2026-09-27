"""Read installed HACS repositories from its in-memory runtime."""

import asyncio

from .report_data import inventory_item, item_error


async def collect(hass, result: list[dict], errors: list[str]) -> None:
    """Return HACS items without reading HACS private storage or GitHub."""
    hacs = hass.data.get("hacs")
    if hacs is None:
        return
    # list_downloaded filters every repository before returning, outside our guard.
    for index, repository in enumerate(hacs.repositories.list_all, 1):
        await asyncio.sleep(0)
        try:
            data = repository.data
            if not data.installed:
                continue
            result.append(inventory_item(2, data.full_name, data.installed_version))
        except Exception as err:
            errors.append(item_error("HACS", index, err))
