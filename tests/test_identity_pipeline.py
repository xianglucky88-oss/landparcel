"""End-to-end pipeline tests on the bundled offline fixture.

Verifies that identity resolution rides along the existing crawl/RPA entry
points without changing their contracts, that repeated and incremental runs
do not drift, and that transport-level flakiness is retried while a hard
failure is surfaced instead of silently partial.
"""
import pytest

from landparcel.config import SPEC
from landparcel.pipeline import run
from landparcel.store import Store
from landparcel import framework
from landparcel.framework import Http, Sleeper, Transport, fetch_all


def _fresh_store(tmp_path, monkeypatch):
    db = tmp_path / "pipeline.db"
    monkeypatch.setattr("landparcel.config.DB_PATH", db)
    con = Store().connect()
    Store(con).reset()
    return con


def test_pipeline_annotates_identity_and_is_replay_stable(tmp_path, monkeypatch):
    con = _fresh_store(tmp_path, monkeypatch)
    try:
        first = run(con=con)
        assert first["identity"]["rule_version"]
        assert first["identity"]["fingerprint"]
        second = run(con=con)
        assert second["identity"]["fingerprint"] == first["identity"]["fingerprint"]
        assert second["count"] == first["count"]
        # stored records carry the new annotation keys while keeping old ones
        rec = con.execute("SELECT payload FROM records LIMIT 1").fetchone()
        import json
        payload = json.loads(rec[0])
        for key in ("parcel_entity_id", "canonical_id", "base_id",
                    "identity_rule_version", "block_key"):
            assert key in payload
        # audit trail of runs exists
        runs = Store(con).all_runs()
        assert len(runs) >= 2 and all(r["status"] == "ok" for r in runs)
    finally:
        con.close()


def test_incremental_run_keeps_the_same_identity_fingerprint(tmp_path, monkeypatch):
    con = _fresh_store(tmp_path, monkeypatch)
    try:
        full = run(mode="full", con=con)
        inc = run(mode="incremental", con=con)
        assert inc["identity"]["fingerprint"] == full["identity"]["fingerprint"]
        assert inc["ingested"] == 0  # nothing newer than the watermark
    finally:
        con.close()


def test_flaky_transport_is_retried_without_losing_records():
    http = Http(Transport(), Sleeper())
    status, _hdr, body, attempts = http.get("/probe")
    assert status == 200
    assert attempts >= 2  # two 429s were retried
    import json
    assert json.loads(body)["ok"] is True


def test_hard_fetch_failure_is_raised_not_swallowed(monkeypatch):
    class BoomTransport(Transport):
        def request(self, method, path, params=None, json_body=None,
                    headers=None):
            return 500, {}, "boom"

    http = Http(BoomTransport(), Sleeper(), max_retries=1)
    with pytest.raises(RuntimeError):
        fetch_all(http)


def test_pipeline_failure_is_surfaced_and_leaves_no_partial_graph(
        tmp_path, monkeypatch):
    con = _fresh_store(tmp_path, monkeypatch)

    def boom(self):
        raise RuntimeError("source down")

    monkeypatch.setattr(framework.Rpa, "collect", boom)
    try:
        with pytest.raises(RuntimeError):
            run(con=con)
        # Nothing was ingested and no identity run was opened.
        assert Store(con).count() == 0
        assert Store(con).all_runs() == []
    finally:
        con.close()


def test_existing_contract_fields_are_preserved(tmp_path, monkeypatch):
    con = _fresh_store(tmp_path, monkeypatch)
    try:
        result = run(con=con)
        assert result["engine"] == SPEC.engine
        assert "fetched" in result and "duplicates" in result
        assert "ingested" in result and "count" in result
        if SPEC.engine == "rpa":
            for key in ("logged_in", "captcha_solved", "collected",
                        "submit_created", "submit_dedup", "log"):
                assert key in result
        else:
            for key in ("mode", "http_attempts", "slept", "watermark"):
                assert key in result
    finally:
        con.close()
