"""Hybrid ranking over the listings the hard filter already kept.

Three things here are load-bearing, all learned from the spec's research:

1. FTS5's `bm25()` is NEGATIVE (more negative = better), so it's `ORDER BY ... ASC`.
   Sorting DESC silently ranks the WORST matches first and everything still "works".
2. Raw prose in a `MATCH` throws on a stray apostrophe ("Macy's"). Tokenize, then quote
   each term.
3. Fuse with RRF (k=60), never a weighted sum: BM25 is unbounded and negative, cosine is
   [-1,1] — the scales are incomparable. RRF over ONE list is order-preserving, so the
   keyless path needs zero branching: it's the same call with one list in.
"""
import re

from .db import get_conn
from .models import ListingQuery

RRF_K = 60
_WORD = re.compile(r"[a-z0-9]+", re.I)


def match_expr(keywords: list[str]) -> str:
    """Keywords -> a safe FTS5 MATCH string. Each term is tokenized and double-quoted,
    so apostrophes/hyphens/punctuation can't blow up the query parser."""
    terms = []
    for kw in keywords:
        for tok in _WORD.findall(kw or ""):
            terms.append(f'"{tok}"')
    return " OR ".join(dict.fromkeys(terms))  # dedup, preserve order


def bm25_ids(candidate_ids: list[int], keywords: list[str]) -> list[int]:
    """Candidates ranked by BM25 relevance, best first. Candidates that match nothing
    are simply absent (rank_listings appends them after the ranked ones)."""
    expr = match_expr(keywords)
    if not expr or not candidate_ids:
        return []
    holes = ",".join("?" * len(candidate_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT rowid FROM listing_fts "
            f"WHERE listing_fts MATCH ? AND rowid IN ({holes}) "
            f"ORDER BY bm25(listing_fts) ASC",     # NEGATIVE score: ASC = best first
            [expr, *candidate_ids],
        ).fetchall()
    return [r["rowid"] for r in rows]


def rrf(lists: list[list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion (Cormack/Clarke/Büttcher, SIGIR'09): score = Σ 1/(k+rank).
    With a single list the output order equals the input order — that is why the
    keyless (BM25-only) path is not a special case anywhere else in the app."""
    scores: dict[int, float] = {}
    for lst in lists:
        for i, id_ in enumerate(lst, start=1):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + i)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def _rationale(row: dict, q: ListingQuery) -> str:
    """One line: why this matched. Deterministic and keyless — the LLM writes the
    conversational reply, but every result explains itself even with no key."""
    bits = []
    if row.get("size_sf"):
        bits.append(f"{row['size_sf']:,} SF")
    if row.get("property_type"):
        bits.append(row["property_type"])
    if row.get("neighborhood"):
        bits.append(f"in {row['neighborhood']}")
    if row.get("asking_rent") and row.get("rent_unit") == "sf_yr":
        rent = f"${row['asking_rent']:,.0f}/SF/yr"
        if q.max_rent_per_sf_yr:
            rent += f" (under your ${q.max_rent_per_sf_yr:,.0f} cap)"
        bits.append("at " + rent)
    elif row.get("sale_price"):
        bits.append(f"asking ${row['sale_price']:,}")
    return " ".join(bits) or row.get("address", "")


def rank_listings(candidate_ids: list[int], q: ListingQuery) -> list[dict]:
    """Rank the survivors of the hard filter. Returns EVERY candidate — ranked ones
    first, then the rest in id order (a listing the filter kept is a valid answer even
    if it matched no keyword). Each carries SpaceFinder's three per-listing fields."""
    if not candidate_ids:
        return []
    lists = [ids for ids in (bm25_ids(candidate_ids, q.keywords),) if ids]
    # Task 12 appends the cosine list here; RRF's signature does not change.
    fused = rrf(lists) if lists else []

    ordered = [i for i, _ in fused]
    ordered += [i for i in candidate_ids if i not in set(ordered)]

    holes = ",".join("?" * len(candidate_ids))
    with get_conn() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM listing WHERE id IN ({holes})", candidate_ids
        ).fetchall()}

    n = len(ordered)
    out = []
    for i, id_ in enumerate(ordered):
        row = rows[id_]
        # semanticScore: 0..1 off the FUSED RANK (not the raw BM25 — that scale is
        # unbounded and negative, and means nothing to a client).
        sem = round(1.0 - (i / n), 3) if n > 1 else 1.0
        out.append({
            "id": id_,
            "semantic_score": sem,
            # ponytail: keyless, rank IS the whole signal, so score is sem*100. When a
            # second signal exists that isn't already inside the fusion, blend it here.
            "score": round(sem * 100, 1),
            "rationale": _rationale(row, q),
        })
    return out


if __name__ == "__main__":  # python -m app.rank
    from .seed import seed

    n = seed()
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM listing").fetchall()]
    q = ListingQuery(keywords=["Wynwood", "retail"], max_rent_per_sf_yr=100)
    out = rank_listings(ids, q)
    assert len(out) == n, (len(out), n)
    assert out[0]["rationale"], out[0]
    print(f"rank ok — {n} listings, top: {out[0]}")
