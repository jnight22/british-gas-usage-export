"""Build a self-contained HTML dashboard from british_gas_usage.csv.

bg_export.py calls this automatically. To rebuild from an existing CSV without logging in:
    python dashboard.py                      # uses output/british_gas_usage.csv
    python dashboard.py path/to/usage.csv
"""

import csv
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

TEMPLATE = Path(__file__).parent / "dashboard_template.html"
PLACEHOLDER = "/*__PAYLOAD__*/null"


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_rows(csv_path: Path) -> list:
    rows = []
    with csv_path.open(newline="") as f:
        for r in csv.DictReader(f):
            if str(r.get("has_data")).lower() not in ("true", "1"):
                continue
            rows.append({
                "date": r["date"],
                "fuel": r["fuel"],
                "kwh": _num(r.get("standard_kwh")),
                "cost": _num(r.get("total_cost")),
            })
    return rows


def build(csv_path: Path, out_path: Path) -> Path:
    payload = {
        "rows": load_rows(csv_path),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": csv_path.name,
    }
    # Escape "</" so the data can never close the <script> tag early.
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, data)
    out_path.write_text(html, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "output" / "british_gas_usage.csv"
    out = build(src, src.with_name("dashboard.html"))
    print(f"Dashboard -> {out}")
    webbrowser.open(out.resolve().as_uri())
