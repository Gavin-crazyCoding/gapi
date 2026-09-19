"""One-shot / refreshable exporter: server catalog → gapi/seed/tiers.json.

READ-ONLY against the FreeLLM API database (opened in immutable mode). It does
not import or modify anything under server/. Re-run after the upstream catalog
changes.

For every model_id it records:
  tier      : Frontier | Large | Medium | Small | null (non-standard labels → null)
  platforms : distinct upstream platform rows (visibility whitelist input)

Usage:
  python scripts/export_tiers.py [--db ../server/data/freeapi.db] [--out seed/tiers.json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TIER_RANK = {"Small": 0, "Medium": 1, "Large": 2, "Frontier": 3}
STANDARD_TIERS = set(TIER_RANK)


def export(db_path: Path) -> dict:
    uri = f"file:{db_path.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    models: dict[str, dict] = {}
    for model_id, platform, label in con.execute(
        "SELECT model_id, platform, size_label FROM models"
    ):
        entry = models.setdefault(model_id, {"tier": None, "platforms": []})
        if platform not in entry["platforms"]:
            entry["platforms"].append(platform)
        if label in STANDARD_TIERS:
            current = entry["tier"]
            if current is None or TIER_RANK[label] > TIER_RANK[current]:
                entry["tier"] = label  # highest tier wins if families disagree
    con.close()
    for entry in models.values():
        entry["platforms"].sort()
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": str(db_path),
        "tiers": TIER_RANK,
        "models": dict(sorted(models.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="../server/data/freeapi.db")
    parser.add_argument("--out", default="seed/tiers.json")
    args = parser.parse_args()

    data = export(Path(args.db))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tiered = sum(1 for m in data["models"].values() if m["tier"])
    print(f"Exported {len(data['models'])} models ({tiered} with standard tier) → {out}")


if __name__ == "__main__":
    main()
