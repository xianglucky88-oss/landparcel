"""Local transport, parsing, storage, and session helpers."""
from __future__ import annotations
import hashlib
import importlib
import json
import uuid

from lxml import html as _lh

from .config import SPEC
from .store import Store
from . import identity


# --------------------------------------------------------------------------- #
# Transport helpers.
# --------------------------------------------------------------------------- #
class Transport:
    """Client for the bundled local service."""

    def __init__(self):
        import sys
        for p in (str(SPEC_ROOT()), str(SPEC_ROOT() / "src")):
            if p not in sys.path:
                sys.path.insert(0, p)
        from starlette.testclient import TestClient
        app = importlib.import_module("mockapp.main").app
        self.client = TestClient(app)
        self.cookies: dict[str, str] = {}

    def request(self, method, path, params=None, json_body=None, headers=None):
        q = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        resp = self.client.request(method, path, params=q, json=json_body,
                                   headers=headers or {}, cookies=self.cookies)
        for k, v in resp.cookies.items():
            self.cookies[k] = v
        return resp.status_code, dict(resp.headers), resp.text


def SPEC_ROOT():
    from .config import ROOT
    return ROOT


class Sleeper:
    """Records requested delays without blocking the caller."""

    def __init__(self):
        self.slept = 0.0
        self.calls = 0

    def sleep(self, seconds):
        self.slept += seconds
        self.calls += 1


class Http:
    """HTTP client with bounded retry support."""

    def __init__(self, transport: Transport, sleeper: Sleeper,
                 max_retries=4, base_backoff=0.5, politeness=0.25):
        self.t = transport
        self.sleeper = sleeper
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.politeness = politeness

    def get(self, path, params=None, headers=None):
        attempt = 0
        while True:
            self.sleeper.sleep(self.politeness)
            status, hdr, body = self.t.request("GET", path, params, headers=headers)
            if status in (429, 500, 502, 503) and attempt < self.max_retries:
                self.sleeper.sleep(self.base_backoff * (2 ** attempt))
                attempt += 1
                continue
            return status, hdr, body, attempt


# --------------------------------------------------------------------------- #
# Response parsing.
# --------------------------------------------------------------------------- #
def _coerce(name, val):
    if name in SPEC.int_fields:
        try:
            return int(val)
        except (TypeError, ValueError):
            return val
    return val


def parse(resp_body, fmt):
    cols = SPEC.columns
    if fmt == "json":
        items = json.loads(resp_body).get("items", [])
        return [{c: _coerce(c, it.get(c)) for c in cols} for it in items]
    if fmt == "html":
        doc = _lh.fromstring(resp_body)
        rows = []
        for tr in doc.xpath("//table//tr[./td]"):
            cells = [td.text_content().strip() for td in tr.xpath("./td")]
            if len(cells) != len(cols):
                continue
            rows.append({c: _coerce(c, v) for c, v in zip(cols, cells)})
        return rows
    if fmt == "text":
        out = []
        for line in resp_body.splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split("\t")]
            d = {c: _coerce(c, parts[i]) if i < len(parts) else _coerce(c, "")
                 for i, c in enumerate(cols)}
            out.append(d)
        return out
    raise ValueError("unknown fmt: %s" % fmt)


def fetch_all(http: Http):
    """Collect records from the local source."""
    records, attempts, page = [], 0, 1
    while True:
        status, _hdr, body, att = http.get("/" + SPEC.endpoint, params={"page": page})
        attempts += att + 1
        if status != 200:
            raise RuntimeError("fetch failed status=%s page=%s" % (status, page))
        page_recs = parse(body, SPEC.fmt)
        records.extend(page_recs)
        if len(page_recs) < SPEC.page_size:
            break
        page += 1
        if page > 200:
            break
    return records, attempts


# --------------------------------------------------------------------------- #
# Record identity helpers.
# --------------------------------------------------------------------------- #
def canonical(d):
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def content_hash(d):
    return hashlib.sha256(canonical(d).encode()).hexdigest()[:24]


def dedup(records):
    seen, out, dup = set(), [], 0
    for r in records:
        h = content_hash(r)
        if h in seen:
            dup += 1
            continue
        seen.add(h)
        out.append(r)
    return out, dup


def normalize(r):
    d = {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
    if isinstance(d.get("status"), str):
        d["status"] = d["status"].lower()
    return d


def _apply_kernel(records):
    k = importlib.import_module(".kernel", __package__)
    for r in records:
        if hasattr(k, "process"):
            r.update(k.process(r))
    if hasattr(k, "reduce"):
        records = k.reduce(records)
    return records


# --------------------------------------------------------------------------- #
# Parcel identity orchestration.
# --------------------------------------------------------------------------- #
def resolve_identity(store: Store, incoming, mode: str):
    """Recompute the identity graph over every known record.

    The graph is always rebuilt from the *full* record set (stored rows plus
    the new batch), which makes historical replay independent of how the data
    originally arrived.  Manual decisions are loaded first and always win.
    Returns the annotated incoming batch plus an audit summary.
    """
    run_id = "run_" + uuid.uuid4().hex[:16]
    started = identity.utc_now_iso()
    store.start_run(run_id, mode, started)
    try:
        history = [identity.strip_annotations(r) for r in store.all_records()]
        combined = history + [identity.strip_annotations(dict(r))
                              for r in incoming]
        graph = identity.build_identity(combined, store.load_decisions())

        ts = identity.utc_now_iso()
        with store.transaction():
            for ent in graph["entities"]:
                store.upsert_entity(ent, ts)
            member_rows: dict[str, list[tuple[str, str]]] = {}
            for ent_id, h, ev in graph["members"]:
                member_rows.setdefault(ent_id, []).append((h, ev))
            for ent_id, rows in member_rows.items():
                store.replace_members(ent_id, rows)
            for rel in graph["relations"]:
                store.upsert_relation(rel, ts)

        by_hash = {identity.identity_hash(identity.strip_annotations(r)): r
                   for r in graph["annotated"]}
        incoming_hashes = {
            identity.identity_hash(identity.strip_annotations(r))
            for r in incoming}
        annotated = [by_hash[h] for h in incoming_hashes if h in by_hash]
        state_counts = {identity.STATE_CANDIDATE: 0,
                        identity.STATE_CONFLICT: 0,
                        identity.STATE_CONFIRMED: 0,
                        identity.STATE_REJECTED: 0}
        for rel in graph["relations"]:
            state_counts[graph["decided"].get(rel["relation_id"],
                                              rel["state"])] += 1
        store.finish_run(run_id, "ok", graph["fingerprint"],
                         len(graph["annotated"]), len(graph["relations"]),
                         identity.utc_now_iso())
        summary = {
            "run_id": run_id,
            "rule_version": graph["rule_version"],
            "fingerprint": graph["fingerprint"],
            # Totals describe the full rebuilt graph (replay-safe); the
            # incoming-only counters describe what this batch contributed.
            "entities": len(graph["entities"]),
            "relations": len(graph["relations"]),
            "candidates": state_counts[identity.STATE_CANDIDATE],
            "conflicts": state_counts[identity.STATE_CONFLICT],
            "confirmed": state_counts[identity.STATE_CONFIRMED],
            "rejected": state_counts[identity.STATE_REJECTED],
            "incoming_records": len(incoming_hashes),
        }
        return annotated, summary
    except Exception as exc:  # partial failure must be visible, not silent
        store.finish_run(run_id, "failed", None, 0, 0,
                         identity.utc_now_iso(), error=repr(exc))
        raise


# --------------------------------------------------------------------------- #
# Collection pipeline.
# --------------------------------------------------------------------------- #
def run(mode="full", con=None):
    own = con is None
    con = con or Store().connect()
    store = Store(con)
    sleeper = Sleeper()
    http = Http(Transport(), sleeper)
    try:
        records, attempts = fetch_all(http)
        norm = [normalize(dict(r)) for r in records]
        norm, dup_in_batch = dedup(norm)
        norm = _apply_kernel(norm)

        wm = store.watermark()
        incoming = norm
        if mode == "incremental" and wm:
            incoming = [r for r in norm if str(r.get("ts", "")) > wm]

        # Identity is resolved over the full history before persistence, so a
        # partial failure during the write rolls the graph back atomically.
        annotated, id_summary = resolve_identity(store, incoming, mode)
        with store.transaction():
            ingested = sum(1 for r in annotated if store.upsert(r))
        total = store.count()
        new_wm = max([str(r.get("ts", "")) for r in norm] + [wm or ""])
        if new_wm:
            store.set_watermark(new_wm)
        return {"engine": "crawl", "mode": mode, "fetched": len(records),
                "duplicates": dup_in_batch, "ingested": ingested, "count": total,
                "http_attempts": attempts, "slept": round(sleeper.slept, 4),
                "watermark": store.watermark(), "identity": id_summary}
    finally:
        if own:
            con.close()


# --------------------------------------------------------------------------- #
# Session pipeline.
# --------------------------------------------------------------------------- #
class Rpa:
    def __init__(self):
        self.t = Transport()
        self.sleeper = Sleeper()
        self.log = []
        self.captcha_solved = False

    def login(self):
        status, _h, _b = self.t.request("POST", "/login",
                                        json_body={"u": "operator", "p": "local"})
        if status != 200:
            raise RuntimeError("login failed status=%s" % status)
        self.log.append("login")
        return True

    def _csrf(self):
        return {"x-csrf": self.t.cookies.get("csrf", "")}

    def _query_page(self, page):
        status, _h, body = self.t.request("GET", "/query", params={"page": page},
                                          headers=self._csrf())
        if status != 200:
            raise RuntimeError("query failed status=%s" % status)
        return json.loads(body)

    def collect(self):
        out, page = [], 1
        while True:
            self.sleeper.sleep(0.1)
            data = self._query_page(page)
            out.extend(data.get("items", []))
            if data.get("captcha_required") and not self.captcha_solved:
                ok = self._solve_captcha()
                if not ok:
                    raise RuntimeError("captcha solve rejected")
                self.captcha_solved = True
                page += 1
                continue
            if not data.get("has_more"):
                break
            page += 1
            if page > 200:
                break
        self.log.append("collect:%d" % len(out))
        return out

    def _solve_captcha(self):
        status, _h, body = self.t.request("POST", "/captcha",
                                          json_body={"code": self.t.cookies.get("challenge", "")},
                                          headers=self._csrf())
        ok = status == 200 and json.loads(body).get("ok") is True
        self.log.append("captcha:%s" % ("solved" if ok else "failed"))
        return ok

    def submit(self, record):
        token = content_hash(record)
        status, _h, body = self.t.request("POST", "/submit",
                                          json_body={"token": token, "record": record},
                                          headers=self._csrf())
        data = json.loads(body)
        self.log.append("submit:%s" % ("created" if data.get("created") else "dedup"))
        return bool(data.get("created"))


def run_rpa(con=None):
    own = con is None
    con = con or Store().connect()
    store = Store(con)
    r = Rpa()
    try:
        logged = r.login()
        items = r.collect()
        norm = [normalize(dict(it)) for it in items]
        norm, dup = dedup(norm)
        norm = _apply_kernel(norm)
        annotated, id_summary = resolve_identity(store, norm, "full")
        with store.transaction():
            ingested = sum(1 for x in annotated if store.upsert(x))
        total = store.count()
        created = dedup_submit = 0
        for x in annotated[:2]:
            if r.submit(x):
                created += 1
            else:
                dedup_submit += 1
        for x in annotated[:1]:
            if not r.submit(x):
                dedup_submit += 1
        wm = max([str(x.get("ts", "")) for x in norm] + [""])
        if wm:
            store.set_watermark(wm)
        return {"engine": "rpa", "logged_in": logged, "captcha_solved": r.captcha_solved,
                "collected": len(items), "fetched": len(items), "duplicates": dup,
                "ingested": ingested, "count": total,
                "submit_created": created, "submit_dedup": dedup_submit, "log": r.log,
                "identity": id_summary}
    finally:
        if own:
            con.close()
