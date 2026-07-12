"""The two keyless fast paths (wp-json, JSON-LD), and the two copyright invariants: we
never persist the page's prose, and we never download a photo."""
import json
import logging
import os
import pathlib

os.environ["ANTHROPIC_API_KEY"] = ""      # keyless: the LLM rung must be skipped, loudly

import pytest  # noqa: E402

from app import db, extract  # noqa: E402
from app.config import settings  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"
SRC = {"key": "test", "name": "Test Brokerage", "url": "https://example.com"}


def test_wp_json_fast_path():
    p = FIX / "ripco_wpjson.json"
    if not p.exists():
        pytest.skip("ripco_wpjson.json not captured — see Task 10 Step 6")
    items = json.loads(p.read_text())
    got = [extract.from_wp_json(i, SRC, "nyc") for i in items]
    got = [g for g in got if g]
    assert got, "the wp-json rung produced nothing — check the pick() key lists"
    d = got[0]
    assert d["source"] == "test" and d["metro"] == "nyc"
    assert d["source_url"].startswith("http")
    assert d["address"]
    assert d["transaction_type"] == "lease"
    assert d["our_description"]                 # our sentence, not the post's content


def test_jsonld_fast_path():
    html = (FIX / "jsonld_listing.html").read_text()
    d = extract.from_jsonld(html, "https://example.com/l/1", SRC, "chi")
    assert d is not None
    assert d["address"] == "1550 N Damen Ave, Wicker Park"
    assert d["size_sf"] == 2100 and d["asking_rent"] == 58.0
    assert d["rent_unit"] == "sf_yr"
    assert json.loads(d["photo_urls_json"]) == ["https://cdn.example.com/photo1.jpg"]


def test_broker_prose_is_never_persisted():
    html = (FIX / "jsonld_listing.html").read_text()
    d = extract.from_jsonld(html, "https://example.com/l/1", SRC, "chi")
    blob = json.dumps(d)
    assert "UNRIVALED" not in blob, "the page's marketing copy leaked into a stored field"
    assert "description" not in d          # only `our_description` exists
    assert "Wicker Park" in d["our_description"] and "2,100 SF" in d["our_description"]


def test_photos_are_referenced_never_downloaded():
    """`photo_urls_json` holds the BROKER'S url. Nothing in extract.py fetches image
    bytes — if this ever changes, it is the CoStar v. CREXi fact pattern verbatim."""
    src = pathlib.Path(extract.__file__).read_text()
    for red_flag in ("httpx.get(photo", "download_image", "s3", "boto3", ".write(img"):
        assert red_flag not in src, f"extract.py appears to fetch/store image bytes: {red_flag}"


def test_llm_rung_is_skipped_loudly_without_a_key(caplog):
    with caplog.at_level(logging.WARNING, logger="openlease"):
        out = extract.from_html_llm("# some page", "https://example.com/x", SRC, "nyc")
    assert out is None
    assert any("ANTHROPIC_API_KEY" in r.message for r in caplog.records), \
        "the LLM rung degraded SILENTLY — that is the failure mode this rule exists to stop"


def test_extract_schema_is_all_required_and_non_nullable():
    for name, f in extract.ListingExtract.model_fields.items():
        assert f.is_required(), f"{name} has a default -> optional param -> request HANGS"
        assert "NoneType" not in str(f.annotation), f"{name} is nullable -> union-param 400"


# --- Fix 2 (review pass): the HTML+LLM rung routes through cache.cached() + the budget
# cap. 10 of 16 sources.yml entries are `rung: html` -- before this fix, a crawl over them
# spent real money with ZERO enforcement of settings.monthly_budget_cents, and re-billed
# in full on every re-crawl of the same page. Mirrors ai.py's own test_ai.py pattern. ---

def _fake_listing_extract_response(calls):
    class _FakeParsed:
        def model_dump(self):
            return {
                "address": "123 Main St", "neighborhood": "", "property_type": "",
                "transaction_type": "", "size_sf": 0, "divisible_min_sf": 0,
                "divisible_max_sf": 0, "floor": "", "ceiling_height_ft": 0.0,
                "asking_rent": 0.0, "rent_unit": "", "lease_type": "", "sale_price": 0,
                "availability_date": "", "broker_name": "", "broker_firm": "",
                "broker_phone": "", "broker_email": "", "features": [],
                "our_description": "A space at 123 Main St.",
            }

    class _FakeResp:
        parsed_output = _FakeParsed()

    class _FakeMessages:
        def parse(self, **kwargs):
            calls.append(1)
            return _FakeResp()

    class _FakeClient:
        messages = _FakeMessages()

    return _FakeClient()


def test_html_llm_rung_hits_cache_and_never_rebills(monkeypatch, tmp_path):
    """Never pay twice: an identical repeated page must not re-invoke the paid client."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "extract_cache_hit.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 1000)
    calls = []
    monkeypatch.setattr(extract.ai, "_client", lambda: _fake_listing_extract_response(calls))

    d1 = extract.from_html_llm("# same page", "https://example.com/x", SRC, "nyc")
    d2 = extract.from_html_llm("# same page", "https://example.com/x", SRC, "nyc")

    assert len(calls) == 1, "an identical page must be a cache hit, not a re-fetch/re-bill"
    assert d1 is not None and d1["address"] == "123 Main St"
    assert d2 is not None and d2["address"] == "123 Main St"


def test_html_llm_budget_exceeded_falls_back_loudly_and_never_crashes(monkeypatch, tmp_path, caplog):
    """A paid call refused by the monthly budget must return None (the crawl just gets no
    listing from this one page, never a crash) and must log LOUDLY at WARNING naming the
    budget as the reason -- same as every other fallback in this app."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "extract_budget.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 0)   # nothing left this month

    def _must_not_be_called():
        raise AssertionError("the Anthropic client must not run when there is nothing left to spend")
    monkeypatch.setattr(extract.ai, "_client", _must_not_be_called)

    with caplog.at_level(logging.WARNING, logger="openlease"):
        d = extract.from_html_llm("# some page", "https://example.com/x", SRC, "nyc")

    assert d is None
    assert "budget" in caplog.text.lower()


def test_html_llm_still_all_required_no_optional_field_added():
    """Guards against the easiest way to reintroduce the project's hardest-won bug while
    wiring this through cache.cached(): ANY optional field on ListingExtract makes
    messages.parse() HANG (2^N grammar shapes). Wiring in caching must never add one."""
    for name, f in extract.ListingExtract.model_fields.items():
        assert f.is_required(), f"{name} gained a default while adding caching -> HANGS"
        assert "NoneType" not in str(f.annotation), f"{name} became nullable -> union-param 400"


def test_the_slug_is_a_full_address_and_the_title_is_not():
    """A national feed's post title is a bare street name with no city ("2732 East 15th
    Street"). Handing that to a metro-scoped geocoder gets a confident WRONG answer: it
    matched a same-named street in Brooklyn, so a Panama City, Florida property was filed
    under NYC and given a New York Walk Score. The WP slug carries the city AND state, so
    it is geocodable — and crawl._place then drops whatever falls outside the four metros."""
    assert extract._slug_address("2446-broadway-new-york-ny") == "2446 broadway new york ny"
    assert extract._slug_address("302-south-colonial-drive-cleburne-tx") == \
        "302 south colonial drive cleburne tx"
    # no state code, or no house number -> we do NOT guess
    assert extract._slug_address("tices-corner-marketplace-431b-chestnut-ridge-road") is None
    assert extract._slug_address("some-blog-post") is None
    assert extract._slug_address("") is None


def test_geo_hint_is_used_for_geocoding_and_never_stored():
    """The hint steers the geocoder; the DISPLAYED address stays the human one. And it must
    not leak into the listing row — save_listing only writes _LISTING_COLS."""
    from app.db import _LISTING_COLS
    assert "geo_hint" not in _LISTING_COLS

    item = {"title": {"rendered": "2732 East 15th Street | Panama City Commercial Parcel"},
            "slug": "2732-east-15th-street-panama-city-fl",
            "link": "https://www.ripcony.com/property-listings/2732-east-15th-street-panama-city-fl/"}
    src = {"key": "ripco", "name": "RIPCO", "url": "https://www.ripcony.com"}
    d = extract.from_wp_json(item, src, "nyc")
    assert d["geo_hint"] == "2732 east 15th street panama city fl"
    assert d["address"].startswith("2732 East 15th Street")   # display keeps the human form
