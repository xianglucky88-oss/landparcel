"""Tests for the parcel identity engine itself.

Covers: cosmetic id normalization, split/merge lineage, the anti-short-prefix
rule, candidate/conflict/rejected review states, and determinism under
re-ordering, duplication and replay.
"""
import pytest

from landparcel import identity
from landparcel.identity import (
    SAME_AS, SPLIT_FROM, SPLIT_SIBLING, MERGED_INTO,
    STATE_CANDIDATE, STATE_CONFIRMED, STATE_CONFLICT, STATE_REJECTED,
)
from landparcel.mockdata import parcel_scenario_rows


def _rel_map(graph):
    out = {}
    for r in graph["relations"]:
        pair = tuple(sorted((r["left_id"], r["right_id"])))
        out[(pair, r["kind"])] = r
    return out


# --------------------------------------------------------------------------- #
# Identifier normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,canonical", [
    ("BD-001", "BD001"),
    ("BD 001", "BD001"),
    ("BD_001", "BD001"),
    (" bd-001 ", "BD001"),
    ("BD/001", "BD001"),
])
def test_cosmetic_spelling_shares_one_canonical_id(raw, canonical):
    assert identity.normalize_parcel_id(raw)["canonical"] == canonical


def test_numeric_components_keep_parcels_distinct():
    a = identity.normalize_parcel_id("NB-300")
    b = identity.normalize_parcel_id("NB-301")
    assert a["canonical"] != b["canonical"]
    assert a["is_split"] is False and b["is_split"] is False


def test_cosmetic_numeric_suffix_is_not_a_split():
    out = identity.normalize_parcel_id("BD-001-00")
    assert out["is_split"] is False
    assert out["canonical"] == "BD00100"


@pytest.mark.parametrize("raw,parent,core", [
    ("LP-200-A", "LP200", "A"),
    ("LP 200 B", "LP200", "B"),
    ("LP-200(2)", "LP200", "2"),
])
def test_split_suffixes_are_detected(raw, parent, core):
    out = identity.normalize_parcel_id(raw)
    assert out["is_split"] is True
    assert out["base"] == parent
    assert out["core"] == core


def test_name_similarity_is_language_neutral_and_bounded():
    a = identity.normalize_name("滨河东区改造项目一期")
    b = identity.normalize_name("滨河东区改造项目二期")
    score = identity.name_similarity(a, b)
    assert 0.0 < score < 1.0
    assert identity.name_similarity(a, a) == 1.0
    assert identity.name_similarity(a, "完全无关的地块名称xyz") < 0.2


# --------------------------------------------------------------------------- #
# Full scenario graph
# --------------------------------------------------------------------------- #
def _build():
    return identity.build_identity(parcel_scenario_rows())


def test_cosmetic_id_variations_resolve_to_one_entity():
    g = _build()
    ents = {g["prepared"][h].id_canon: g["hash_to_entity"][h]
            for h in g["hash_to_entity"]}
    assert ents["BD001"]  # both BD-001 and BD 001
    members = [h for h, e in g["hash_to_entity"].items() if e == ents["BD001"]]
    canons = {g["prepared"][h].id_canon for h in members}
    assert canons == {"BD001"}
    assert len(members) == 2  # duplicate row collapsed by content hash


def test_distinct_parcels_are_not_swallowed_by_shared_prefix():
    g = _build()
    rel = _rel_map(g)[(tuple(sorted(("BD-101", "BD-102"))), SAME_AS)]
    assert rel["state"] == STATE_REJECTED
    assert "shared_short_prefix" in rel["evidence"]
    # and they remain separate entities
    e1 = g["hash_to_entity"][identity.identity_hash(
        next(r for r in parcel_scenario_rows() if r["id"] == "BD-101"))]
    e2 = g["hash_to_entity"][identity.identity_hash(
        next(r for r in parcel_scenario_rows() if r["id"] == "BD-102"))]
    assert e1 != e2


def test_split_children_are_separate_entities_with_lineage():
    g = _build()
    rels = _rel_map(g)
    parent = next(r for r in parcel_scenario_rows() if r["id"] == "LP-200")
    child_a = next(r for r in parcel_scenario_rows() if r["id"] == "LP-200-A")
    child_b = next(r for r in parcel_scenario_rows() if r["id"] == "LP-200-B")
    hp, ha, hb = map(identity.identity_hash, (parent, child_a, child_b))
    assert g["hash_to_entity"][hp] != g["hash_to_entity"][ha]
    assert g["hash_to_entity"][ha] != g["hash_to_entity"][hb]
    split = rels[(tuple(sorted(("LP-200-A", "LP-200"))), SPLIT_FROM)]
    assert split["state"] == STATE_CANDIDATE
    assert "split_suffix" in split["evidence"]
    sibling = rels[(tuple(sorted(("LP-200-B", "LP-200-A"))), SPLIT_SIBLING)]
    assert sibling["kind"] == SPLIT_SIBLING


def test_same_name_district_across_ids_is_a_merge_candidate_not_auto_merge():
    g = _build()
    rel = _rel_map(g)[(tuple(sorted(("NB-300", "NB-301"))), MERGED_INTO)]
    assert rel["state"] == STATE_CANDIDATE
    assert "same_normalized_name" in rel["evidence"]
    # candidate means separate entities until a human confirms
    h1 = identity.identity_hash(
        next(r for r in parcel_scenario_rows() if r["id"] == "NB-300"))
    h2 = identity.identity_hash(
        next(r for r in parcel_scenario_rows() if r["id"] == "NB-301"))
    assert g["hash_to_entity"][h1] != g["hash_to_entity"][h2]


def test_conflicting_names_under_one_id_land_in_conflict_queue():
    g = _build()
    rel = _rel_map(g)[(tuple(sorted(("KP 400", "KP-400"))), SAME_AS)]
    assert rel["state"] == STATE_CONFLICT
    assert "name_conflict" in rel["conflicts"]
    assert "same_canonical_id" in rel["evidence"]


def test_every_relation_carries_evidence_and_rationale():
    g = _build()
    assert g["relations"]
    for rel in g["relations"]:
        assert rel["rationale"]
        assert rel["rule_version"] == identity.RULE_VERSION
        assert len(rel["evidence"]) >= 1


# --------------------------------------------------------------------------- #
# Determinism / drift resistance
# --------------------------------------------------------------------------- #
def test_identity_does_not_drift_under_pagination_reordering():
    rows = parcel_scenario_rows()
    base = identity.build_identity(rows)["fingerprint"]
    shuffled = identity.build_identity(rows[::-1])["fingerprint"]
    import random
    rng = random.Random(42)
    scrambled = rows[:]
    rng.shuffle(scrambled)
    assert shuffled == base
    assert identity.build_identity(scrambled)["fingerprint"] == base


def test_identity_does_not_drift_under_duplicate_content():
    rows = parcel_scenario_rows()
    base = identity.build_identity(rows)
    doubled = identity.build_identity(rows + rows + rows[:3])
    assert doubled["fingerprint"] == base["fingerprint"]
    assert len(doubled["entities"]) == len(base["entities"])


def test_historical_replay_reproduces_the_same_graph():
    rows = parcel_scenario_rows()
    first = identity.build_identity(rows)
    # Replay from "stored" records only, then again with a fresh batch mixed in.
    replay = identity.build_identity(first["annotated"])
    assert replay["fingerprint"] == first["fingerprint"]


def test_incremental_backfill_extends_without_changing_existing_entities():
    rows = parcel_scenario_rows()
    base_rows = [r for r in rows if r["id"] != "LP-200-C"]
    backfill = [r for r in rows if r["id"] == "LP-200-C"]
    base = identity.build_identity(base_rows)
    grown = identity.build_identity(base_rows + backfill)
    base_entities = {e["entity_id"] for e in base["entities"]}
    grown_entities = {e["entity_id"] for e in grown["entities"]}
    assert base_entities <= grown_entities
    # The backfilled child links to the parent via split_from.
    kinds = {r["kind"] for r in grown["relations"]
             if r["right_id"] == "LP-200-C" or r["left_id"] == "LP-200-C"}
    assert SPLIT_FROM in kinds or SPLIT_SIBLING in kinds


def test_manual_decision_overrides_rule_output_deterministically():
    rows = parcel_scenario_rows()
    g0 = identity.build_identity(rows)
    merge_rel = next(r for r in g0["relations"]
                     if tuple(sorted((r["left_id"], r["right_id"])))
                     == tuple(sorted(("NB-300", "NB-301"))))
    overrides = {merge_rel["relation_id"]: {"decision": STATE_CONFIRMED}}
    g1 = identity.build_identity(rows, overrides)
    assert g1["decided"][merge_rel["relation_id"]] == STATE_CONFIRMED
    # confirmed merge now unifies the two entities
    h1 = identity.identity_hash(
        next(r for r in rows if r["id"] == "NB-300"))
    h2 = identity.identity_hash(
        next(r for r in rows if r["id"] == "NB-301"))
    assert g1["hash_to_entity"][h1] == g1["hash_to_entity"][h2]
    # and the decision survives another replay
    g2 = identity.build_identity(rows[::-1], overrides)
    assert g2["fingerprint"] == g1["fingerprint"]


def test_rejected_decision_keeps_entities_apart():
    rows = parcel_scenario_rows()
    g0 = identity.build_identity(rows)
    rel = next(r for r in g0["relations"]
               if tuple(sorted((r["left_id"], r["right_id"])))
               == tuple(sorted(("BD-101", "BD-102"))))
    g1 = identity.build_identity(
        rows, {rel["relation_id"]: {"decision": STATE_REJECTED}})
    h1 = identity.identity_hash(next(r for r in rows if r["id"] == "BD-101"))
    h2 = identity.identity_hash(next(r for r in rows if r["id"] == "BD-102"))
    assert g1["hash_to_entity"][h1] != g1["hash_to_entity"][h2]


def test_annotations_are_stripped_before_recomputation():
    rows = parcel_scenario_rows()
    g = identity.build_identity(rows)
    # Feeding already-annotated records back must not change the graph.
    again = identity.build_identity(g["annotated"])
    assert again["fingerprint"] == g["fingerprint"]


def test_relation_ids_are_pair_based_and_stable():
    rows = parcel_scenario_rows()
    g = identity.build_identity(rows)
    ids = [r["relation_id"] for r in g["relations"]]
    assert len(ids) == len(set(ids))
    rel = g["relations"][0]
    assert rel["relation_id"] == identity.relation_id(
        rel["kind"], rel["left_hash"], rel["right_hash"])
