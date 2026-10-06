import json
from dataclasses import replace

import pytest

from ingestion.chunk import Chunk
from ingestion.index import LocalIndex, SyncResult, fingerprint, sync

SITE = "https://www.desu.edu"


def chunk(chunk_id, text="Some text.", modified_time="2026-08-14T10:22:31-04:00", index=0):
    return Chunk(
        chunk_id=chunk_id,
        chunk_index=index,
        source_url=SITE + "/housing",
        title="Housing",
        heading_path="Housing",
        modified_time=modified_time,
        topic="housing",
        low_text=False,
        word_count=len(text.split()),
        text=text,
    )


class RecordingIndex:
    """An in-memory Index that records every batch the sync sends it."""

    def __init__(self, chunks=()):
        self.stored = {c.chunk_id: c for c in chunks}
        self.batches = []

    def fingerprints(self):
        return {i: fingerprint(c) for i, c in self.stored.items()}

    def apply(self, upserts, deletes):
        self.batches.append(([c.chunk_id for c in upserts], list(deletes)))
        for i in deletes:
            del self.stored[i]
        self.stored.update({c.chunk_id: c for c in upserts})


@pytest.fixture(params=["local", "recording"])
def make_index(request, tmp_path):
    def make(chunks=()):
        if request.param == "recording":
            return RecordingIndex(chunks)
        index = LocalIndex(tmp_path / "index" / "chunks.json")
        if chunks:
            index.apply(list(chunks), [])
        return index

    return make


def stored_ids(index):
    return sorted(index.fingerprints())


def test_adds_updates_and_removes_by_chunk_id(make_index):
    a, b, c = chunk("a"), chunk("b"), chunk("c")
    index = make_index([a, b])
    b_redated = replace(b, modified_time="2026-09-01T09:00:00-04:00")  # same ID, new metadata

    result = sync(index, [b_redated, c])

    assert result == SyncResult(added=["c"], updated=["b"], removed=["a"])
    assert stored_ids(index) == ["b", "c"]
    assert sync(index, [b_redated, c]) == SyncResult()  # nothing left to do


def test_empty_index_gets_every_chunk(make_index):
    index = make_index()
    result = sync(index, [chunk("a"), chunk("b")])
    assert (result.added, result.updated, result.removed) == (["a", "b"], [], [])
    assert stored_ids(index) == ["a", "b"]


def test_no_changes_sends_no_batch():
    index = RecordingIndex([chunk("a")])
    sync(index, [chunk("a")])
    assert index.batches == []


def test_changes_go_in_one_batch():
    index = RecordingIndex([chunk("a"), chunk("b")])
    sync(index, [chunk("b", text="Edited, same ID."), chunk("c")])
    assert index.batches == [(["c", "b"], ["a"])]


def test_local_index_is_replaced_atomically_and_not_rewritten_when_unchanged(tmp_path):
    index = LocalIndex(tmp_path / "chunks.json")
    sync(index, [chunk("b", index=1), chunk("a", index=0)])
    data = json.loads(index.path.read_text())
    assert [c["chunk_id"] for c in data["chunks"]] == ["a", "b"]  # in page order
    assert list(tmp_path.iterdir()) == [index.path]  # no temp file left behind
    mtime = index.path.stat().st_mtime_ns

    sync(index, [chunk("b", index=1), chunk("a", index=0)])

    assert index.path.stat().st_mtime_ns == mtime
