from datetime import timedelta

import pytest

from ingestion.sources import (
    CHANGE_FREQUENCIES,
    SourceRegistryError,
    load_check_intervals,
    load_sources,
)

VALID_ENTRY = """
  - url: https://www.desu.edu/admissions
    topic: admissions
    change_frequency: medium
    notes: Admissions landing page.
"""


def write_registry(tmp_path, entries):
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n" + entries)
    return path


def test_real_registry_loads():
    sources = load_sources()
    assert sources
    assert all(s.change_frequency in CHANGE_FREQUENCIES for s in sources)


def test_valid_entry_is_parsed(tmp_path):
    [source] = load_sources(write_registry(tmp_path, VALID_ENTRY))
    assert source.url == "https://www.desu.edu/admissions"
    assert source.topic == "admissions"
    assert source.change_frequency == "medium"
    assert source.notes == "Admissions landing page."


@pytest.mark.parametrize("field", ["url", "topic", "change_frequency", "notes"])
def test_missing_field_is_rejected(tmp_path, field):
    entries = "\n".join(line for line in VALID_ENTRY.splitlines() if f"{field}:" not in line)
    if field == "url":
        entries = entries.replace("    topic:", "  - topic:")
    with pytest.raises(SourceRegistryError, match=field):
        load_sources(write_registry(tmp_path, entries + "\n"))


def test_empty_field_is_rejected(tmp_path):
    entries = VALID_ENTRY.replace("notes: Admissions landing page.", 'notes: ""')
    with pytest.raises(SourceRegistryError, match="notes"):
        load_sources(write_registry(tmp_path, entries))


@pytest.mark.parametrize(
    "url",
    [
        "http://www.desu.edu/admissions",
        "https://www.example.com/admissions",
        "https://notdesu.edu/admissions",
        "www.desu.edu/admissions",
        "not a url",
    ],
)
def test_bad_url_is_rejected(tmp_path, url):
    entries = VALID_ENTRY.replace("https://www.desu.edu/admissions", url)
    with pytest.raises(SourceRegistryError, match="url"):
        load_sources(write_registry(tmp_path, entries))


def test_unknown_change_frequency_is_rejected(tmp_path):
    entries = VALID_ENTRY.replace("change_frequency: medium", "change_frequency: hourly")
    with pytest.raises(SourceRegistryError, match="change_frequency"):
        load_sources(write_registry(tmp_path, entries))


def test_unknown_field_is_rejected(tmp_path):
    entries = VALID_ENTRY + "    owner: web team\n"
    with pytest.raises(SourceRegistryError, match="owner"):
        load_sources(write_registry(tmp_path, entries))


def test_duplicate_url_is_rejected(tmp_path):
    with pytest.raises(SourceRegistryError, match="duplicate"):
        load_sources(write_registry(tmp_path, VALID_ENTRY + VALID_ENTRY))


def test_missing_sources_list_is_rejected(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("pages: []\n")
    with pytest.raises(SourceRegistryError, match="sources"):
        load_sources(path)


def test_invalid_yaml_is_rejected(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("sources: [unclosed\n")
    with pytest.raises(SourceRegistryError, match="YAML"):
        load_sources(path)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(SourceRegistryError, match="cannot read"):
        load_sources(tmp_path / "nope.yaml")


def test_real_check_intervals_load():
    assert load_check_intervals() == {
        "fast": timedelta(0),
        "medium": timedelta(days=1),
        "slow": timedelta(days=7),
    }


def write_intervals(tmp_path, intervals):
    path = tmp_path / "sources.yaml"
    path.write_text(intervals + "sources:\n" + VALID_ENTRY)
    return path


def test_check_intervals_accept_fractional_hours(tmp_path):
    path = write_intervals(tmp_path, "check_interval_hours: {fast: 0.5, medium: 24, slow: 168}\n")
    assert load_check_intervals(path)["fast"] == timedelta(minutes=30)


@pytest.mark.parametrize(
    "intervals",
    [
        "",  # missing
        "check_interval_hours: {fast: 0, medium: 24}\n",  # slow missing
        "check_interval_hours: {fast: 0, medium: 24, slow: 168, hourly: 1}\n",
        "check_interval_hours: {fast: -1, medium: 24, slow: 168}\n",
        "check_interval_hours: {fast: soon, medium: 24, slow: 168}\n",
        "check_interval_hours: {fast: true, medium: 24, slow: 168}\n",
    ],
)
def test_bad_check_intervals_are_rejected(tmp_path, intervals):
    with pytest.raises(SourceRegistryError, match="check_interval_hours"):
        load_check_intervals(write_intervals(tmp_path, intervals))
