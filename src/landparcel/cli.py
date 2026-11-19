
"""Command-line entry point.

Backward-compatible forms (unchanged)::

    python -m landparcel.cli                 # full run, prints status JSON
    python -m landparcel.cli --incremental   # incremental run
    python -m landparcel.cli --full          # explicit full run

Reviewer subcommands (new)::

    python -m landparcel.cli report          # candidate/conflict/decision view
    python -m landparcel.cli legacy          # recompute with the old rule
    python -m landparcel.cli decide RELATION_ID confirmed|rejected \\
        --reviewer NAME --basis "依据..."
"""
import json, sys
from . import pipeline
from .store import Store
from . import review


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] == "report":
        con = Store().connect()
        try:
            _print(Store(con).review_report())
        finally:
            con.close()
        return

    if argv and argv[0] == "legacy":
        con = Store().connect()
        try:
            _print(review.recompute_legacy(Store(con)))
        finally:
            con.close()
        return

    if argv and argv[0] == "decide":
        if len(argv) < 3:
            raise SystemExit("usage: decide RELATION_ID confirmed|rejected "
                             "--reviewer NAME --basis TEXT")
        relation_id, decision = argv[1], argv[2]
        reviewer, basis = "reviewer", ""
        rest = argv[3:]
        while rest:
            flag = rest.pop(0)
            if flag == "--reviewer" and rest:
                reviewer = rest.pop(0)
            elif flag == "--basis" and rest:
                basis = rest.pop(0)
        con = Store().connect()
        try:
            _print(review.decide(Store(con), relation_id, decision,
                                 reviewer, basis))
        finally:
            con.close()
        return

    mode = "full"
    if "--incremental" in argv:
        mode = "incremental"
    if "--full" in argv:
        mode = "full"
    print(json.dumps(pipeline.run(mode=mode), ensure_ascii=False))

if __name__ == "__main__":
    main()
