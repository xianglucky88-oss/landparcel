
from types import SimpleNamespace

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "landparcel.db"

SPEC = SimpleNamespace(
    slug="landparcel",
    fmt="html",
    engine="rpa",
    endpoint="items",
    columns=["id", "name", "value", "status", "ts", "district"],
    page_size=4,
    int_fields=["value"],
    ts_field="ts",
)
