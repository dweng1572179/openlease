"""BM25 direction, RRF properties, and the keyless invariant (RRF over one list is a
passthrough — which is the whole reason the ranker has no `if voyage_key` branch)."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_rank.db")
os.environ["DB_PATH"] = _DB
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

from app import db, rank  # noqa: E402
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
        ids[slug] = db.save_listing(dict(
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
