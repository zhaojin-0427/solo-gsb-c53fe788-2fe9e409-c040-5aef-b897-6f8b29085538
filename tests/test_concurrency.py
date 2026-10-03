"""Cross-connection / cross-process idempotency tests.

Two independent service processes share one SQLite database file but hold
separate connections (and, in reality, separate process locks).  These tests
open multiple connections against one file DB and drive the service layer
directly on them from worker threads, so the in-process ``RLock`` cannot
hide a broken check-then-write sequence — only ``BEGIN IMMEDIATE`` plus the
UNIQUE indexes can keep the arbitration correct.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db as db_mod
from app import seed, service
from app.errors import ConflictError
from app.models import AccountingRequest, RecalculateRequest

from tests.conftest import REFINERY_REQUEST


@pytest.fixture
def file_db(tmp_path):
    path = str(tmp_path / "cross.db")
    opener = db_mod.connect(path)
    db_mod.init_db(opener)
    seed.seed(opener)
    opener.commit()
    yield path
    opener.close()


def _extra_conn(path):
    # Mirrors connect() but without the first-time schema bootstrap; each
    # connection also gets its own busy timeout like a real second process.
    conn = db_mod.sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = db_mod.sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _submit(path, payload):
    conn = _extra_conn(path)
    try:
        return service._submit_assessment(
            conn, AccountingRequest.model_validate(payload))
    finally:
        conn.close()


def test_identical_concurrent_submissions_create_one_snapshot(file_db):
    # The original defect: the loser surfaced a raw SQLite UNIQUE constraint
    # error.  Now both contenders must succeed and share one snapshot.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _submit(file_db, REFINERY_REQUEST),
                                range(2)))

    assert [r["idempotent_replay"] for r in results].count(False) == 1
    assert [r["idempotent_replay"] for r in results].count(True) == 1
    assert results[0]["snapshot_id"] == results[1]["snapshot_id"]
    assert results[0]["input_hash"] == results[1]["input_hash"]

    conn = _extra_conn(file_db)
    try:
        rows = conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()
        assert rows["n"] == 1
        stored = db_mod.get_snapshot_by_hash(conn, results[0]["input_hash"])
        assert stored["client_request_id"] == \
            REFINERY_REQUEST["client_request_id"]
    finally:
        conn.close()


def test_same_client_key_different_input_concurrency(file_db):
    other = {**REFINERY_REQUEST,
             "external_materials": [
                 {"ref": "crude", "name": "原油", "quantity": "999",
                  "unit": "kg", "factor_ref": "ef.crude_oil"}],
             "processes": [
                 {**REFINERY_REQUEST["processes"][0],
                  "inputs": [
                      {"ref": "crude", "quantity": "999", "unit": "kg"}]}]}
    # Sanity: different input summary.
    h1 = service.input_hash(
        AccountingRequest.model_validate(REFINERY_REQUEST))
    h2 = service.input_hash(AccountingRequest.model_validate(other))
    assert h1 != h2

    barrier = threading.Barrier(2)

    def racy(payload):
        conn = _extra_conn(file_db)
        try:
            barrier.wait(timeout=10)
            return service._submit_assessment(
                conn, AccountingRequest.model_validate(payload))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(racy, REFINERY_REQUEST),
                   pool.submit(racy, other)]
        outcomes = []
        for f in futures:
            try:
                outcomes.append(("ok", f.result()))
            except ConflictError as exc:
                outcomes.append(("conflict", exc))

    assert {kind for kind, _ in outcomes} == {"ok", "conflict"}
    ok = next(value for kind, value in outcomes if kind == "ok")
    conflict = next(value for kind, value in outcomes
                    if kind == "conflict")
    assert conflict.code == "CLIENT_REQUEST_ID_CONFLICT"
    assert conflict.details["existing_snapshot_id"] == ok["snapshot_id"]
    assert conflict.details["existing_input_hash"] == ok["input_hash"]

    # Exactly one snapshot survives; the loser left no partial evidence.
    conn = _extra_conn(file_db)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()
        assert n["n"] == 1
        keys = conn.execute(
            "SELECT DISTINCT client_request_id AS k FROM snapshots").fetchall()
        assert [r["k"] for r in keys] == [
            REFINERY_REQUEST["client_request_id"]]
    finally:
        conn.close()


def test_concurrent_recalculation_shares_one_new_snapshot(file_db):
    first = _submit(file_db, REFINERY_REQUEST)
    sid = first["snapshot_id"]

    def recalc():
        conn = _extra_conn(file_db)
        try:
            return service._recalculate(
                conn, sid,
                RecalculateRequest(factor_version="v2024-01"))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recalc(), range(2)))

    flags = [r["new_snapshot"]["idempotent_replay"] for r in results]
    assert sorted(flags, key=str) == [False, True]
    new_ids = {r["new_snapshot"]["snapshot_id"] for r in results}
    assert len(new_ids) == 1
    # Accounting output and diffs are unchanged for the replay.
    assert results[0]["product_diffs"] == results[1]["product_diffs"]
    assert results[0]["factor_changes"] == results[1]["factor_changes"]
    assert {r["new_snapshot"]["source_snapshot"] for r in results} == {sid}

    conn = _extra_conn(file_db)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()
        assert n["n"] == 2  # original + one recalculated snapshot
    finally:
        conn.close()


def test_recalculate_reuses_other_clients_key_is_409(file_db):
    # The client key first anchors a genuinely different snapshot (a
    # v2024-01 assessment whose crude input differs from the refinery batch);
    # reusing it on the refinery recalculation must be rejected.
    first = _submit(file_db, {**REFINERY_REQUEST,
                              "client_request_id": "req-original"})
    other_payload = {
        "client_request_id": "req-recalc",
        "factor_version": "v2024-01",
        "allocation_method": "mass",
        "external_materials": [
            {"ref": "crude", "name": "原油", "quantity": "500",
             "unit": "kg", "factor_ref": "ef.crude_oil"}],
        "processes": [{
            "process_id": "P", "batch_quantity": "500", "unit": "kg",
            "inputs": [
                {"ref": "crude", "quantity": "500", "unit": "kg"}],
            "outputs": [{"product_id": "x", "quantity": "500", "unit": "kg",
                         "mass_weight": "500"}]}]}
    second = _submit(file_db, other_payload)
    assert second["idempotent_replay"] is False

    conn = _extra_conn(file_db)
    try:
        with pytest.raises(ConflictError) as exc:
            service._recalculate(
                conn, first["snapshot_id"],
                RecalculateRequest(factor_version="v2024-01",
                                   client_request_id="req-recalc"))
        assert exc.value.code == "CLIENT_REQUEST_ID_CONFLICT"
        assert exc.value.details["existing_snapshot_id"] == \
            second["snapshot_id"]

        # The rejected recalculation created no evidence.
        n = conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()
        assert n["n"] == 2
    finally:
        conn.close()


def test_database_enforces_client_key_unique_index(file_db):
    conn = _extra_conn(file_db)
    try:
        service._submit_assessment(
            conn, AccountingRequest.model_validate(REFINERY_REQUEST))

        # The partial UNIQUE index exists...
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_snapshots_client_unique'").fetchone()
        assert row is not None and "UNIQUE" in row["sql"].upper()
        # ...and the legacy non-unique index is gone.
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_snapshots_client'").fetchone()
        assert legacy is None

        # NULL client keys remain unrestricted.
        no_key = {k: v for k, v in REFINERY_REQUEST.items()
                  if k != "client_request_id"}
        service._submit_assessment(
            conn, AccountingRequest.model_validate(no_key))
    finally:
        conn.close()
