"""Regenerate gold/meetings_ai_public.csv from the committed silver tables.

Gold derives entirely from silver, so this avoids the full PDF rebuild:

    python pipeline/export_gold.py --data-dir data
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from eaglegis.gold import AI_PUBLIC_FIELDS, build_ai_public_rows
from eaglegis.writer import write_csv

SILVER_TABLES = {
    "boards": "core/boards.csv",
    "meeting_formats": "core/meeting_formats.csv",
    "meeting_types": "core/meeting_types.csv",
    "meetings": "core/meetings.csv",
    "agenda_categories": "core/agenda_categories.csv",
    "agenda_items": "core/agenda_items.csv",
    "agenda_item_projects": "core/agenda_item_projects.csv",
    "projects": "core/projects.csv",
    "motions": "core/motions.csv",
    "locations_v2": "v2/locations_v2.csv",
}


def load_tables(silver_dir: Path) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {}
    for name, rel_path in SILVER_TABLES.items():
        path = silver_dir / rel_path
        if not path.exists():
            raise SystemExit(f"Missing silver table: {path}")
        with path.open(encoding="utf-8") as handle:
            tables[name] = list(csv.DictReader(handle))
    return tables


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    rows = build_ai_public_rows(load_tables(data_dir / "silver"))
    out_path = data_dir / "gold" / "meetings_ai_public.csv"
    write_csv(out_path, rows, AI_PUBLIC_FIELDS)
    ai_ready = sum(1 for row in rows if row["AiReady"] == "true")
    print(f"Wrote {len(rows)} rows ({ai_ready} AI-ready) to {out_path}")


if __name__ == "__main__":
    main()
