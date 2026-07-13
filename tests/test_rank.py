"""BM25 direction, RRF properties, and the keyless invariant (RRF over one list is a
passthrough — which is the whole reason the ranker has no `if voyage_key` branch)."""
import logging
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_rank.db")
os.environ["DB_PATH"] = _DB
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

from app import cache, db, rank, registry  # noqa: E402
from app.models import ListingQuery  # noqa: E402


def _setup():
    db.init_db()
    with db.get_conn() as c:
        c.execute("DELETE FROM listing")
    ids = {}
    for slug, desc in [
        ("a", "Corner retail storefront in Wynwood with heavy foot traffic"),
        ("b", "Warehouse industrial space with dock loading"),
        ("c", "Wynwood retail retail retail gallery storefront"),
    ]:
        ids[slug] = db.save_listing(dict(source="test", 
            metro="mia", source_url=f"t://{slug}", address=f"{slug} Test St",
            neighborhood="Wynwood" if slug != "b" else "Doral",
            property_type="retail" if slug != "b" else "industrial",
            size_sf=2000, asking_rent=80, rent_unit="sf_yr", our_description=desc,
        ))
    return ids


def test_bm25_is_ascending_best_first():
    ids = _setup()
    got = rank.bm25_ids(list(ids.values()), ["Wynwood", "retail"])
    # `c` mentions both terms more often -> best. `b` mentions neither -> absent.
    assert got and got[0] == ids["c"], got
    assert ids["b"] not in got, got


def test_match_expr_survives_punctuation():
    # raw prose in MATCH throws on an apostrophe; quoted tokens don't
    assert rank.match_expr(["Macy's", "co-working"]) == '"Macy" OR "s" OR "co" OR "working"'
    assert rank.match_expr([]) == "" and rank.match_expr(["!!!"]) == ""
    ids = _setup()
    assert rank.bm25_ids(list(ids.values()), ["Macy's"]) == []   # no throw, just no hits


def test_rrf_single_list_is_order_preserving():
    src = [7, 3, 9, 1]
    assert [i for i, _ in rank.rrf([src])] == src


def test_rrf_fuses_two_lists():
    # 9 sits at rank 2 in BOTH lists; 7 is rank 1 in one list but ABSENT from the
    # other (a list that never surfaced a candidate contributes nothing for it).
    # Agreement across lists beats a single list's top spot — a plain weighted sum
    # over one incomparable scale cannot express that.
    fused = [i for i, _ in rank.rrf([[7, 9, 3], [9, 3]])]
    assert fused[0] == 9 and set(fused) == {3, 7, 9}, fused
    scores = dict(rank.rrf([[7, 9, 3], [9, 3]]))
    assert scores[9] > scores[7]


def test_rrf_k_is_60_and_ranks_are_1_indexed():
    """The ordering assertions above hold for ANY k and either indexing base, so they
    cannot catch a regression in the two constants the spec calls load-bearing. Pin the
    arithmetic itself: rank-1 in a single list scores exactly 1/(60+1)."""
    assert rank.RRF_K == 60
    scores = dict(rank.rrf([[7, 3]]))
    assert abs(scores[7] - 1 / 61) < 1e-12, scores   # 1-indexed: 1/(60+1), not 1/(60+0)
    assert abs(scores[3] - 1 / 62) < 1e-12, scores


def test_rank_listings_returns_every_candidate():
    ids = _setup()
    q = ListingQuery(keywords=["Wynwood", "retail"], max_rent_per_sf_yr=100)
    out = rank.rank_listings(list(ids.values()), q)
    assert len(out) == 3, out                      # the unmatched `b` is still an answer
    assert out[0]["id"] == ids["c"]
    assert out[0]["semantic_score"] > out[-1]["semantic_score"]
    assert 0.0 <= out[-1]["semantic_score"] <= 1.0
    assert "SF" in out[0]["rationale"] and "under your" in out[0]["rationale"]


# --- Task 12: optional Voyage cosine, fused into the same RRF call ------------------------

def test_cosine_is_a_noop_without_a_key():
    """The keyless invariant: no key -> no cosine list -> RRF over one list -> the exact
    BM25 order. If this ever fails, the ranker grew a branch it doesn't need."""
    os.environ["VOYAGE_API_KEY"] = ""
    registry.reset()
    ids = _setup()
    assert rank.cosine_ids(list(ids.values()), "wynwood retail") == []
    assert rank.embed_listings(list(ids.values())) == 0

    q = ListingQuery(keywords=["Wynwood", "retail"])
    fused = [r["id"] for r in rank.rank_listings(list(ids.values()), q)]
    bm25 = rank.bm25_ids(list(ids.values()), q.keywords)
    assert fused[:len(bm25)] == bm25, (fused, bm25)


def test_vector_round_trip_is_l2_normalized():
    import numpy as np
    ids = _setup()
    db.save_vector(ids["a"], [3.0, 4.0] + [0.0] * 1022)   # norm 5 -> should store 0.6, 0.8
    got, M = db.load_vectors([ids["a"]])
    assert got == [ids["a"]]
    assert abs(float(np.linalg.norm(M[0])) - 1.0) < 1e-5
    assert abs(float(M[0][0]) - 0.6) < 1e-5


def _retaily(text: str) -> bool:
    return any(w in text.lower() for w in ("retail", "storefront", "espresso"))


class _FakeEmbedder:
    """Deterministic 2-dim stand-in for Voyage: retail-flavored text -> [1, 0], everything
    else -> [0, 1]. Exercises embed_listings/cosine_ids/rank_listings for real, with zero
    network calls (the suite must stay hermetic — no live Voyage call, key or not)."""

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        return [[1.0, 0.0] if _retaily(t) else [0.0, 1.0] for t in texts]


def test_cosine_surfaces_a_query_with_zero_keyword_overlap(monkeypatch):
    """The reason this task exists: a query sharing not a single word with any listing's
    stored text (unlike "Wynwood"/"retail" elsewhere in this file, none of these words
    appear in any _setup() address/neighborhood/type/description) gets NOTHING from BM25.
    A fake embedder that maps retail-flavored text and this query onto the same axis
    proves cosine_ids — and the RRF fusion inside rank_listings — surface the retail
    listings anyway, without a live Voyage call."""
    monkeypatch.setattr(registry, "embedder", lambda: _FakeEmbedder())
    ids = _setup()
    assert rank.embed_listings(list(ids.values())) == 3   # all 3 backfilled

    query_words = ["somewhere", "open", "espresso", "stand"]
    query = " ".join(query_words)
    assert rank.bm25_ids(list(ids.values()), query_words) == []   # BM25: nothing at all

    top = rank.cosine_ids(list(ids.values()), query)
    assert top, top
    assert top[0] in (ids["a"], ids["c"]), top       # the two retail listings, not `b`
    assert ids["b"] not in top[:2]

    q = ListingQuery(keywords=query_words)
    fused = rank.rank_listings(list(ids.values()), q)
    assert fused[0]["id"] in (ids["a"], ids["c"]), fused


class _BudgetBlownEmbedder:
    """Mirrors what voyage.py actually does: route through the REAL cache.cached(), so
    with the monthly cap monkeypatched to 0 it's the genuine cache.BudgetExceeded (never
    a hand-rolled stand-in) that propagates — same mechanism proven in test_ai.py. fetch()
    asserts it's never called: a paid provider must be blocked BEFORE spending, not after."""

    def embed(self, texts, input_type="document"):
        def fetch():
            raise AssertionError("must not be called — budget should block before fetch")
        return cache.cached("voyage", input_type, {"texts": texts}, fetch, cost_cents=1)


def test_embed_listings_falls_back_loudly_on_budget_exceeded(monkeypatch, caplog):
    """Voyage IS a paid surface. A refused-by-budget call must degrade the backfill
    (never crash it) and must log LOUDLY at WARNING naming the budget as the reason —
    same contract as every other paid provider (ai.py's nl_to_query/reply)."""
    from app.config import settings
    monkeypatch.setattr(settings, "monthly_budget_cents", 0)   # nothing left this month
    monkeypatch.setattr(registry, "embedder", lambda: _BudgetBlownEmbedder())
    ids = _setup()
    with caplog.at_level(logging.WARNING, logger="openlease"):
        assert rank.embed_listings(list(ids.values())) == 0
    assert "budget" in caplog.text.lower()


def test_cosine_falls_back_loudly_on_budget_exceeded(monkeypatch, caplog):
    """Same guarantee at search time: a candidate already has a stored vector (so the
    function doesn't short-circuit before ever calling the embedder), but embedding the
    QUERY text blows the budget. cosine_ids must return [] (RRF fuses BM25 alone) and log
    LOUDLY, not raise into the search request."""
    from app.config import settings
    ids = _setup()
    db.save_vector(ids["a"], [1.0, 0.0])
    monkeypatch.setattr(settings, "monthly_budget_cents", 0)
    monkeypatch.setattr(registry, "embedder", lambda: _BudgetBlownEmbedder())
    with caplog.at_level(logging.WARNING, logger="openlease"):
        assert rank.cosine_ids(list(ids.values()), "wynwood retail") == []
    assert "budget" in caplog.text.lower()


# --- a key is an UNLOCK, never a requirement -------------------------------------------

class _BrokenEmbedder:
    """A stale/revoked key, a 429, a Voyage outage — anything that is NOT BudgetExceeded."""

    def embed(self, texts, input_type="document"):
        import httpx
        raise httpx.HTTPStatusError("401 Unauthorized", request=None, response=None)


def test_a_bad_voyage_key_degrades_to_bm25_instead_of_500ing_the_search(monkeypatch, caplog):
    """rank.py caught ONLY BudgetExceeded, so an HTTPStatusError from a stale key
    propagated out through rank_listings -> /api/search as a 500. That turns the optional
    semantic layer into a hard requirement — the exact inverse of the keyless promise.
    It must degrade to BM25 and say so LOUDLY."""
    ids = _setup()
    db.save_vector(ids["a"], [1.0, 0.0])          # a vector exists, so we reach the embedder
    monkeypatch.setattr(registry, "embedder", lambda: _BrokenEmbedder())

    with caplog.at_level(logging.WARNING, logger="openlease"):
        assert rank.cosine_ids(list(ids.values()), "wynwood retail") == []
    assert "voyage failed" in caplog.text.lower()

    # ...and the SEARCH still works, fused over BM25 alone
    q = ListingQuery(keywords=["Wynwood", "retail"])
    out = rank.rank_listings(list(ids.values()), q)
    assert out and out[0]["id"] == ids["c"]


def test_embed_listings_survives_a_broken_key_too(monkeypatch, caplog):
    ids = _setup()
    monkeypatch.setattr(registry, "embedder", lambda: _BrokenEmbedder())
    with caplog.at_level(logging.WARNING, logger="openlease"):
        assert rank.embed_listings(list(ids.values())) == 0
    assert "voyage failed" in caplog.text.lower()


def test_something_in_the_app_actually_calls_embed_listings():
    """The whole Voyage feature was UNREACHABLE: nothing outside tests ever called
    embed_listings, so listing_vec was always empty, cosine_ids short-circuited on every
    search, and setting VOYAGE_API_KEY changed nothing — while the Settings dashboard
    reported semantic ranking as "on". A feature that is advertised and does not run is
    worse than one that is absent."""
    import inspect

    from app import crawl
    assert hasattr(crawl, "embed_pending")
    assert "embed_listings" in inspect.getsource(crawl.embed_pending)
    # ...and the enrichment pass invokes it, so an ingest actually populates the vectors
    assert "embed_pending" in inspect.getsource(crawl.enrich_pending)
