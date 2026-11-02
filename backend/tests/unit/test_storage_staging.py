"""Staged outputs in file storage (KTD2, U4): office-scoped, path-safe keys, atomic replacement, and delete-by-prefix
that can reach staged files only. No database."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.platform.storage import LocalStorage, staged_key, staging_prefix, storage_key

OFFICE, OTHER = uuid.uuid4(), uuid.uuid4()
VERSION, OTHER_VERSION = uuid.uuid4(), uuid.uuid4()
ATTEMPT = hashlib.sha256(b"attempt").hexdigest()
OTHER_ATTEMPT = hashlib.sha256(b"another").hexdigest()


@pytest.fixture
def store(tmp_path):
    return LocalStorage(tmp_path)


def test_a_staged_file_is_replaced_atomically_while_an_original_is_written_once(store):
    key = staged_key(OFFICE, VERSION, ATTEMPT, "state.json")
    store.replace(key, b"one")
    store.replace(key, b"two")
    assert store.get(key) == b"two" and store.exists(key)
    original = storage_key(OFFICE, hashlib.sha256(b"file").hexdigest())
    store.put(original, b"first")
    store.put(original, b"second")  # content-addressed: the file is never rewritten
    assert store.get(original) == b"first"
    leftovers = [p.name for p in (store.root / str(OFFICE) / "staging" / str(VERSION) / ATTEMPT).iterdir()]
    assert leftovers == ["state.json"]  # no temporary file left behind


@pytest.mark.parametrize("name", ["", "../x", "a/b", "W000001.json", "state.json.gz.exe", "state json", ".json",
                                  "w" * 20 + ".json"])
def test_a_staged_name_must_be_one_of_the_plain_shapes(name):
    with pytest.raises(ValueError):
        staged_key(OFFICE, VERSION, ATTEMPT, name)


@pytest.mark.parametrize("key", [
    "not-a-uuid/staging/" + str(VERSION) + "/" + ATTEMPT + "/state.json",
    f"{OFFICE}/staging/not-a-uuid/{ATTEMPT}/state.json",
    f"{OFFICE}/staging/{VERSION}/{ATTEMPT[:-1]}/state.json",
    f"{OFFICE}/staging/{VERSION}/{ATTEMPT.upper()}/state.json",
    f"{OFFICE}/staging/{VERSION}/../{ATTEMPT}/state.json",
    f"{OFFICE}/other/{VERSION}/{ATTEMPT}/state.json",
    f"{OFFICE}/staging/{VERSION}/{ATTEMPT}/sub/state.json",
    f"{OFFICE}/{'a' * 63}/extra",
])
def test_a_key_that_is_not_an_office_scoped_shape_never_reaches_the_filesystem(store, key):
    with pytest.raises(ValueError):
        store.replace(key, b"x")
    with pytest.raises(ValueError):
        store.get(key)


def test_an_attempt_key_must_be_a_sha256():
    with pytest.raises(ValueError):
        staging_prefix(OFFICE, VERSION, "../escape")


def test_delete_by_prefix_removes_a_versions_attempt_or_a_name_and_nothing_else(store):
    keys = {(v, a, n): staged_key(OFFICE, v, a, n)
            for v in (VERSION, OTHER_VERSION) for a in (ATTEMPT, OTHER_ATTEMPT)
            for n in ("state.json", "w000000.json.gz", "w000001.json.gz", "c000000.json.gz")}
    for k in keys.values():
        store.replace(k, b"x")
    foreign = staged_key(OTHER, VERSION, ATTEMPT, "state.json")
    store.replace(foreign, b"x")
    original = storage_key(OFFICE, hashlib.sha256(b"file").hexdigest())
    store.put(original, b"the file")

    assert store.delete_prefix(staging_prefix(OFFICE, VERSION, ATTEMPT) + "w") == 2  # a name prefix
    assert not store.exists(keys[(VERSION, ATTEMPT, "w000000.json.gz")])
    assert store.exists(keys[(VERSION, ATTEMPT, "state.json")])
    assert store.delete_prefix(staging_prefix(OFFICE, VERSION, ATTEMPT)) == 2  # one attempt
    assert store.exists(keys[(VERSION, OTHER_ATTEMPT, "state.json")])
    assert store.delete_prefix(staging_prefix(OFFICE, VERSION)) == 4  # the rest of the version
    assert not any(store.exists(k) for (v, _, _), k in keys.items() if v == VERSION)
    assert all(store.exists(k) for (v, _, _), k in keys.items() if v == OTHER_VERSION)
    assert store.exists(foreign) and store.get(original) == b"the file"
    assert store.delete_prefix(staging_prefix(OFFICE, VERSION)) == 0  # idempotent: nothing left


def test_delete_by_prefix_cannot_reach_originals_or_other_offices(store):
    original = storage_key(OFFICE, hashlib.sha256(b"file").hexdigest())
    store.put(original, b"the file")
    for prefix in (f"{OFFICE}/", f"{OFFICE}", original, f"{OFFICE}/{original.split('/')[1][:10]}",
                   f"{OFFICE}/staging", f"{OFFICE}/staging/{VERSION}", f"{OFFICE}/../{OTHER}/staging/",
                   f"{OFFICE}/staging/{VERSION}/{ATTEMPT[:10]}/", "/", ""):
        with pytest.raises(ValueError):
            store.delete_prefix(prefix)
    assert store.get(original) == b"the file"
    assert store.delete_prefix(staging_prefix(OFFICE)) == 0  # an office without staged files
