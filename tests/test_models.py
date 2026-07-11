"""Models + the camelCase API boundary. The two copyright divergences (our_description
serialized AS `description`; photo_urls AS `photos`) are contract, so they are asserted."""
import json
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_models.db")
os.environ["DB_PATH"] = _DB
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

from app import db, seed  # noqa: E402
from app.models import METRO_KEYS, METROS, ListingQuery, to_api  # noqa: E402


def test_metros_loaded():
    assert METRO_KEYS == ("nyc", "mia", "la", "chi"), METRO_KEYS
    assert METROS["la"]["owner_available"] is False        # CA statute — not a bug
    assert METROS["chi"]["zoning_available"] == "partial"  # city-only
    assert len(METROS["nyc"]["bbox"]) == 4


def test_query_serializes_camel_case():
    q = ListingQuery(property_types=["retail"], min_size_sf=1000, max_rent_per_sf_yr=64.0)
    wire = q.model_dump(by_alias=True)
    assert wire["propertyTypes"] == ["retail"]
    assert wire["minSizeSf"] == 1000
    assert wire["maxRentPerSfYr"] == 64.0
    assert wire["transactionType"] == "lease"
    assert "excludeZip3" in wire and "excludeAddrStates" in wire
    # and it round-trips from the wire names (priorState comes back camelCase)
    assert ListingQuery(**{"propertyTypes": ["office"], "maxSizeSf": 5000}).max_size_sf == 5000


def test_upsert_and_api_shape():
    db.init_db()
    n = seed.seed()
    assert n == 12, n
    n_again = seed.seed()          # re-seed must UPDATE, not duplicate
    assert n_again == 12, n_again

    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM listing WHERE source_url = 'seed://mia/1'").fetchone()
    rid = row["id"]

    # a thinner re-crawl must not null out a good value (COALESCE guard)
    db.save_listing({"source_url": "seed://mia/1", "metro": "mia", "address": row["address"]})
    assert db.get_listing(rid)["size_sf"] == 1500

    api = to_api(db.get_listing(rid))
    assert api["sizeSf"] == 1500 and api["propertyType"] == "retail"
    assert api["transactionType"] == "lease" and api["sourceUrl"] == "seed://mia/1"
    # the two divergences: our prose is served under SpaceFinder's key; no `ourDescription`
    assert "Wynwood" in api["description"] and "ourDescription" not in api
    assert api["photos"] == []          # JSON columns decode, empty -> []
    assert "photoUrlsJson" not in api
