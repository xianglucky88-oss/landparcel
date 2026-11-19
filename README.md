# landparcel

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/landparcel`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

## Parcel identity resolution

Name similarity alone cannot identify a parcel (宗地) after a split or merge,
while cosmetic identifier changes (spaces, hyphens, re-stamped suffixes) must
not create meaningless new parcels.  The identity engine in
`landparcel/identity.py` replaces the old six-character name-prefix clustering
with evidence-based, fully deterministic resolution.

### How a record is identified

* **Identifier normalization** (`normalize_parcel_id`): spaces, hyphens,
  underscores and slashes are flattened and the text is upper-cased, so
  `BD-001`, `BD 001` and `BD_001` share one canonical id.  Genuine split
  suffixes (`LP-200-A`, `LP 200 B`, `LP-200(2)`) keep the parent id as their
  `base_id`; bare numeric components (`NB-300` vs `NB-301`) never merge.
* **Independent evidence signals**: canonical/base id, split lineage, exact
  and near (character-trigram) name match, and district.  Every emitted
  relation stores its evidence codes, conflicts, confidence and rationale.
* **Anti short-prefix rule**: two different ids whose names merely share a
  stem before diverging (e.g. `…改造项目一期` / `…改造项目二期`) are recorded
  with `shared_short_prefix` evidence and the `rejected` state, so distinct
  parcels can never be swallowed by a common prefix.
* **Review states**: `candidate` (awaits a human), `conflict` (signals
  disagree), `confirmed`, `rejected`.  Split children are always separate
  entities linked by `split_from` / `split_sibling` relations rather than
  collapsed into one parcel.

### Reviewer workflow

```text
python -m landparcel.cli report
    # candidates, conflicts, confirmed and rejected relations, plus two
    # separately traceable failure buckets:
    #   wrong_merge_suspects  -- 归并错误 (prefix-rule style false merges)
    #   missed_merge_suspects -- 归并遗漏 (still-unresolved candidate links)

python -m landparcel.cli decide RELATION_ID confirmed|rejected \
    --reviewer NAME --basis "人工结论依据"
    # stores the conclusion with reviewer and basis; it always wins on replay

python -m landparcel.cli legacy
    # recomputes with the old name-prefix-6 rule and reconciles every legacy
    # cluster into erroneous_merges / pending_legacy_merges / kept_merges
```

Manual decisions are stored in `identity_decisions` and take precedence over
rule output on every subsequent run.  Each run is recorded in
`identity_runs` with a rule version and graph fingerprint.

### Drift guarantees

The graph is rebuilt over the full record set (stored history plus the new
batch) as a pure, order-independent function.  The test suite proves the
fingerprint is unchanged by:

* recomputation with the old rule and historical replay;
* pagination/input reordering and repeated duplicate content;
* incremental backfill, which only extends the graph;
* a local write failure, which rolls the graph transaction back and leaves a
  `failed` run row with the error instead of partial data.

### Storage

The original `records` and `meta` tables and their methods are unchanged, so
existing databases and offline entry points keep working.  Identity data
lives in additive tables: `parcel_entities`, `parcel_members`,
`parcel_relations`, `identity_decisions` and `identity_runs`.  Record primary
keys are computed without derived annotations, so re-annotation never creates
duplicate rows.
