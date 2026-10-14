"""Add the approved rows of a discovery candidates file to sources.yaml.

Rows marked `yes` in the approve column are appended to sources.yaml, then the whole file is
checked with the same loader the crawler and pipeline use. If any entry is invalid (a missing
field, a non-https or off-domain URL, a bad change_frequency, a URL already in the registry),
nothing is written and the problem rows are reported.
"""

import argparse
import csv
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

from ingestion.discover import DEFAULT_OUTPUT_DIR
from ingestion.sources import DEFAULT_PATH as DEFAULT_SOURCES_PATH
from ingestion.sources import (
    REQUIRED_FIELDS,
    SourceRegistryError,
    load_check_intervals,
    load_delete_after,
    load_sources,
)

DEFAULT_CANDIDATES = DEFAULT_OUTPUT_DIR / "candidates.csv"
APPROVED = {"yes", "y", "x", "true", "1"}
ENTRY_INDEX = re.compile(r"^sources\[(\d+)\]")


@dataclass(frozen=True)
class Approved:
    line: int  # line in the candidates file, for error messages
    entry: dict[str, str]


def read_approved(path: Path | str) -> list[Approved]:
    """The rows marked approved, as sources.yaml entries. Empty notes default to the title."""
    path = Path(path)
    try:
        with path.open(newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        raise SourceRegistryError(f"cannot read {path}: {e}") from e
    approved = []
    for line, row in enumerate(rows, start=2):  # line 1 is the header
        if (row.get("approve") or "").strip().lower() not in APPROVED:
            continue
        title = (row.get("title") or "").strip()
        notes = (row.get("notes") or "").strip() or (f"{title}." if title else "")
        entry = {f: (row.get(f) or "").strip() for f in REQUIRED_FIELDS if f != "notes"}
        approved.append(Approved(line, {**entry, "notes": notes}))
    return approved


def merge(
    approved: list[Approved],
    sources_path: Path | str = DEFAULT_SOURCES_PATH,
    *,
    candidates_name: str = "candidates file",
    dry_run: bool = False,
) -> Counter:
    """Append the approved entries to sources.yaml if the result passes the loader's checks.
    Returns the count of added entries by topic. Raises SourceRegistryError, writing nothing,
    if any entry is invalid."""
    sources_path = Path(sources_path)
    existing = len(load_sources(sources_path))
    if not approved:
        return Counter()
    text = sources_path.read_text().rstrip("\n") + "\n" + _entries_yaml(approved)

    # Check the merged file with the real loader before replacing anything.
    fd, tmp = tempfile.mkstemp(dir=sources_path.parent, prefix=".sources-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        try:
            load_sources(tmp)
            load_check_intervals(tmp)
            load_delete_after(tmp)
        except SourceRegistryError as e:
            raise SourceRegistryError(_explain(str(e), existing, approved, candidates_name)) from e
        if dry_run:
            os.unlink(tmp)
        else:
            os.replace(tmp, sources_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return Counter(a.entry["topic"] for a in approved)


def _entries_yaml(approved: list[Approved]) -> str:
    lines = ["", "  # Added from discovery (python -m ingestion.approve)"]
    for a in sorted(approved, key=lambda a: (a.entry["topic"], a.entry["url"])):
        for i, f in enumerate(REQUIRED_FIELDS):
            lines.append(f"{'  - ' if i == 0 else '    '}{f}: {_scalar(a.entry[f])}")
    return "\n".join(lines) + "\n"


def _scalar(value: str) -> str:
    """value as a YAML scalar: bare when it reads back unchanged, else double-quoted."""
    try:
        if value and yaml.safe_load(f"k: {value}") == {"k": value}:
            return value
    except yaml.YAMLError:
        pass
    return json.dumps(value)  # a JSON string is a valid YAML double-quoted scalar


def _explain(error: str, existing: int, approved: list[Approved], candidates_name: str) -> str:
    """Point a loader error about an appended entry at its line in the candidates file."""
    ordered = sorted(approved, key=lambda a: (a.entry["topic"], a.entry["url"]))
    if (m := ENTRY_INDEX.match(error)) and int(m[1]) >= existing:
        a = ordered[int(m[1]) - existing]
        return f"{candidates_name} line {a.line} ({a.entry['url'] or 'no url'}): {error}"
    if error.startswith("duplicate urls: "):
        dupes = set(error.removeprefix("duplicate urls: ").split(", "))
        lines = [str(a.line) for a in ordered if a.entry["url"] in dupes]
        return f"{candidates_name} line{'s' * (len(lines) > 1)} {', '.join(lines)}: {error}"
    return error


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES_PATH)
    parser.add_argument(
        "--dry-run", action="store_true", help="check and count the approved rows; write nothing"
    )
    args = parser.parse_args(argv)

    try:
        approved = read_approved(args.candidates)
        added = merge(
            approved, args.sources, candidates_name=str(args.candidates), dry_run=args.dry_run
        )
    except SourceRegistryError as e:
        raise SystemExit(f"Not merged: {e}") from e
    verb = "Would add" if args.dry_run else "Added"
    print(f"{verb} {sum(added.values())} sources to {args.sources}")
    for topic, n in sorted(added.items()):
        print(f"  {topic}: {n}")


if __name__ == "__main__":
    main()
