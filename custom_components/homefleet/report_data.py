"""Validate snapshot values against SQL Server's UTF-16 string limits."""


class InvalidReportValue(ValueError):
    """A field cannot be represented in the backend; never include its value."""


def report_text(value, limit: int, field: str, *, required: bool = False, truncate: bool = False) -> str | None:
    """Keep raw states intact; only descriptive metadata may be shortened."""
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise InvalidReportValue(field)
    try:
        encoded = value.encode("utf-16-le")
    except UnicodeEncodeError:
        raise InvalidReportValue(field) from None
    if len(encoded) <= limit * 2:
        return value
    if not truncate:
        raise InvalidReportValue(field)
    # Do not leave half of a surrogate pair at the boundary.
    shortened = encoded[:limit * 2].decode("utf-16-le", errors="ignore")
    if required and not shortened.strip():
        raise InvalidReportValue(field)
    return shortened


def inventory_item(kind: int, name, version=None, state=None) -> dict:
    """Build one valid inventory row before adding it to the report."""
    return {
        "type": kind,
        "name": report_text(name, 255, "name", required=True, truncate=True),
        "version": report_text(version, 100, "version", truncate=True),
        "state": report_text(state, 50, "state"),
    }


def item_error(source: str, index: int, error: Exception) -> str:
    """Describe a failed item without copying source data or exception messages."""
    field = f" ({error})" if isinstance(error, InvalidReportValue) else ""
    return f"{source}: položka {index} sa nedá odoslať{field}"
