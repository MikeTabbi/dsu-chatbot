"""Load and validate the source registry (ingestion/sources.yaml)."""

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_PATH = Path(__file__).with_name("sources.yaml")
CHANGE_FREQUENCIES = ("slow", "medium", "fast")
ALLOWED_DOMAIN = "desu.edu"
REQUIRED_FIELDS = ("url", "topic", "change_frequency", "notes")


class SourceRegistryError(ValueError):
    """The source registry file is missing, malformed, or has an invalid entry."""


@dataclass(frozen=True)
class Source:
    url: str
    topic: str
    change_frequency: str
    notes: str


def load_sources(path: Path | str = DEFAULT_PATH) -> list[Source]:
    """Read the registry and return its sources, raising SourceRegistryError on any problem."""
    path = Path(path)
    data = _read(path)
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise SourceRegistryError(f"{path} must have a top-level 'sources' list")

    sources = [_parse_entry(i, entry) for i, entry in enumerate(data["sources"])]

    duplicates = [url for url, n in Counter(s.url for s in sources).items() if n > 1]
    if duplicates:
        raise SourceRegistryError(f"duplicate urls: {', '.join(duplicates)}")
    return sources


def load_check_intervals(path: Path | str = DEFAULT_PATH) -> dict[str, timedelta]:
    """How long after its last check each change_frequency's pages are due to be checked again."""
    path = Path(path)
    data = _read(path)
    hours = data.get("check_interval_hours") if isinstance(data, dict) else None
    if not isinstance(hours, dict) or set(hours) != set(CHANGE_FREQUENCIES):
        raise SourceRegistryError(
            f"{path} must have a 'check_interval_hours' mapping with exactly: "
            f"{', '.join(CHANGE_FREQUENCIES)}"
        )
    for frequency, value in hours.items():
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise SourceRegistryError(
                f"check_interval_hours.{frequency} must be a number of hours >= 0: {value!r}"
            )
    return {frequency: timedelta(hours=hours[frequency]) for frequency in CHANGE_FREQUENCIES}


def load_delete_after(path: Path | str = DEFAULT_PATH) -> int:
    """How many checks in a row a page must answer 404 or 410 before its files are deleted."""
    path = Path(path)
    data = _read(path)
    value = data.get("delete_after_missing_checks") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceRegistryError(
            f"{path} must set delete_after_missing_checks to a whole number >= 1: {value!r}"
        )
    return value


def _read(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text())
    except OSError as e:
        raise SourceRegistryError(f"cannot read {path}: {e}") from e
    except yaml.YAMLError as e:
        raise SourceRegistryError(f"{path} is not valid YAML: {e}") from e


def _parse_entry(index: int, entry: object) -> Source:
    where = f"sources[{index}]"
    if not isinstance(entry, dict):
        raise SourceRegistryError(f"{where} must be a mapping")

    missing = [f for f in REQUIRED_FIELDS if not isinstance(entry.get(f), str) or not entry[f]]
    if missing:
        raise SourceRegistryError(f"{where} missing or empty fields: {', '.join(missing)}")
    unknown = set(entry) - set(REQUIRED_FIELDS)
    if unknown:
        raise SourceRegistryError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")

    url = entry["url"].strip()
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (
        host == ALLOWED_DOMAIN or host.endswith("." + ALLOWED_DOMAIN)
    ):
        raise SourceRegistryError(f"{where} url must be an https URL on {ALLOWED_DOMAIN}: {url}")

    if entry["change_frequency"] not in CHANGE_FREQUENCIES:
        raise SourceRegistryError(
            f"{where} change_frequency must be one of {', '.join(CHANGE_FREQUENCIES)}: "
            f"{entry['change_frequency']!r}"
        )

    return Source(
        url=url,
        topic=entry["topic"].strip(),
        change_frequency=entry["change_frequency"],
        notes=entry["notes"].strip(),
    )


if __name__ == "__main__":
    loaded = load_sources()
    print(f"Loaded {len(loaded)} sources")
    for topic, count in sorted(Counter(s.topic for s in loaded).items()):
        print(f"  {topic}: {count}")
