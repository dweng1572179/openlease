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
import logging
import re

from . import cache
from .db import get_conn
from .models import ListingQuery

log = logging.getLogger("openlease")

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


def _text_of(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in
                    ("address", "neighborhood", "property_type", "our_description"))


def embed_listings(listing_ids: list[int]) -> int:
    """Backfill `listing_vec` for ids that don't have a vector yet; returns how many were
    embedded. No key -> registry.embedder() is None -> a no-op returning 0, and search
    stays BM25-only (the keyless invariant).

    Voyage IS a paid surface (its free tier covers this corpus 400x, but the budget
    guardrail still applies). A BudgetExceeded partway through stops the backfill —
    LOUDLY logged, never a crash — and returns however many embedded before the cap hit;
    whatever's already saved is kept, and the rest picks up on the next call."""
    from . import registry
    emb = registry.embedder()
    if not emb or not listing_ids:
        return 0
    from .db import load_vectors, save_vector
    have, _ = load_vectors(listing_ids)
    todo = [i for i in listing_ids if i not in set(have)]
    if not todo:
        return 0
    holes = ",".join("?" * len(todo))
    with get_conn() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM listing WHERE id IN ({holes})", todo).fetchall()}
    ids = [i for i in todo if i in rows]
    done = 0
    for chunk in (ids[i:i + 64] for i in range(0, len(ids), 64)):
        try:
            vecs = emb.embed([_text_of(rows[i]) for i in chunk], input_type="document")
        except cache.BudgetExceeded as e:
            log.warning(
                "Embedding backfill stopped after %d/%d listings — monthly paid-spend "
                "cap reached (%s); the rest stay BM25-only until the cap resets.",
                done, len(ids), e,
            )
            return done
        except Exception as e:  # noqa: BLE001 — a key is an UNLOCK, never a requirement
            # An expired key (401), a rate limit (429) or a Voyage outage must degrade to
            # BM25, not abort the backfill. Catching only BudgetExceeded let an
            # HTTPStatusError escape — the one paid surface in the app that could still
            # take the caller down with it.
            log.warning("Embedding backfill stopped after %d/%d listings — Voyage failed "
                        "(%s: %s); the rest stay BM25-only.", done, len(ids),
                        type(e).__name__, e)
            return done
        for i, v in zip(chunk, vecs):
            save_vector(i, v)
        done += len(chunk)
    return done


def cosine_ids(candidate_ids: list[int], query_text: str) -> list[int]:
    """Brute-force `M @ q` in numpy: 0.84ms over 5000x1024. No vector index, no extension,
    no failure mode on a stock python.

    No key, no candidates, no embedded vectors, an empty query, or a BudgetExceeded
    mid-search all degrade the SAME way — an empty list — so RRF fuses over BM25 alone,
    exactly the keyless path. A BudgetExceeded is the one case worth a LOUD log; the rest
    are unremarkable "there's nothing to rank semantically" cases."""
    from . import registry
    emb = registry.embedder()
    if not emb or not candidate_ids or not query_text.strip():
        return []
    import numpy as np

    from .db import load_vectors
    ids, M = load_vectors(candidate_ids)
    if not ids:
        return []
    try:
        q = np.asarray(emb.embed([query_text], input_type="query")[0], dtype=np.float32)
    except cache.BudgetExceeded as e:
        log.warning(
            "Semantic ranking skipped for this search — monthly paid-spend cap reached "
            "(%s); falling back to BM25 only.", e,
        )
        return []
    except Exception as e:  # noqa: BLE001 — a key is an UNLOCK, never a requirement
        # A bad/expired key or a Voyage hiccup must not 500 the user's search. Catching
        # only BudgetExceeded meant an HTTPStatusError propagated straight out through
        # rank_listings -> /api/search: setting a stale VOYAGE_API_KEY would have turned
        # the optional semantic layer into a hard requirement.
        log.warning("Semantic ranking skipped for this search — Voyage failed (%s: %s); "
                    "falling back to BM25 only.", type(e).__name__, e)
        return []
    n = float(np.linalg.norm(q))
    if n:
        q = q / n
    sims = M @ q                                   # both sides L2-normed -> cosine
    order = np.argsort(-sims)
    return [ids[i] for i in order]


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
    # Keyless: cosine_ids returns [] and RRF fuses ONE list, which is order-preserving —
    # so there is no `if voyage_key` branch anywhere in the ranker.
    query_text = " ".join([*q.keywords, q.neighborhood, *q.property_types]).strip()
    lists = [ids for ids in (bm25_ids(candidate_ids, q.keywords),
                             cosine_ids(candidate_ids, query_text)) if ids]
    fused = rrf(lists) if lists else []

    ordered = [i for i, _ in fused]
    seen = set(ordered)
    ordered += [i for i in candidate_ids if i not in seen]

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
