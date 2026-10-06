"""Keep the search index in step with the chunk files, by chunk ID.

The pipeline only talks to the Index interface, so the Azure AI Search index (#22) can implement
the same three operations as the local index file the local retriever reads.
"""

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from ingestion.chunk import Chunk

DEFAULT_PATH = Path("data/index/chunks.json")

log = logging.getLogger(__name__)


class Index(Protocol):
    def fingerprints(self) -> dict[str, str]:
        """Every chunk ID in the index, with the fingerprint of the chunk stored under it."""
        ...

    def apply(self, upserts: list[Chunk], deletes: list[str]) -> None:
        """Add or replace the upserts by chunk ID and remove the deleted IDs, in one batch."""
        ...


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)  # chunk IDs
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


def fingerprint(chunk: Chunk | dict) -> str:
    """Hash of every field. The chunk ID covers the text; this also catches metadata changes,
    such as a new modified_time or topic, that keep the same ID."""
    data = asdict(chunk) if isinstance(chunk, Chunk) else chunk
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def sync(index: Index, chunks: list[Chunk]) -> SyncResult:
    """Make the index hold exactly these chunks. Writes nothing when it already does."""
    wanted = {c.chunk_id: c for c in chunks}
    current = index.fingerprints()
    result = SyncResult(
        added=[i for i in wanted if i not in current],
        updated=[i for i in wanted if i in current and current[i] != fingerprint(wanted[i])],
        removed=sorted(set(current) - set(wanted)),
    )
    if result.changed:
        index.apply([wanted[i] for i in result.added + result.updated], result.removed)
    return result


def load_chunk_files(chunks_dir: Path | str) -> list[Chunk]:
    """Every chunk in the files ingestion.chunk wrote to chunks_dir."""
    return [
        Chunk(**c)
        for path in sorted(Path(chunks_dir).glob("*.json"))
        for c in json.loads(path.read_text())["chunks"]
    ]


class LocalIndex:
    """The chunks the local retriever searches, in one JSON file.

    Each write replaces the file atomically (write a temp file, then rename), so a server
    reloading it never reads half a file.
    """

    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)

    def chunks(self) -> list[Chunk]:
        return [Chunk(**c) for c in self._load().values()]

    def fingerprints(self) -> dict[str, str]:
        return {chunk_id: fingerprint(c) for chunk_id, c in self._load().items()}

    def apply(self, upserts: list[Chunk], deletes: list[str]) -> None:
        stored = self._load()
        for chunk_id in deletes:
            stored.pop(chunk_id, None)
        for chunk in upserts:
            stored[chunk.chunk_id] = asdict(chunk)
        ordered = sorted(stored.values(), key=lambda c: (c["source_url"], c["chunk_index"]))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"chunks": ordered}, indent=1) + "\n")
        os.replace(tmp, self.path)

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        return {c["chunk_id"]: c for c in data["chunks"]}
