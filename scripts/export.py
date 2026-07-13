"""Export the corpus for a collaborator.

    .venv/bin/python -m scripts.export             # listings only  (small)
    .venv/bin/python -m scripts.export --with-scores   # + POIs and the Overpass cache

The database on disk is ~144 MB, and almost none of that is the part anyone wants. 45,186
LISTINGS are the corpus. 416,290 POI rows and the cached Overpass responses are the bulk —
and both are DERIVED: a collaborator regenerates them with `POST /api/enrich`. So the
default export carries the listings and leaves the derivable behind.

`--with-scores` bundles the POIs and the Overpass cache too. It is much bigger, and the only
reason to want it is that it makes their Walk Scores appear INSTANTLY instead of after hours
of politely-rate-limited Overpass calls. That is a real kindness; it is just not the default.

Three formats, because a collaborator is one of two people:
  openlease-corpus.db   — they want to RUN the app. Drop it in, it works.
  listings.csv          — they want to open it in a spreadsheet.
  listings.jsonl        — they want to pipe it into something.
"""
import argparse
import csv
import gzip
import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from app.config import settings
from app.db import get_conn

OUT = Path("export")

# Never leave with these. `saved`, `portfolio` and `search_turn` are the OWNER'S workspace —
# their shortlists, their notes, the questions they typed. The corpus is public facts about
# buildings; those tables are a person's working session, and they are not part of the data.
PRIVATE = ("saved", "portfolio", "portfolio_listing", "search_session", "search_turn",
           "setting", "highlight")

DERIVED = ("poi", "transit_nearby", "provider_cache", "listing_vec", "crawl_log")


def _sizes(p: Path) -> str:
    n = p.stat().st_size
    return f"{n/1e6:.1f} MB" if n > 1e6 else f"{n/1e3:.0f} KB"


def export(with_scores: bool = False) -> None:
    OUT.mkdir(exist_ok=True)

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM listing ORDER BY id")]
    if not rows:
        raise SystemExit("no listings to export — run POST /api/crawl first")
    cols = list(rows[0])

    # --- 1. CSV: for a human with a spreadsheet -------------------------------
    csv_p = OUT / "openlease-listings.csv"
    with csv_p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # --- 2. JSONL: for a machine ---------------------------------------------
    jsonl_p = OUT / "openlease-listings.jsonl"
    with jsonl_p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")

    # --- 3. a runnable SQLite: for someone who wants the APP ------------------
    db_p = OUT / "openlease-corpus.db"
    if db_p.exists():
        db_p.unlink()
    # copy the live DB (a plain file copy of a WAL database can be torn — use the backup API)
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(db_p)
    src.backup(dst)
    src.close()

    drop = list(PRIVATE) + ([] if with_scores else list(DERIVED))
    for t in drop:
        try:
            dst.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass          # table doesn't exist in this schema version — fine
    dst.commit()
    dst.execute("VACUUM")          # actually reclaim the space, or the file stays 144 MB
    dst.close()

    # --- gzip everything: a 45k-row CSV compresses about 5x --------------------
    made = []
    for p in (csv_p, jsonl_p, db_p):
        gz = p.with_suffix(p.suffix + ".gz")
        with p.open("rb") as fi, gzip.open(gz, "wb", compresslevel=9) as fo:
            shutil.copyfileobj(fi, fo)
        p.unlink()
        made.append(gz)

    print(f"  {len(rows):,} listings exported to {OUT}/\n")
    for p in made:
        print(f"    {p.name:<34} {_sizes(p)}")
    print(f"\n  scores/POIs included: {'yes' if with_scores else 'NO (they run POST /api/enrich)'}")
    print("  owner's saves, portfolios and chat history: EXCLUDED\n")
    print("  give the collaborator:")
    print("    gunzip openlease-corpus.db.gz && mv openlease-corpus.db ~/openlease/openlease.db")
    print("    ./run.sh          # -> http://localhost:8788")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-scores", action="store_true",
                    help="bundle POIs + the Overpass cache so their Walk Scores work instantly")
    export(**vars(ap.parse_args()))
