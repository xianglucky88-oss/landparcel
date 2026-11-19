"""Parcel identity resolution.

Why this module exists
----------------------
After a parcel (宗地) is split or merged, similar names are no longer enough to
decide whether two records describe the same real-world entity.  At the same
time, cosmetic changes inside an identifier -- extra spaces, different
hyphenation, or a re-stamped suffix -- must not manufacture meaningless new
parcels.

This module replaces name-prefix clustering with *evidence based* identity
work:

* candidate relations between records are derived from several independent
  signals (canonical id, split lineage, exact / near name, district);
* every relation keeps its supporting evidence and a review state, so an
  auditor can see candidate links, unresolved conflicts, manual conclusions
  and the rationale behind them;
* a shared *short* prefix is explicitly treated as rejection evidence, so
  distinct parcels can never be swallowed by a common name stem;
* all outputs are deterministic and order-independent, which makes historical
  replay and drift detection possible.

The module never deletes or silently rewrites data.  Manual decisions live in
their own table and always win over rule output on replay.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# Bumped whenever the rules below change.  Every emitted relation and every
# run row records this version, so old-rule recomputation stays traceable.
RULE_VERSION = "identity-2026.11.0"

# Relation kinds.
SAME_AS = "same_as"          # records believed to describe one parcel
SPLIT_FROM = "split_from"    # right record is a split child of left parcel
SPLIT_SIBLING = "split_sibling"  # two children split from the same parent
MERGED_INTO = "merged_into"  # right record is a merge of left parcels

# Review states.
STATE_CANDIDATE = "candidate"        # rule proposed, awaiting human review
STATE_CONFIRMED = "confirmed"        # human accepted (or auto-accepted below)
STATE_REJECTED = "rejected"          # human declined / rule auto-distinct
STATE_CONFLICT = "conflict"          # signals disagree, must be resolved

# Evidence codes -- the audit vocabulary attached to every relation.
EV_SAME_CANONICAL = "same_canonical_id"      # ids equal after normalization
EV_SAME_BASE = "same_base_id"                # cosmetic id variation only
EV_SPLIT_SUFFIX = "split_suffix"             # child suffix (-A / (2)) detected
EV_SAME_NAME = "same_normalized_name"
EV_NEAR_NAME = "near_name"
EV_SAME_DISTRICT = "same_district"
EV_SHORT_PREFIX = "shared_short_prefix"      # anti-merge evidence
EV_NAME_CONFLICT = "name_conflict"
EV_DISTRICT_CONFLICT = "district_conflict"
EV_CROSS_BASE = "cross_base_id"
EV_DUPLICATE_CONTENT = "duplicate_content"

# Tunables, kept in one place so reviewers can audit the thresholds.
NEAR_NAME_THRESHOLD = 0.82
# Below this floor two names share no usable overlap at all.  Pairs in the
# band between floor and NEAR_NAME_THRESHOLD are only retained when they share
# a genuine prefix, in which case the relation is recorded as *rejected* --
# this is the evidence trail for the old short-prefix rule's wrong merges.
NAME_LINK_FLOOR = 0.4
MIN_NEAR_NAME_LEN = 2
# A shared prefix followed by diverging tails proves nothing; the
# _has_short_prefix_evidence helper below turns that into rejection evidence.
# Cosmetic suffixes that never create a new parcel on their own.
_COSMETIC_SUFFIXES = ("00", "01", "NEW", "OLD", "BAK")

# Keys that the engine annotates onto records; stripped before recomputation so
# identity can never feed back into itself.
ANNOTATION_KEYS = ("canonical_id", "base_id", "parcel_entity_id",
                   "identity_rule_version")


# --------------------------------------------------------------------------- #
# Identifier normalization
# --------------------------------------------------------------------------- #
def _collapse_spaces(text: str) -> str:
    return " ".join(text.split())


def normalize_parcel_id(raw: Any) -> dict[str, str]:
    """Normalize a parcel identifier without losing lineage information.

    Returns a dict with:
      * ``canonical`` -- upper-cased form with spaces, underscores, slashes
        and hyphens removed.  Cosmetic spelling differences (``"BD-001"``,
        ``"BD 001"``, ``"BD_001"``) therefore map to the same canonical id.
      * ``base``      -- the identifier before a genuine split suffix, i.e.
        the parent parcel's canonical id (``""`` when there is no split).
      * ``core``      -- trailing split token (``""`` when absent).
      * ``is_split``  -- whether the trailing token looks like a genuine split
        marker (``-A``, ``-2``, ``(3)`` ...) rather than a cosmetic stamp.

    Only the *last* hyphen/underscore-separated token can be a split suffix,
    and it must be short and purely alphabetic (or parenthesised digits);
    ordinary multi-part ids such as ``NB-300`` / ``NB-301`` stay whole and
    distinct, so differing numeric components never merge two parcels.
    """
    text = _collapse_spaces(str(raw if raw is not None else "")).upper()

    # First remember any genuine parenthesised suffix:
    # "LP-200(2)" -> ("LP-200", "2").
    paren_core = ""
    paren_head = ""
    if "(" in text:
        pre, rest = text.split("(", 1)
        if ")" in rest:
            inside, post = rest.split(")", 1)
            if inside.strip() and not post.strip():
                text, paren_core, paren_head = (
                    pre.strip(), inside.strip(), pre.strip())

    # Split off the last dashed/underscore/space-separated token for split
    # detection.  Only an alphabetic token ("-A", " B") or a parenthesised
    # token ("(2)") can mark a genuine split; a trailing *bare numeric*
    # component ("NB-300", "BD-001-00") is an ordinary id part and is never a
    # split, so different numbers can never merge two parcels.
    tail, core, is_split = "", paren_core, bool(paren_core)
    if not core:
        for sep in ("-", "_", " "):
            if sep in text:
                head, cand = text.rsplit(sep, 1)
                cand = cand.strip()
                if head.strip() and cand:
                    tail, core = head, cand
                    break
        is_split = (bool(core) and len(core) <= 3 and core.isalpha()
                    and core not in _COSMETIC_SUFFIXES)
        if not is_split:
            tail, core = "", ""

    flatten = lambda s: "".join(ch for ch in s if ch not in (" ", "-", "_", "/"))
    canonical = flatten(text)
    if is_split:
        base = flatten(paren_head or tail)
    else:
        base = canonical
    return {"canonical": canonical, "base": base, "core": core,
            "is_split": is_split}


def normalize_name(raw: Any) -> str:
    return "".join(str(raw if raw is not None else "").split()).lower()


# --------------------------------------------------------------------------- #
# Name similarity (deterministic, no external deps)
# --------------------------------------------------------------------------- #
def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def _trigrams(text: str) -> set[str]:
    if len(text) < 3:
        return {text} if text else set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def name_similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] based on character-trigram overlap.

    Trigram overlap is language-neutral (it works for Chinese parcel names
    without word segmentation) and fully deterministic.  Identical normalized
    names always score 1.0.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = _trigrams(a), _trigrams(b)
    overlap = len(ta & tb)
    if overlap == 0:
        return 0.0
    return round(overlap / len(ta | tb), 4)


def _has_short_prefix_evidence(a: str, b: str) -> bool:
    """True when the names merely share a stem before diverging on both sides.

    Identical names are handled elsewhere, so here any common prefix followed
    by non-empty, differing tails on both records counts as uninformative
    prefix evidence -- no matter how long the stem is.  This mirrors the old
    rule's failure mode (prefix matching) and turns it into explicit,
    reviewable rejection evidence instead of a silent merge.
    """
    if not a or not b or a == b:
        return False
    shared = _common_prefix_len(a, b)
    return 0 < shared < min(len(a), len(b))


# --------------------------------------------------------------------------- #
# Record preparation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Prepared:
    h: str                 # full collision-resistant identity hash
    chash: str             # 24-char legacy records-table key
    raw: dict[str, Any]
    id_canon: str
    id_base: str
    id_core: str
    is_split: bool
    name_norm: str
    name_raw: str
    district: str


def _sha24(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _sha_full(text: str) -> str:
    # Identity hashes (relation keys, entity keys, fingerprints) use the full
    # digest so deterministic fixture data can never collide.  The 24-char
    # form remains only for compatibility with the legacy records table.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(record: dict[str, Any]) -> str:
    # Matches Store.hash: derived annotations never participate in the key,
    # so identity member rows always join the records-table primary key.
    core = strip_annotations(record)
    blob = json.dumps(core, sort_keys=True, ensure_ascii=False)
    return _sha24(blob)


def identity_hash(record: dict[str, Any]) -> str:
    """Collision-resistant full digest used to label a record in the graph."""
    blob = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return _sha_full(blob)


def prepare(record: dict[str, Any]) -> Prepared:
    ident = normalize_parcel_id(record.get("id", ""))
    return Prepared(
        h=identity_hash(record),
        chash=content_hash(record),
        raw=record,
        id_canon=ident["canonical"],
        id_base=ident["base"],
        id_core=ident["core"],
        is_split=ident["is_split"],
        name_norm=normalize_name(record.get("name", "")),
        name_raw=str(record.get("name", "")),
        district=_collapse_spaces(str(record.get("district", ""))).lower(),
    )


# --------------------------------------------------------------------------- #
# Pairwise evidence evaluation
# --------------------------------------------------------------------------- #
@dataclass
class PairResult:
    kind: str
    state: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    rationale: str = ""


def _ordered(a: Prepared, b: Prepared):
    """Deterministic left/right ordering for a pair."""
    return (a, b) if (a.h, b.h) <= (b.h, a.h) else (b, a)


def evaluate_pair(a: Prepared, b: Prepared) -> PairResult | None:
    """Evaluate the identity relationship between two prepared records.

    Returns ``None`` when there is no worth-recording relation at all.
    """
    lo, hi = _ordered(a, b)
    evidence: list[str] = []
    conflicts: list[str] = []

    same_name = bool(lo.name_norm) and lo.name_norm == hi.name_norm
    near = (name_similarity(lo.name_norm, hi.name_norm)
            if lo.name_norm and hi.name_norm else 0.0)
    near_name = (not same_name and near >= NEAR_NAME_THRESHOLD
                 and len(lo.name_norm) >= MIN_NEAR_NAME_LEN
                 and len(hi.name_norm) >= MIN_NEAR_NAME_LEN)
    has_name_link = near >= NAME_LINK_FLOOR
    same_district = bool(lo.district) and lo.district == hi.district
    district_conflict = bool(lo.district and hi.district
                             and lo.district != hi.district)
    short_prefix = _has_short_prefix_evidence(lo.name_norm, hi.name_norm)
    same_canon = lo.id_canon and lo.id_canon == hi.id_canon
    same_base = lo.id_base and lo.id_base == hi.id_base

    # 1) Split lineage.  Split children are separate parcels: a child vs its
    #    parent is a split_from relation, two children of one parent are
    #    split_sibling relations -- neither ever collapses into same_as.
    child, parent = None, None
    if lo.is_split and lo.id_base == hi.id_canon:
        child, parent = lo, hi
    elif hi.is_split and hi.id_base == lo.id_canon:
        child, parent = hi, lo
    if child is not None:
        ev = [EV_SPLIT_SUFFIX]
        if same_district:
            ev.append(EV_SAME_DISTRICT)
        if district_conflict:
            conflicts.append(EV_DISTRICT_CONFLICT)
        state = STATE_CONFLICT if conflicts else STATE_CANDIDATE
        return PairResult(
            SPLIT_FROM, state, 0.85, ev, conflicts,
            "id %r carries split suffix %r of base parcel %r"
            % (child.id_canon, child.id_core, parent.id_canon))

    if lo.is_split and hi.is_split and same_base:
        ev = [EV_SPLIT_SUFFIX]
        if same_district:
            ev.append(EV_SAME_DISTRICT)
        if district_conflict:
            conflicts.append(EV_DISTRICT_CONFLICT)
        state = STATE_CONFLICT if conflicts else STATE_CANDIDATE
        return PairResult(
            SPLIT_SIBLING, state, 0.8, ev, conflicts,
            "ids %r and %r are split children of base parcel %r"
            % (lo.id_canon, hi.id_canon, lo.id_base))

    # 2) Same canonical/base identifier: cosmetic id variation, never a new
    #    parcel.  (Split children never reach this branch -- handled above.)
    if same_canon or (same_base and not lo.is_split and not hi.is_split):
        if same_canon:
            evidence.append(EV_SAME_CANONICAL)
        if same_base and not same_canon:
            evidence.append(EV_SAME_BASE)
        if same_name:
            evidence.append(EV_SAME_NAME)
        if near_name:
            evidence.append(EV_NEAR_NAME)
        if same_district:
            evidence.append(EV_SAME_DISTRICT)
        if not same_name and lo.name_norm and hi.name_norm:
            conflicts.append(EV_NAME_CONFLICT)
        if district_conflict:
            conflicts.append(EV_DISTRICT_CONFLICT)
        confidence = 0.95 if same_canon else 0.9
        state = STATE_CONFLICT if conflicts else STATE_CONFIRMED
        rationale = "identifiers reduce to the same base id %r" % lo.id_base
        return PairResult(SAME_AS, state, confidence, evidence, conflicts,
                          rationale)

    # 3) Different base ids: look at name / district evidence only.
    #    No usable name overlap at all -> nothing worth auditing.
    if not (same_name or near_name or (short_prefix and has_name_link)):
        return None
    evidence.append(EV_CROSS_BASE)
    if same_name:
        evidence.append(EV_SAME_NAME)
    elif near_name:
        evidence.append(EV_NEAR_NAME)
    if same_district:
        evidence.append(EV_SAME_DISTRICT)
    if district_conflict:
        conflicts.append(EV_DISTRICT_CONFLICT)
    if short_prefix:
        # Explicit anti-merge evidence: a common stem must not merge parcels.
        evidence.append(EV_SHORT_PREFIX)
        confidence = 0.05
        rationale = ("only a short shared name prefix links %r and %r; "
                     "treated as distinct parcels"
                     % (lo.name_raw, hi.name_raw))
        return PairResult(SAME_AS, STATE_REJECTED, confidence, evidence,
                          conflicts, rationale)

    confidence = round(
        min(0.9, (0.7 if same_name else 0.55)
            + (0.1 if same_district else 0.0)
            + (near - NEAR_NAME_THRESHOLD) * 0.2), 4)
    if same_name and same_district:
        # Strong merge signal across different ids -- still human-confirmed.
        kind, state = MERGED_INTO, STATE_CANDIDATE
        rationale = "same name and district under different ids; possible merge"
    else:
        kind, state = SAME_AS, STATE_CANDIDATE
        rationale = "name similarity across different ids; needs confirmation"
    return PairResult(kind, state, confidence, evidence, conflicts, rationale)


def _candidate_pairs(prepared: list[Prepared]) -> Iterable[tuple[Prepared, Prepared]]:
    """Generate bounded candidate pairs via blocking -- never O(n^2) blind.

    Blocks are built on strong keys (base id, full normalized name, name
    trigrams) plus a split-parent index.  The dangerous short-prefix case is
    only *evaluated* inside these blocks, never used as a block key itself;
    trigram blocking guarantees that a pair sharing a long name stem is still
    examined -- and then explicitly rejected when the stem is uninformative.
    """
    blocks: dict[str, list[int]] = {}
    split_parents: dict[str, list[int]] = {}
    for idx, p in enumerate(prepared):
        if p.id_base:
            blocks.setdefault("id:" + p.id_base, []).append(idx)
        if p.name_norm:
            blocks.setdefault("nm:" + p.name_norm, []).append(idx)
            for tri in _trigrams(p.name_norm):
                blocks.setdefault("tg:" + tri, []).append(idx)
        if p.is_split:
            split_parents.setdefault(p.id_base, []).append(idx)

    seen: set[tuple[str, str]] = set()
    for key, idxs in blocks.items():
        idxs = sorted(idxs)
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                i, j = idxs[x], idxs[y]
                pair = tuple(sorted((prepared[i].h, prepared[j].h)))
                if pair not in seen:
                    seen.add(pair)
                    yield prepared[i], prepared[j]
    # Split children against every record carrying the parent canonical id.
    canon_index: dict[str, list[int]] = {}
    for idx, p in enumerate(prepared):
        if p.id_canon:
            canon_index.setdefault(p.id_canon, []).append(idx)
    for base, child_idxs in split_parents.items():
        for ci in child_idxs:
            for pi in canon_index.get(base, []):
                if ci == pi:
                    continue
                pair = tuple(sorted((prepared[ci].h, prepared[pi].h)))
                if pair not in seen:
                    seen.add(pair)
                    yield prepared[ci], prepared[pi]


# --------------------------------------------------------------------------- #
# Identity build (deterministic graph)
# --------------------------------------------------------------------------- #
def relation_id(kind: str, left_h: str, right_h: str) -> str:
    # Identity is pair-based on purpose: a given record pair can only ever
    # hold one relation, so re-evaluation under a slightly different kind
    # updates the same row instead of manufacturing duplicates.
    key = "rel|%s|%s" % tuple(sorted((left_h, right_h)))
    return _sha_full(key)


def entity_id_for(base_id: str, first_seen_h: str) -> str:
    return "ent_" + _sha_full("entity|%s|%s" % (base_id or "UNKNOWN",
                                                first_seen_h))[:24]


def strip_annotations(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in ANNOTATION_KEYS}


def build_identity(records: list[dict[str, Any]],
                   overrides: dict[str, dict[str, Any]] | None = None
                   ) -> dict[str, Any]:
    """Compute the full identity graph for a batch of records.

    Pure function: same inputs (including the same override map) always yield
    the same output, regardless of input ordering or duplicate rows.

    Output keys:
      annotated      -- records with identity annotation keys added
      prepared       -- Prepared objects keyed by content hash
      relations      -- list of relation dicts (rule output)
      entities       -- list of entity dicts
      members        -- list of (entity_id, content_hash, evidence) rows
      fingerprint    -- stable digest of the whole graph
    """
    overrides = overrides or {}
    # Deterministic de-duplication on the full digest (a 24-char truncation
    # can collide on deterministic fixture data); ordering by hash makes the
    # whole build independent of input pagination order.
    unique: dict[str, dict[str, Any]] = {}
    for r in records:
        clean = strip_annotations(dict(r))
        unique[identity_hash(clean)] = clean
    ordered_hashes = sorted(unique)
    prepared = [prepare(unique[h]) for h in ordered_hashes]
    by_hash = {p.h: p for p in prepared}

    relations: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for a, b in _candidate_pairs(prepared):
        result = evaluate_pair(a, b)
        if result is None:
            continue
        lo, hi = _ordered(a, b)
        rel_id = relation_id(result.kind, lo.h, hi.h)
        if rel_id in emitted:
            continue
        emitted.add(rel_id)
        relations.append({
            "relation_id": rel_id,
            "kind": result.kind,
            "state": result.state,
            "confidence": result.confidence,
            "left_hash": lo.h,
            "right_hash": hi.h,
            "left_id": lo.raw.get("id", ""),
            "right_id": hi.raw.get("id", ""),
            "left_name": lo.name_raw,
            "right_name": hi.name_raw,
            "evidence": json.dumps(result.evidence, ensure_ascii=False),
            "conflicts": json.dumps(result.conflicts, ensure_ascii=False),
            "rationale": result.rationale,
            "rule_version": RULE_VERSION,
        })

    # Union-find over same_as / merged_into, honoring manual overrides.
    parent = {h: h for h in ordered_hashes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        # Relations can reference hashes absent from the current batch
        # (replay over a partial history); those edges are simply skipped.
        if x not in parent or y not in parent:
            return
        rx, ry = find(x), find(y)
        if rx != ry:
            # Smallest hash becomes the root for determinism.
            if rx < ry:
                parent[ry] = rx
            else:
                parent[rx] = ry

    decided: dict[str, str] = {}
    for rel in relations:
        rid = rel["relation_id"]
        ov = overrides.get(rid)
        state = ov["decision"] if ov else rel["state"]
        decided[rid] = state
        if state == STATE_CONFIRMED and rel["kind"] in (SAME_AS, MERGED_INTO):
            union(rel["left_hash"], rel["right_hash"])
        if state == STATE_CONFIRMED and rel["kind"] == SPLIT_FROM:
            # A confirmed split keeps child and parent as *separate* entities;
            # lineage is carried by the relation itself.
            pass

    # Entity roots: union-find root per record, with split children naturally
    # forming their own root unless a confirmed same_as links them elsewhere.
    root_members: dict[str, list[str]] = {}
    for h in ordered_hashes:
        root_members.setdefault(find(h), []).append(h)

    entities: list[dict[str, Any]] = []
    members: list[tuple[str, str, str]] = []
    hash_to_entity: dict[str, str] = {}
    for root in sorted(root_members):
        group = sorted(root_members[root])
        base_ids = sorted({by_hash[h].id_base for h in group if by_hash[h].id_base})
        base_id = base_ids[0] if base_ids else ""
        ent_id = entity_id_for(base_id, group[0])
        entities.append({
            "entity_id": ent_id,
            "base_id": base_id,
            "first_seen_hash": group[0],
            "member_count": len(group),
            "rule_version": RULE_VERSION,
        })
        for h in group:
            hash_to_entity[h] = ent_id
            p = by_hash[h]
            ev = EV_SAME_BASE if p.id_base == base_id else "union_root"
            # Member keys use the legacy 24-char record hash so
            # parcel_members joins the records table primary key.
            members.append((ent_id, p.chash, ev))

    annotated: list[dict[str, Any]] = []
    for h in ordered_hashes:
        p = by_hash[h]
        rec = dict(p.raw)
        rec["canonical_id"] = p.id_canon
        rec["base_id"] = p.id_base
        rec["parcel_entity_id"] = hash_to_entity[h]
        rec["identity_rule_version"] = RULE_VERSION
        annotated.append(rec)

    fingerprint = graph_fingerprint(entities, members, relations, decided)
    return {
        "annotated": annotated,
        "prepared": by_hash,
        "relations": relations,
        "decided": decided,
        "entities": entities,
        "members": members,
        "hash_to_entity": hash_to_entity,
        "fingerprint": fingerprint,
        "rule_version": RULE_VERSION,
    }


def graph_fingerprint(entities: list[dict[str, Any]],
                      members: list[tuple[str, str, str]],
                      relations: list[dict[str, Any]],
                      decided: dict[str, str]) -> str:
    payload = {
        "entities": sorted((e["entity_id"], e["base_id"], e["first_seen_hash"])
                           for e in entities),
        "members": sorted((m[0], m[1]) for m in members),
        "relations": sorted((r["relation_id"], r["kind"],
                             decided.get(r["relation_id"], r["state"]))
                            for r in relations),
        "rule_version": RULE_VERSION,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return _sha24(blob)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
