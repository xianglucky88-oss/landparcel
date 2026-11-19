
"""Build deterministic source rows for the local fixture."""
import hashlib
from datetime import datetime, timedelta, timezone
from .config import SPEC

NAMES = ["Alpha Widget", "Beta Gadget", "Gamma Set", "Delta Unit"]
STATUS = ["new", "active", "pending", "sold", "withdrawn"]
SOURCE_START = datetime(2030, 1, 1, tzinfo=timezone.utc)

def _h(salt, i):
    return int(hashlib.md5(("%s-%s-%d" % (SPEC.slug, salt, i)).encode()).hexdigest()[:6], 16)

def build_rows():
    cols = SPEC.columns
    ps = SPEC.page_size
    n = ps * 2 + 1
    rows = []
    for i in range(1, n + 1):
        d = {}
        for c in cols:
            if c == "id":
                d[c] = "%s-%04d" % (SPEC.slug[:3].upper(), i)
            elif c == "name":
                d[c] = NAMES[(i - 1) % len(NAMES)]
            elif c == "value":
                d[c] = _h("v", i)
            elif c == "status":
                d[c] = STATUS[(i - 1) % len(STATUS)]
            elif c == "ts":
                d[c] = (SOURCE_START + timedelta(days=i % 5)).isoformat().replace("+00:00", "Z")
            else:
                d[c] = "%s_%d" % (c, _h(c, i) % 97)
        rows.append(d)
    rows.append(dict(rows[0]))
    return rows

def extra_row(k):
    d = {}
    for c in SPEC.columns:
        if c == "id":
            d[c] = "%s-X%04d" % (SPEC.slug[:3].upper(), k)
        elif c == "ts":
            d[c] = (SOURCE_START + timedelta(days=30 + k)).isoformat().replace("+00:00", "Z")
        elif c == "name":
            d[c] = NAMES[k % len(NAMES)]
        elif c == "value":
            d[c] = _h("x", k)
        elif c == "status":
            d[c] = "active"
        else:
            d[c] = "%s_new_%d" % (c, k)
    return d


# --------------------------------------------------------------------------- #
# Parcel identity fixture (used by the identity test-suite only; the bundled
# source produced by build_rows() is intentionally left unchanged).
# --------------------------------------------------------------------------- #
def parcel_scenario_rows():
    """Deterministic parcel records covering every identity edge case.

    Scenarios:
      1. cosmetic id variation (spaces/hyphens/suffix stamps) -> same parcel
      2. shared short name prefix -> must remain distinct (old-rule failure)
      3. split suffix -> split_from lineage, separate entities
      4. same name+district under different ids -> merge candidate
      5. same base id but conflicting names -> conflict queue
      6. exact duplicate row -> content dedup, never a second entity
      7. incremental backfill row (later timestamp, new split child)
    """
    base_ts = datetime(2031, 6, 1, tzinfo=timezone.utc)

    def row(i, id_, name, district, ts_off=0, status="active", value=None):
        return {
            "id": id_,
            "name": name,
            "value": value if value is not None else 1000 + i,
            "status": status,
            "ts": (base_ts + timedelta(days=ts_off)).isoformat().replace(
                "+00:00", "Z"),
            "district": district,
        }

    return [
        # 1a-1c: the same physical parcel under cosmetic id spellings.
        row(1, "BD-001", "滨河东路1号地块", "河东区", 0),
        row(2, "BD 001", "滨河东路1号地块", "河东区", 1),
        row(3, "BD-001-00", "滨河东路1号地块", "河东区", 2),
        # 2a-2b: common short prefix, genuinely distinct parcels.
        row(4, "BD-101", "滨河东区改造项目一期", "河东区", 1),
        row(5, "BD-102", "滨河东区改造项目二期", "河东区", 1),
        # 3a/3b: parcel split into two children.
        row(6, "LP-200", "老城区仓库地块", "城西区", 0),
        row(7, "LP-200-A", "老城区仓库地块（北区）", "城西区", 3),
        row(8, "LP-200-B", "老城区仓库地块（南区）", "城西区", 3),
        # 4a/4b: merge suspects -- different ids, identical name & district.
        row(9, "NB-300", "南湖综合开发地块", "南湖区", 2),
        row(10, "NB-301", "南湖综合开发地块", "南湖区", 2),
        # 5a/5b: same base id, but names disagree -> conflict, not auto-merge.
        row(11, "KP-400", "科技园北区地块", "高新区", 4),
        row(12, "KP 400", "科技园南区地块", "高新区", 4),
        # 6: exact duplicate of row 1.
        row(1, "BD-001", "滨河东路1号地块", "河东区", 0),
        # 7: incremental backfill -- a later split child of LP-200.
        row(13, "LP-200-C", "老城区仓库地块（东区）", "城西区", 9),
    ]
