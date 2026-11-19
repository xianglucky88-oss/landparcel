"""Reviewer-facing operations: manual conclusions and old-rule recomputation.

These helpers sit on top of :mod:`landparcel.identity` and never change the
deterministic rule output themselves.  A manual decision is stored with its
basis (依据) and reviewer, and from then on wins over rule output during every
replay.
"""
from __future__ import annotations

import json

from . import identity
from .identity import (RULE_VERSION, STATE_CONFIRMED, STATE_REJECTED,
                       content_hash, normalize_name, strip_annotations)
from .store import Store

VALID_DECISIONS = (STATE_CONFIRMED, STATE_REJECTED)


def decide(store: Store, relation_id: str, decision: str,
           reviewer: str, basis: str) -> dict:
    """Record a manual conclusion for a candidate/conflict relation."""
    if decision not in VALID_DECISIONS:
        raise ValueError("decision must be one of %r" % (VALID_DECISIONS,))
    if not basis or not str(basis).strip():
        raise ValueError("a manual decision requires a non-empty basis")
    relations = {r["relation_id"]: r for r in store.all_relations()}
    if relation_id not in relations:
        raise KeyError("unknown relation_id: %s" % relation_id)
    ts = identity.utc_now_iso()
    store.save_decision(relation_id, decision, reviewer, basis, ts)

    # Rebuild the graph immediately so entity membership reflects the call.
    records = [strip_annotations(r) for r in store.all_records()]
    graph = identity.build_identity(records, store.load_decisions())
    with store.transaction():
        for ent in graph["entities"]:
            store.upsert_entity(ent, ts)
        rows: dict[str, list[tuple[str, str]]] = {}
        for ent_id, h, ev in graph["members"]:
            rows.setdefault(ent_id, []).append((h, ev))
        for ent_id, member_rows in rows.items():
            store.replace_members(ent_id, member_rows)
        for rel in graph["relations"]:
            store.upsert_relation(rel, ts)
    out = dict(relations[relation_id])
    out["state"] = decision
    out["reviewer"] = reviewer
    out["basis"] = basis
    out["decided_ts"] = ts
    return out


# --------------------------------------------------------------------------- #
# Legacy rule (kept verbatim in spirit from the original kernel block rule)
# --------------------------------------------------------------------------- #
def legacy_block_key(record: dict) -> str:
    """The pre-2026 rule: first six chars of the flattened lower-cased name."""
    nm = normalize_name(record.get("name", ""))
    return nm[:6] or "x"


def recompute_legacy(store: Store) -> dict:
    """Re-run the *old* short-prefix rule over stored records.

    The old rule grouped records by the first six characters of the name.
    Every multi-record legacy cluster is reconciled against the new engine:

    * pairs the new engine explicitly rejected (shared-prefix evidence) are
      ``erroneous_merges`` (错误归并) -- the old rule swallowed distinct
      parcels;
    * pairs the new engine leaves as candidate/conflict are
      ``pending_legacy_merges`` -- merges the old rule made silently that a
      human must now confirm or reject;
    * pairs joined by a confirmed same-as relation are ``kept_merges``.

    Nothing is modified; the function is pure audit output.
    """
    records = [strip_annotations(r) for r in store.all_records()]
    clusters: dict[str, list[str]] = {}
    for rec in records:
        clusters.setdefault(legacy_block_key(rec), []).append(content_hash(rec))
    key = {content_hash(r): r for r in records}
    full_hash = {content_hash(r): identity.identity_hash(r) for r in records}

    relations = store.all_relations()
    decisions = store.load_decisions()
    rel_index: dict[tuple[str, str], dict] = {}
    for rel in relations:
        rel_index[tuple(sorted((rel["left_hash"], rel["right_hash"])))] = rel

    erroneous, pending, kept = [], [], []
    for block, hashes in sorted(clusters.items()):
        if len(hashes) < 2:
            continue
        hashes = sorted(hashes)
        for i in range(len(hashes)):
            for j in range(i + 1, len(hashes)):
                ha, hb = hashes[i], hashes[j]
                a, b = key[ha], key[hb]
                pa, pb = identity.prepare(a), identity.prepare(b)
                entry = {
                    "legacy_block": block,
                    "left_id": a.get("id", ""),
                    "right_id": b.get("id", ""),
                    "left_name": a.get("name", ""),
                    "right_name": b.get("name", ""),
                }
                # Identifier-backed equivalence is legitimate under both
                # rules, so it is not counted as an old-rule mistake.
                if pa.id_base and pb.id_base and pa.id_base == pb.id_base:
                    continue
                rel = rel_index.get(tuple(sorted(
                    (full_hash[ha], full_hash[hb]))))
                if rel is None:
                    # The new engine does not even see a relationship here:
                    # the old cluster was purely prefix-driven.
                    erroneous.append(entry)
                    continue
                evidence = json.loads(rel.get("evidence") or "[]")
                state = decisions.get(rel["relation_id"], {}).get(
                    "decision", rel["state"])
                entry["relation_id"] = rel["relation_id"]
                entry["new_state"] = state
                if state == STATE_REJECTED or "shared_short_prefix" in evidence:
                    erroneous.append(entry)
                elif state == STATE_CONFIRMED and rel["kind"] == identity.SAME_AS:
                    kept.append(entry)
                else:
                    pending.append(entry)
    return {
        "legacy_rule": "name-prefix-6",
        "current_rule": RULE_VERSION,
        "cluster_count": len(clusters),
        "clusters": {k: sorted(v) for k, v in sorted(clusters.items())},
        "erroneous_merges": erroneous,
        "pending_legacy_merges": pending,
        "kept_merges": kept,
    }
