import csv

import pytest

from ingestion.approve import main, merge, read_approved
from ingestion.discover import CANDIDATE_COLUMNS
from ingestion.sources import SourceRegistryError, load_sources

REGISTRY = """\
# A comment that must survive the merge.
check_interval_hours:
  fast: 0
  medium: 24
  slow: 168
delete_after_missing_checks: 3

sources:
  - url: https://www.desu.edu/admissions
    topic: admissions
    change_frequency: medium
    notes: Admissions landing page.
"""


def row(url, topic="financial-aid", frequency="medium", approve="yes", title="A page", notes=""):
    return {
        "approve": approve,
        "url": url,
        "topic": topic,
        "change_frequency": frequency,
        "title": title,
        "notes": notes,
        "word_count": "120",
        "found_in": "sitemap.xml",
        "duplicate_urls": "",
    }


@pytest.fixture
def files(tmp_path):
    sources = tmp_path / "sources.yaml"
    sources.write_text(REGISTRY)
    candidates = tmp_path / "candidates.csv"

    def write(*rows):
        with candidates.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CANDIDATE_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return candidates

    return sources, write


def test_merges_only_approved_rows(files):
    sources, write = files
    candidates = write(
        row("https://www.desu.edu/admissions/financial-aid", title="Financial Aid: Overview"),
        row("https://www.desu.edu/academics/library", topic="library", approve=""),
        row("https://cast.desu.edu/departments/chemistry", topic="academics", approve="Y"),
    )
    added = merge(read_approved(candidates), sources)

    assert added == {"financial-aid": 1, "academics": 1}
    loaded = {s.url: s for s in load_sources(sources)}
    assert set(loaded) == {
        "https://www.desu.edu/admissions",
        "https://www.desu.edu/admissions/financial-aid",
        "https://cast.desu.edu/departments/chemistry",
    }
    # Empty notes default to the title; a title with ": " is quoted so it stays one string
    assert loaded["https://www.desu.edu/admissions/financial-aid"].notes == (
        "Financial Aid: Overview."
    )
    text = sources.read_text()
    assert text.startswith("# A comment that must survive the merge.")
    assert "check_interval_hours" in text


@pytest.mark.parametrize(
    "bad, message",
    [
        (row("http://www.desu.edu/admissions/apply-now"), "https URL"),
        (row("https://www.example.com/admissions"), "https URL"),
        (row("https://www.desu.edu/a", frequency="weekly"), "change_frequency"),
        (row("https://www.desu.edu/a", topic=""), "missing or empty fields: topic"),
        (row("https://www.desu.edu/a", title="", notes=""), "missing or empty fields: notes"),
        (row("https://www.desu.edu/admissions"), "duplicate urls"),
    ],
)
def test_rejects_invalid_entries_and_writes_nothing(files, bad, message):
    sources, write = files
    candidates = write(row("https://www.desu.edu/academics/library", topic="library"), bad)
    with pytest.raises(SourceRegistryError, match=message) as e:
        merge(read_approved(candidates), sources, candidates_name="candidates.csv")
    assert "candidates.csv line 3" in str(e.value)
    assert sources.read_text() == REGISTRY
    assert sorted(p.name for p in sources.parent.iterdir()) == ["candidates.csv", "sources.yaml"]


def test_cli_dry_run_writes_nothing(files, capsys):
    sources, write = files
    candidates = write(row("https://www.desu.edu/academics/library", topic="library"))
    main(["--candidates", str(candidates), "--sources", str(sources), "--dry-run"])
    assert "Would add 1 sources" in capsys.readouterr().out
    assert sources.read_text() == REGISTRY


def test_cli_reports_invalid_entry(files):
    sources, write = files
    candidates = write(row("https://www.desu.edu/a", frequency="hourly"))
    with pytest.raises(SystemExit, match="Not merged: .*line 2"):
        main(["--candidates", str(candidates), "--sources", str(sources)])
    assert sources.read_text() == REGISTRY
