
"""Real sqlite3 store: content-hash primary key, INSERT-OR-IGNORE dedup, watermark.

The original ``records`` / ``meta`` tables are untouched, so every existing
offline entry point and previously written database keeps working.  Additional
tables hold the parcel-identity graph and its audit trail:

* ``parcel_entities``  -- stable entity nodes (one per resolved parcel)
* ``parcel_members``   -- record hash -> entity membership with evidence
* ``parcel_relations`` -- candidate / conflict / decided relations + rationale
* ``identity_runs``    -- run history with fingerprints for replay & drift
* ``identity_decisions`` -- manual conclusions with basis and reviewer
"""
import contextlib
import json, sqlite3
from .config import DB_PATH
from .identity import RULE_VERSION, STATE_REJECTED

class Store:
    def __init__(self, con=None):
        self.con = con
        if con is not None:
            self._init()

    def connect(self):
        con = sqlite3.connect(DB_PATH)
        self.__init__(con)
        return con

    def _init(self):
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS records("
            "content_hash TEXT PRIMARY KEY, id TEXT, ts TEXT, payload TEXT NOT NULL)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        # --- identity graph (additive, never alters legacy tables) ---
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS parcel_entities("
            "entity_id TEXT PRIMARY KEY, base_id TEXT, first_seen_hash TEXT, "
            "member_count INTEGER, rule_version TEXT, created_ts TEXT)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS parcel_members("
            "entity_id TEXT, content_hash TEXT, evidence TEXT, "
            "PRIMARY KEY(entity_id, content_hash))")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS parcel_relations("
            "relation_id TEXT PRIMARY KEY, kind TEXT, state TEXT, "
            "confidence REAL, left_hash TEXT, right_hash TEXT, "
            "left_id TEXT, right_id TEXT, left_name TEXT, right_name TEXT, "
            "evidence TEXT, conflicts TEXT, rationale TEXT, "
            "rule_version TEXT, updated_ts TEXT)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS identity_decisions("
            "relation_id TEXT PRIMARY KEY, decision TEXT, reviewer TEXT, "
            "basis TEXT, decided_ts TEXT)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS identity_runs("
            "run_id TEXT PRIMARY KEY, mode TEXT, rule_version TEXT, "
            "status TEXT, fingerprint TEXT, record_count INTEGER, "
            "relation_count INTEGER, error TEXT, started_ts TEXT, ended_ts TEXT)")
        self.con.commit()

    @contextlib.contextmanager
    def transaction(self):
        """Commit on success, roll back on failure (partial-failure safety)."""
        try:
            yield self.con
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    @staticmethod
    def hash(record):
        # The primary key identifies the source record itself.  Derived
        # identity annotations are stripped first, so an annotation/rule
        # update never creates a duplicate row and the key always joins
        # parcel_members.content_hash.  Legacy records carry no annotation
        # keys, so the behaviour is unchanged for them.
        from .identity import ANNOTATION_KEYS
        core = {k: v for k, v in record.items() if k not in ANNOTATION_KEYS}
        blob = json.dumps(core, sort_keys=True, ensure_ascii=False)
        import hashlib
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def upsert(self, record):
        cur = self.con.execute(
            "INSERT OR IGNORE INTO records(content_hash,id,ts,payload) VALUES(?,?,?,?)",
            (self.hash(record), str(record.get("id", "")),
             str(record.get("ts", "")), json.dumps(record, ensure_ascii=False)))
        self.con.commit()
        return cur.rowcount > 0

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def all_records(self):
        return [json.loads(p) for (_h, p) in
                self.con.execute("SELECT content_hash,payload FROM records")]

    def get(self, content_hash):
        row = self.con.execute(
            "SELECT payload FROM records WHERE content_hash=?", (content_hash,)).fetchone()
        return json.loads(row[0]) if row else None

    def watermark(self):
        row = self.con.execute("SELECT v FROM meta WHERE k='watermark'").fetchone()
        return row[0] if row else None

    def set_watermark(self, value):
        self.con.execute(
            "INSERT INTO meta(k,v) VALUES('watermark',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(value),))
        self.con.commit()

    # ------------------------------------------------------------------ #
    # Identity graph persistence
    # ------------------------------------------------------------------ #
    def upsert_entity(self, entity, ts):
        self.con.execute(
            "INSERT INTO parcel_entities(entity_id,base_id,first_seen_hash,"
            "member_count,rule_version,created_ts) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(entity_id) DO UPDATE SET "
            "base_id=excluded.base_id, member_count=excluded.member_count, "
            "rule_version=excluded.rule_version",
            (entity["entity_id"], entity["base_id"], entity["first_seen_hash"],
             entity["member_count"], entity["rule_version"], ts))

    def replace_members(self, entity_id, rows):
        self.con.execute("DELETE FROM parcel_members WHERE entity_id=?",
                         (entity_id,))
        self.con.executemany(
            "INSERT OR REPLACE INTO parcel_members(entity_id,content_hash,evidence)"
            " VALUES(?,?,?)", [(entity_id, h, ev) for (h, ev) in rows])

    def upsert_relation(self, rel, ts):
        """Insert rule output, but never clobber a human-decided relation."""
        self.con.execute(
            "INSERT INTO parcel_relations(relation_id,kind,state,confidence,"
            "left_hash,right_hash,left_id,right_id,left_name,right_name,"
            "evidence,conflicts,rationale,rule_version,updated_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(relation_id) DO UPDATE SET "
            "confidence=excluded.confidence, evidence=excluded.evidence, "
            "conflicts=excluded.conflicts, rationale=excluded.rationale, "
            "rule_version=excluded.rule_version, updated_ts=excluded.updated_ts",
            (rel["relation_id"], rel["kind"], rel["state"], rel["confidence"],
             rel["left_hash"], rel["right_hash"], rel["left_id"], rel["right_id"],
             rel["left_name"], rel["right_name"], rel["evidence"],
             rel["conflicts"], rel["rationale"], rel["rule_version"], ts))

    def all_relations(self):
        cols = ("relation_id,kind,state,confidence,left_hash,right_hash,"
                "left_id,right_id,left_name,right_name,evidence,conflicts,"
                "rationale,rule_version,updated_ts")
        return [dict(zip(cols.split(","), row)) for row in
                self.con.execute("SELECT %s FROM parcel_relations "
                                 "ORDER BY relation_id" % cols)]

    def all_entities(self):
        cols = ("entity_id,base_id,first_seen_hash,member_count,rule_version,"
                "created_ts")
        return [dict(zip(cols.split(","), row)) for row in
                self.con.execute("SELECT %s FROM parcel_entities "
                                 "ORDER BY entity_id" % cols)]

    def entity_of(self, content_hash):
        row = self.con.execute(
            "SELECT entity_id FROM parcel_members WHERE content_hash=?",
            (content_hash,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ #
    # Manual decisions
    # ------------------------------------------------------------------ #
    def save_decision(self, relation_id, decision, reviewer, basis, ts):
        self.con.execute(
            "INSERT INTO identity_decisions(relation_id,decision,reviewer,"
            "basis,decided_ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(relation_id) DO UPDATE SET decision=excluded.decision,"
            "reviewer=excluded.reviewer,basis=excluded.basis,"
            "decided_ts=excluded.decided_ts",
            (relation_id, decision, reviewer, basis, ts))
        self.con.execute(
            "UPDATE parcel_relations SET state=?, updated_ts=? "
            "WHERE relation_id=?", (decision, ts, relation_id))
        self.con.commit()

    def load_decisions(self):
        cols = "relation_id,decision,reviewer,basis,decided_ts"
        out = {}
        for row in self.con.execute(
                "SELECT %s FROM identity_decisions ORDER BY relation_id" % cols):
            d = dict(zip(cols.split(","), row))
            out[d["relation_id"]] = d
        return out

    # ------------------------------------------------------------------ #
    # Run history (replay / drift)
    # ------------------------------------------------------------------ #
    def start_run(self, run_id, mode, ts):
        self.con.execute(
            "INSERT OR REPLACE INTO identity_runs(run_id,mode,rule_version,"
            "status,fingerprint,record_count,relation_count,error,"
            "started_ts,ended_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, mode, RULE_VERSION, "running", None, 0, 0, None, ts, None))
        self.con.commit()

    def finish_run(self, run_id, status, fingerprint, record_count,
                   relation_count, ts, error=None):
        self.con.execute(
            "UPDATE identity_runs SET status=?,fingerprint=?,record_count=?,"
            "relation_count=?,error=?,ended_ts=? WHERE run_id=?",
            (status, fingerprint, record_count, relation_count, error, ts, run_id))
        self.con.commit()

    def all_runs(self):
        cols = ("run_id,mode,rule_version,status,fingerprint,record_count,"
                "relation_count,error,started_ts,ended_ts")
        return [dict(zip(cols.split(","), row)) for row in
                self.con.execute("SELECT %s FROM identity_runs ORDER BY started_ts"
                                 % cols)]

    # ------------------------------------------------------------------ #
    # Reviewer report
    # ------------------------------------------------------------------ #
    def review_report(self):
        """Everything a reviewer needs in one deterministic structure."""
        decisions = self.load_decisions()
        relations = self.all_relations()
        entities = self.all_entities()

        candidates, conflicts, confirmed, rejected = [], [], [], []
        wrong_merge_suspects, missed_merge_suspects = [], []
        for rel in relations:
            rel = dict(rel)
            rel["evidence_list"] = json.loads(rel.get("evidence") or "[]")
            rel["conflict_list"] = json.loads(rel.get("conflicts") or "[]")
            dec = decisions.get(rel["relation_id"])
            rel["manual"] = bool(dec)
            if dec:
                rel["reviewer"] = dec["reviewer"]
                rel["basis"] = dec["basis"]
                rel["decided_ts"] = dec["decided_ts"]
            state = dec["decision"] if dec else rel["state"]
            bucket = {
                "candidate": candidates,
                "conflict": conflicts,
                "confirmed": confirmed,
                "rejected": rejected,
            }.get(state)
            if bucket is not None:
                bucket.append(rel)
            # Separate traceability for the two failure modes.
            if state == STATE_REJECTED and "shared_short_prefix" in rel["evidence_list"]:
                wrong_merge_suspects.append(rel)
            if state in ("candidate", "conflict") and rel["kind"] in (
                    "same_as", "merged_into"):
                missed_merge_suspects.append(rel)

        return {
            "rule_version": RULE_VERSION,
            "entity_count": len(entities),
            "relation_count": len(relations),
            "manual_decision_count": len(decisions),
            "candidates": candidates,
            "conflicts": conflicts,
            "confirmed": confirmed,
            "rejected": rejected,
            "wrong_merge_suspects": wrong_merge_suspects,
            "missed_merge_suspects": missed_merge_suspects,
            "entities": entities,
        }

    def reset(self):
        for tbl in ("records", "meta", "parcel_entities", "parcel_members",
                    "parcel_relations", "identity_decisions", "identity_runs"):
            self.con.execute("DELETE FROM %s" % tbl)
        self.con.commit()
