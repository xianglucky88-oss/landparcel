"""Persistence, review workflow and drift tests for parcel identity.

These tests exercise the real sqlite store (in-memory), the reviewer
helpers, partial-failure rollback, pagination-order invariance through the
pipeline, and the legacy offline entry points.
"""
import sqlite3
import pytest

from landparcel import identity, review
from landparcel.store import Store
from landparcel.framework import resolve_identity
from landparcel.mockdata import parcel_scenario_rows


@pytest.fixture()
def store():
    con = sqlite3.connect(":memory:")
    s = Store(con)
    yield s
    con.close()


def _ingest(store, rows, mode="full"):
    annotated, summary = resolve_identity(store, rows, mode)
    with store.transaction():
        for r in annotated:
            store.upsert(r)
    return annotated, summary


# --------------------------------------------------------------------------- #
# Store / graph persistence
# --------------------------------------------------------------------------- #
def test_graph_is_persisted_and_queryable(store):
    rows = parcel_scenario_rows()
    _, summary = _ingest(store, rows)
    assert store.count() == len({identity.content_hash(r) for r in rows})
    assert len(store.all_entities()) == summary["entities"]
    assert len(store.all_relations()) == summary["relations"]
    # every member points at a real entity
    entity_ids = {e["entity_id"] for e in store.all_entities()}
    for rec in store.all_records():
        assert store.entity_of(identity.content_hash(rec)) in entity_ids


def test_replay_against_history_keeps_fingerprint_stable(store):
    rows = parcel_scenario_rows()
    _, s1 = _ingest(store, rows)
    _, s2 = _ingest(store, rows)
    assert s1["fingerprint"] == s2["fingerprint"]
    runs = store.all_runs()
    assert all(r["status"] == "ok" for r in runs)
    assert runs[0]["rule_version"] == identity.RULE_VERSION


def test_incremental_backfill_does_not_rewrite_identity(store):
    rows = parcel_scenario_rows()
    base = [r for r in rows if r["id"] != "LP-200-C"]
    backfill = [r for r in rows if r["id"] == "LP-200-C"]
    _, before = _ingest(store, base)
    _, after = _ingest(store, backfill, mode="incremental")
    before_ents = {e["entity_id"] for e in store.all_entities()}
    # full graph contains every original entity plus the new split child
    assert after["entities"] >= before["entities"]
    assert before_ents <= {e["entity_id"] for e in store.all_entities()}


# --------------------------------------------------------------------------- #
# Review report: candidates, conflicts, manual conclusions, traceability
# --------------------------------------------------------------------------- #
def test_review_report_separates_the_two_failure_modes(store):
    _ingest(store, parcel_scenario_rows())
    rep = store.review_report()
    assert rep["candidates"], "merge/split candidates must be visible"
    assert rep["conflicts"], "the KP-400 name conflict must be queued"
    # wrong merge: old prefix rule would merge distinct parcels
    wrong = {tuple(sorted((w["left_id"], w["right_id"])))
             for w in rep["wrong_merge_suspects"]}
    assert tuple(sorted(("BD-101", "BD-102"))) in wrong
    # missed merge: unresolved candidate links that might be one parcel
    missed = {tuple(sorted((m["left_id"], m["right_id"])))
              for m in rep["missed_merge_suspects"]}
    assert tuple(sorted(("NB-300", "NB-301"))) in missed
    assert wrong.isdisjoint(missed)


def test_manual_decision_is_stored_with_basis_and_survives_replay(store):
    rows = parcel_scenario_rows()
    _ingest(store, rows)
    conflict = store.review_report()["conflicts"][0]
    out = review.decide(store, conflict["relation_id"], "rejected",
                        "auditor-chen", "档案权利人不一致，驳回归并")
    assert out["state"] == "rejected"
    decisions = store.load_decisions()
    saved = decisions[conflict["relation_id"]]
    assert saved["reviewer"] == "auditor-chen"
    assert saved["basis"] == "档案权利人不一致，驳回归并"
    # replay: rule output must not clobber the manual conclusion
    _ingest(store, rows)
    rep = store.review_report()
    rel = next(r for r in rep["rejected"]
               if r["relation_id"] == conflict["relation_id"])
    assert rel["manual"] is True
    assert rel["basis"] == "档案权利人不一致，驳回归并"


def test_decide_validates_inputs(store):
    _ingest(store, parcel_scenario_rows())
    rid = store.all_relations()[0]["relation_id"]
    with pytest.raises(ValueError):
        review.decide(store, rid, "maybe", "x", "basis")
    with pytest.raises(ValueError):
        review.decide(store, rid, "confirmed", "x", "  ")
    with pytest.raises(KeyError):
        review.decide(store, "nope", "confirmed", "x", "basis")


def test_confirmed_merge_unifies_entities(store):
    rows = parcel_scenario_rows()
    _ingest(store, rows)
    rel = next(r for r in store.all_relations()
               if tuple(sorted((r["left_id"], r["right_id"])))
               == tuple(sorted(("NB-300", "NB-301"))))
    review.decide(store, rel["relation_id"], "confirmed", "auditor-li",
                  "两编号对应同一出让合同，确认归并")
    h1 = identity.content_hash(next(r for r in rows if r["id"] == "NB-300"))
    h2 = identity.content_hash(next(r for r in rows if r["id"] == "NB-301"))
    assert store.entity_of(h1) == store.entity_of(h2)


# --------------------------------------------------------------------------- #
# Old-rule recomputation
# --------------------------------------------------------------------------- #
def test_legacy_recompute_flags_erroneous_prefix_merges(store):
    _ingest(store, parcel_scenario_rows())
    leg = review.recompute_legacy(store)
    assert leg["legacy_rule"] == "name-prefix-6"
    bad = {tuple(sorted((m["left_id"], m["right_id"])))
           for m in leg["erroneous_merges"]}
    assert tuple(sorted(("BD-101", "BD-102"))) in bad
    # identifier-backed equivalence is never reported as an old-rule error
    assert not any(
        {m["left_id"], m["right_id"]} == {"BD-001", "BD 001"}
        for m in leg["erroneous_merges"])
    pending = {tuple(sorted((m["left_id"], m["right_id"])))
               for m in leg["pending_legacy_merges"]}
    assert tuple(sorted(("NB-300", "NB-301"))) in pending


# --------------------------------------------------------------------------- #
# Partial failure: identity writes roll back, failure is recorded
# --------------------------------------------------------------------------- #
def test_partial_failure_rolls_back_and_is_auditable(store):
    rows = parcel_scenario_rows()
    before_entities = len(store.all_entities())

    def boom(entity_id, rows_):
        raise RuntimeError("simulated local failure")

    store.replace_members = boom  # fail inside the graph write transaction
    with pytest.raises(RuntimeError):
        resolve_identity(store, rows, "full")
    assert len(store.all_entities()) == before_entities
    failed = [r for r in store.all_runs() if r["status"] == "failed"]
    assert failed and failed[-1]["error"]
    # the store remains usable after the failure
    del store.replace_members  # restore the real method for the retry
    _, summary = _ingest(store, rows)
    assert summary["relations"]


# --------------------------------------------------------------------------- #
# Order invariance through the orchestration layer
# --------------------------------------------------------------------------- #
def test_resolve_identity_is_order_independent(store):
    rows = parcel_scenario_rows()
    _, s1 = _ingest(store, rows)
    con2 = sqlite3.connect(":memory:")
    store2 = Store(con2)
    try:
        _, s2 = _ingest(store2, rows[::-1])
        assert s1["fingerprint"] == s2["fingerprint"]
    finally:
        con2.close()


def test_duplicate_rows_create_no_extra_entities_or_relations(store):
    rows = parcel_scenario_rows()
    _, s1 = _ingest(store, rows)
    _, s2 = _ingest(store, rows + rows)
    assert s1["fingerprint"] == s2["fingerprint"]
    assert store.count() == len({identity.content_hash(r) for r in rows})


# --------------------------------------------------------------------------- #
# Backward compatibility: legacy tables and helpers keep working
# --------------------------------------------------------------------------- #
def test_legacy_store_contract_remains_intact(store):
    record = {"id": "sample", "ts": "t1", "v": 1}
    assert store.upsert(record) is True
    assert store.upsert(record) is False
    store.set_watermark("checkpoint")
    assert store.watermark() == "checkpoint"
    assert store.count() >= 1
    assert store.get(Store.hash(record))["id"] == "sample"


def test_legacy_kernel_block_rule_is_still_available():
    from landparcel.kernel import process, reduce
    out = process({"id": "x", "name": "Alpha Widget"})
    assert "block_key" in out
    reduced = reduce([{"id": "1", "block_key": "a"},
                      {"id": "2", "block_key": "a"}])
    assert reduced[0]["cluster_size"] == 2
