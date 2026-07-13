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


# --- rung 3c: facts out of the page text, keyless ---------------------------------------

_SRC = {"key": "westmac", "name": "WESTMAC", "url": "https://www.westmac.com"}

_PAGE = """<html><head><title>540 Rose Avenue | WESTMAC Commercial</title></head><body>
<nav>Home Listings About</nav>
<h1>540 Rose Avenue</h1>
<div class="detail">
  <p>Prime Venice retail opportunity! An incredible flagship space.</p>
  <ul><li>Size: 1,400 SF</li><li>Rent: $5.50/SF/mo NNN</li><li>Type: Retail</li></ul>
</div>
<footer>(c) WESTMAC</footer></body></html>"""


def test_facts_rung_pulls_size_rent_and_type_off_a_real_page():
    """The rung that makes the product usable with no key. Most broker sites publish no
    feed and no real-estate JSON-LD — their listings are prose on an HTML page — so without
    this the only way to get a size or an ask was the paid LLM rung, and a keyless crawl
    produced a link directory: addresses with no SF and no rent, which the hard filter
    ("~1,500 SF under $8k/mo") cannot filter on at all."""
    d = extract.from_html_facts(_PAGE, "https://www.westmac.com/listings/540-rose/", _SRC, "la")
    assert d["address"] == "540 Rose Avenue"
    assert d["size_sf"] == 1400
    assert d["asking_rent"] == 5.5
    assert d["rent_unit"] == "sf_mo"          # LA quotes per MONTH — 12x matters
    assert d["property_type"] == "retail"
    assert d["source_url"].endswith("/540-rose/")


def test_the_facts_rung_never_stores_the_brokers_prose():
    """The page's own copy ("Prime Venice retail opportunity! An incredible flagship
    space.") must never reach the database. our_description is OURS, from the facts."""
    d = extract.from_html_facts(_PAGE, "https://www.westmac.com/listings/540-rose/", _SRC, "la")
    assert "flagship" not in d["our_description"].lower()
    assert "incredible" not in d["our_description"].lower()
    assert "opportunity" not in d["our_description"].lower()
    assert "1,400 SF" in d["our_description"] and "540 Rose Avenue" in d["our_description"]


def test_a_monthly_quote_is_not_rendered_as_a_yearly_one():
    """describe() hardcoded "/SF/yr" and rounded to whole dollars, so a real LA listing at
    $5.50/SF/mo read as "$6/SF/yr" — off by 12x, cents gone, and entirely plausible."""
    d = extract.from_html_facts(_PAGE, "https://www.westmac.com/listings/540-rose/", _SRC, "la")
    assert "$5.50/SF/mo" in d["our_description"], d["our_description"]
    assert "/SF/yr" not in d["our_description"]


def test_an_address_with_no_facts_is_not_a_listing():
    """The hard filter runs on SF and rent. A row with neither is invisible to every query
    that matters, so it is not worth storing — we would just be a link directory."""
    bare = "<html><h1>123 Nowhere Street</h1><p>Call for details.</p></html>"
    assert extract.from_html_facts(bare, "https://x.test/listings/123", _SRC, "la") is None


def test_absurd_numbers_are_rejected_as_parse_artifacts():
    """A phone number, a zip, a year — a page is full of digits. A size must carry a UNIT
    and be within human bounds. (`d is None or d["size_sf"] is None` asserted nothing: the
    first branch is true whenever extraction gives up for ANY reason.)"""
    junk = ("<html><h1>1 Main Street</h1><p>Call 310-555-1212. Est. 1987. "
            "Suite 4 SF. 99,999,999 SF campus. Rent: $5/SF/yr.</p></html>")
    d = extract.from_html_facts(junk, "https://x.test/listings/1", _SRC, "nyc")
    assert d is not None, "the rent is real, so we DO get a listing"
    assert d["asking_rent"] == 5.0
    assert d.get("size_sf") is None, "4 SF and 99,999,999 SF are both out of human bounds"


def test_the_city_comes_from_the_page_so_the_address_can_be_geocoded():
    """Broker pages write "540 Rose Avenue Venice, CA 90291" — no comma before the city —
    so a naive capture runs back through the street name. We already KNOW the street, so
    subtract its words. This is what turns a bare street name into a geocodable address,
    and it is what reveals a listing that is not in this market at all: Rexford is a
    SoCal-wide REIT, and crawled under `la` it hands us buildings in Oxnard and Carlsbad."""
    assert extract._city_of("540 Rose Avenue Venice, CA 90291", "540 Rose Avenue") == ("Venice", "CA")
    assert extract._city_of("1442 2nd Street Santa Monica, CA", "1442 2nd Street") == ("Santa Monica", "CA")
    assert extract._city_of("Del Norte Boulevard Oxnard, CA", "701 Del Norte Boulevard") == ("Oxnard", "CA")
    # CRE boilerplate sits exactly where a city would
    assert extract._city_of("540 Rose Avenue NNN Venice, CA", "540 Rose Avenue") == ("Venice", "CA")
    assert extract._city_of("no city named anywhere", "1 Main St") is None


# --- a broker page is full of numbers that are NOT this listing's ------------------------

def test_a_market_statistic_never_becomes_an_asking_rent():
    """THE fabrication bug. Metro Manhattan's listing pages quote the Midtown market in
    their own copy — "asking rents held flat at $78.23/SF (Cushman & Wakefield, April
    2026)" — and taking min() of everything that looked like a rent stamped EVERY Metro
    Manhattan listing "$78/SF/yr". That is not an ask. It is a market average, presented to
    a broker as this suite's rent, and it is exactly the kind of plausible-looking wrong
    data this project refuses to ship. A wrong rent is worse than no rent: a search that
    filters on a fabricated number is worse than one that filters on nothing."""
    page = ("Midtown asking rents held flat at $78.23/SF (Cushman & Wakefield, April 2026), "
            "with Class A at $85.28/SF and Class B at $77.55/SF. "
            "3,305 SF available on the partial 29th floor. 3,305 SF. 3,305 SF.")
    assert extract._rent_of(page, "nyc") is None, "four market figures — we must not pick one"
    assert extract._size_of(page) == 3305, "the size the page actually repeats"


def test_a_labelled_rent_beats_a_market_number_on_the_same_page():
    page = "Asking Rent: $62/SF/yr. Midtown averaged $78.23/SF last quarter."
    assert extract._rent_of(page, "nyc") == (62.0, "sf_yr")


def test_one_unlabelled_rent_is_unambiguous_and_is_taken():
    """A page that quotes exactly one $/SF figure is telling you the ask."""
    assert extract._rent_of("1,500 SF of ground-floor retail. $95/SF/yr.", "nyc") == (95.0, "sf_yr")


def test_a_size_filter_dropdown_never_becomes_a_size():
    """"Filter by size: 1,000 SF / 1,999 SF / 4,999 SF" is a form control. Each option
    appears exactly once; a listing repeats its own size."""
    assert extract._size_of("Filter by size: 1,000 SF 1,999 SF 4,999 SF 9,999 SF") is None


def test_the_property_type_is_the_one_the_page_talks_about():
    """Taking the first hit in _TYPES order typed every Metro Manhattan listing "retail" —
    their pages say "office" twenty times and "retail" twice, and "retail" happens to come
    first in the tuple."""
    page = "Office space. Prime office tower. Office suite. Office. Nearby retail."
    d = extract.from_html_facts(
        "<html><h1>122 East 42nd Street</h1><p>Size: 825 SF. " + page + "</p></html>",
        "https://x.test/listings/122-east-42nd", _SRC, "nyc")
    assert d["property_type"] == "office"


def test_every_decoy_a_real_broker_page_puts_next_to_the_rent():
    """These are the exact shapes on the pages we crawl. Each decoy sits right where a rent
    would, and each one was, at some point, stored as a listing's asking rent."""
    def R(t, metro="nyc"):
        return extract._rent_of(t, metro)
    # metro-manhattan: the real ask, and the three things around it
    assert R("Size: 3,305 SF Rent/SF: $ 60 Monthly Rent: $ 16,525") == (60.0, "sf_yr")
    assert R("Max Rent/Month Select all $5,000 $10,000 $15,000") is None       # a filter
    assert R("asking rents held flat at $78.23/SF (Cushman & Wakefield)") is None  # the market
    # westmac: the real ask, and the two things around it
    assert R("540 Rose Avenue For Lease - $10.00/SF/Mo. NNN", "la") == (10.0, "sf_mo")
    assert R("Triple net charges +/-$1.41/SF/Mo.", "la") is None               # the NNN charge
    assert R("Related Listings For Lease 1702 Lincoln Boulevard $4.50/SF/Mo.", "la") is None
    # ripco: no rent at all, and it says so
    assert R("Asking Rent Upon Request Asking Price $6,500,000") is None


def test_a_period_label_does_not_reach_across_into_the_next_one():
    """"Rent/SF: $60  Monthly Rent: $16,525" — reading "Monthly" as $60's period made it
    $60/SF/MO instead of $60/SF/YR. A 12x error, and entirely plausible."""
    assert extract._rent_of("Rent/SF: $ 60 Monthly Rent: $ 16,525", "nyc") == (60.0, "sf_yr")
    # (3,305 SF x $60/SF/yr = $16,525/month — the page is internally consistent.)


def test_an_asking_price_is_a_sale_not_a_rent():
    """RIPCO's 57 West 38th St says "Asking Rent Upon Request" AND "Asking Price
    $6,500,000". Correctly refusing the rent left us with nothing, when the page was
    telling us plainly what it was."""
    assert extract._sale_of("Asking Rent Upon Request Asking Price $6,500,000") == 6_500_000
    d = extract.from_html_facts(
        "<html><h1>57 West 38th Street</h1><p>9,470 SF. Asking Rent Upon Request. "
        "Asking Price $6,500,000.</p></html>",
        "https://www.ripcony.com/property-listings/57-west-38th-street/", _SRC, "nyc")
    assert d["transaction_type"] == "sale"
    assert d["sale_price"] == 6_500_000
    assert d.get("asking_rent") is None


# --- the review's Critical findings, each reproduced then closed ------------------------

def test_the_buildings_footprint_never_becomes_the_suites_size():
    """C1. "1250 Broadway is a 807,000 SF tower. This 3,305 SF suite is available." — both
    numbers appear once, so "most repeated" had no winner and the code took document order:
    the TOWER. It went into the database as the size of the suite.

    This used to REFUSE (return None), because with two one-off candidates we could not tell
    which was which. We can now: the sentence calls 807,000 a TOWER, and _building_sf reads
    that, so the tower is excluded and the suite is the only candidate left. Refusing was
    the right answer when we were blind; it is the wrong answer now that we can see, because
    it threw away a perfectly good listing. What must never happen — the tower being stored
    as the suite — is what is actually asserted here."""
    assert extract._size_of(
        "1250 Broadway is a 807,000 SF tower. This 3,305 SF suite is available.") == 3305
    # ...and when the listing does state its own size twice, we take it
    assert extract._size_of(
        "807,000 SF tower. This 3,305 SF suite. 3,305 SF available.") == 3305
    # ...but where the building is NOT identifiable, two one-off candidates are still a
    # coin-flip, and we still refuse rather than guess.
    assert extract._size_of("3,305 SF. 807,000 SF.") is None


def test_an_unqualified_rate_is_never_silently_called_yearly():
    """C2. The yearly and monthly bands OVERLAP at $5-$90/SF. LA and industrial quote per
    MONTH. Defaulting a bare "$5.75/SF" to YEARLY turned a $69/SF/yr West LA office into a
    $5.75/SF/yr bargain — a 12x error that surfaces in every "cheap space" search, and that
    reads as a find rather than a fault."""
    assert extract._rent_of("Rate: $5.75/SF NNN. Creative office.", "la") == (5.75, "sf_mo")
    assert extract._rent_of("Rate: $5.75/SF NNN.", "nyc") == (5.75, "sf_yr")
    assert extract._rent_of("Rate: $5.75/SF NNN.", "") is None      # unknown market: refuse
    assert extract._rent_of("Rate: $2.10/SF", "", "industrial") == (2.10, "sf_mo")


def test_a_size_dropdown_labelled_Size_is_still_a_dropdown():
    """I1. The only defence was a hardcoded `(?<!filter by )`. Any site labelling its filter
    "Size" walked straight back into the bug."""
    assert extract._size_of("Size 1,000 SF 1,999 SF 4,999 SF 9,999 SF") is None
    assert extract._size_of("Size: 3,305 SF") == 3305               # a real label still works


def test_a_date_is_not_a_size():
    """I2. The size label's UNIT had been made optional, so "Availability: 2026" became a
    2,026 SF listing."""
    assert extract._size_of("Availability: 2026") is None


def test_the_sites_own_nav_does_not_retype_a_lease_as_a_sale():
    """I3. Every one of these sites has "Properties For Sale" in its header menu, and
    `page_text` strips <nav>/<footer> but not <header>. A lease listing retyped `sale` is
    invisible to every lease search."""
    html = ('<html><header><ul><li>Properties For Sale</li></ul></header>'
            '<h1>1225 Lincoln Rd</h1><p>For Lease. Size: 2,400 SF. Rent: $95/SF/yr.</p></html>')
    d = extract.from_html_facts(html, "https://x.test/listings/1225", _SRC, "mia")
    assert d["transaction_type"] == "lease"


def test_the_brokers_headline_never_becomes_the_address():
    """I5. `_headline` split on pipes and en-dashes but not a plain hyphen, so
    "<title>280 Broadway - Ground Floor Retail!! - Prime Corner</title>" was stored in a FACT
    field — and persisted the broker's own marketing copy, which we never store."""
    assert extract._headline(
        "<title>540 Rose Avenue - WESTMAC Commercial Brokerage</title>") == "540 Rose Avenue"
    assert extract._headline(
        "<title>280 Broadway - Ground Floor Retail!! - Prime Corner</title>") == "280 Broadway"


def test_a_multi_tenant_building_has_no_single_size_and_the_page_says_so():
    """Rexford's pages are BUILDINGS, not suites, and they label every number:

        "Property Total SF: 125,514"        <- the complex
        "Available Unit(s) SF 5,961-9,358"  <- what you can actually lease

    "Most repeated" picked 125,514 — the building — as the size of a 9,358 SF unit, because
    the total is the number a building page repeats. Reading any single figure as "the size"
    is wrong in a different way each time. So: the total is the BUILDING, the range is what
    is leasable, and `size_sf` is the largest contiguous unit — which is what a tenant with a
    size in mind is actually shopping for."""
    txt = ("Multi-tenant industrial complex totaling 125,514 SF with 21 tenant spaces. "
           "Property Details Property Total SF: 125,514 Number of Buildings 1 "
           "Available Unit(s) SF 5,961-9,358")
    assert extract._building_sf(txt) == 125514
    assert extract._available_range(txt) == (5961, 9358)
    assert extract._size_of(txt) is None, "the building total must not be the size"

    d = extract.from_html_facts(
        "<html><h1>701 Del Norte Boulevard</h1><p>" + txt + " Oxnard, CA</p></html>",
        "https://www.rexfordindustrial.com/properties/701-del-norte-boulevard/", _SRC, "la")
    assert d["size_sf"] == 9358                      # the largest unit you can lease
    assert (d["divisible_min_sf"], d["divisible_max_sf"]) == (5961, 9358)
    assert d["total_building_sf"] == 125514          # ...and the building, kept separately


# --- the publisher's own office is not a listing ------------------------------
#
# Every one of these regressions shipped a real row into a real database. They are
# written with the data that actually fooled us, not with a synthetic stand-in.

def test_jsonld_ignores_the_brokerage_organization_node():
    """Metro 1's Squarespace footer emits an Organization node carrying the FIRM'S OWN
    Wynwood head office. The old rung took an address from any node that had one, so it
    filed 120 NE 27th St as Miami inventory five times — three of them on blog posts."""
    html = """<script type="application/ld+json">
    {"@type":"Organization","name":"Metro 1","address":{"streetAddress":"120 Northeast 27th Street, Suite 2","addressLocality":"Miami"}}
    </script>"""
    assert extract.from_jsonld(html, "https://metro1.com/articles/x", SRC, "mia") is None


def test_jsonld_still_reads_a_real_property_node():
    """The allowlist must not throw the baby out: a RealEstateListing still parses."""
    html = """<script type="application/ld+json">
    {"@type":"RealEstateListing","address":{"streetAddress":"2618 NW 2nd Ave","addressLocality":"Miami"},
     "floorSize":{"value":"1500"}}
    </script>"""
    d = extract.from_jsonld(html, "https://x.com/properties/2618", SRC, "mia")
    assert d and d["size_sf"] == 1500
    assert d["address"].startswith("2618 NW 2nd Ave")


def test_jsonld_reads_a_yoast_graph():
    """Yoast wraps every node in @graph — a listing site using it must still parse."""
    html = """<script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"WebSite","name":"Broker"},
      {"@type":"Organization","address":{"streetAddress":"1 Broker Plaza"}},
      {"@type":"Product","address":{"streetAddress":"350 Fifth Ave"},"floorSize":{"value":"2200"}}]}
    </script>"""
    d = extract.from_jsonld(html, "https://x.com/properties/350", SRC, "nyc")
    assert d["size_sf"] == 2200
    assert d["address"].startswith("350 Fifth Ave")   # not the broker's plaza


# --- space that is already gone -----------------------------------------------

def test_leased_listings_are_not_ingested():
    """Terranova keeps closed deals on the site as trophies. Five of its nine Miami rows
    were titled '... - Leased'. A search engine that answers with space somebody else
    already rented is a scrapbook."""
    for title, slug in [("105 Miracle Mile &#8211; Leased", "105-miracle-mile-leased"),
                        ("300 Miracle Mile", "300-miracle-mile-leased"),
                        ("221 Ocean Dr - SOLD", "221-ocean-dr")]:
        item = {"title": {"rendered": title}, "slug": slug,
                "link": f"https://terranovacorp.com/property/{slug}",
                "acf": {"address": "105 Miracle Mile", "size": "2242"}}
        assert extract.from_wp_json(item, SRC, "mia") is None, f"ingested a dead deal: {title}"


def test_available_listing_survives_the_off_market_filter():
    item = {"title": {"rendered": "308 Miracle Mile"}, "slug": "308-miracle-mile",
            "link": "https://terranovacorp.com/property/308-miracle-mile/",
            "acf": {"address": "308 Miracle Mile", "size": "2,242"}}
    d = extract.from_wp_json(item, SRC, "mia")
    assert d and d["size_sf"] == 2242


def test_html_entities_never_reach_a_fact_field():
    """A feed hands us its title escaped. An address carrying '&#8211;' is not an address:
    it does not geocode, and it renders as mojibake on the map pin."""
    item = {"title": {"rendered": "255 Alhambra Circle &#8211; Suite 400"},
            "slug": "255-alhambra-circle", "link": "https://terranovacorp.com/property/255",
            "acf": {"address": "255 Alhambra Circle &#8211; Suite 400"}}
    d = extract.from_wp_json(item, SRC, "mia")
    assert d and "&#" not in d["address"]
    assert "–" in d["address"] or "-" in d["address"]


def test_a_tower_is_not_a_suite():
    """Blanca's pages are Class A office TOWERS whose suite-level availability lives
    off-site, so the biggest number on the page is the whole building. Nothing labels it
    'Total SF' — it is said in prose — so the labelled pattern sailed past it and we stored
    1450 Brickell as a 625,800 SF suite. A 625,800 SF 'space for lease' is not a listing,
    it is a skyscraper."""
    txt = ("1450 Brickell is a 35-story, 625,800 RSF Class A office tower located at the "
           "entrance to Brickell Avenue. 625,800 SF total. Currently available: 17,881 SF "
           "of contiguous space on floors 20-21. 17,881 SF available now.")
    d = extract.from_html_facts(txt, "https://blancacre.com/properties/1450-brickell",
                                SRC, "mia")
    assert d, "the facts rung gave up on a page that plainly states a size"
    assert d.get("total_building_sf") == 625800, "the tower is the BUILDING"
    assert d.get("size_sf") != 625800, "stored a 35-story tower as the leasable suite"
    assert d.get("size_sf") == 17881, f"the available suite is 17,881 SF, got {d.get('size_sf')}"


def test_building_prose_variants_are_all_read_as_the_building():
    for txt, want in [
        ("a 300,000 SF office building at 1 Main St", 300000),
        ("2601 S Bayshore, a 311,755 square-foot tower", 311755),
        ("the 80,414 SF corporate centre", 80414),
        ("52,333 RSF Class A property", 52333),
    ]:
        assert extract._building_sf(txt) == want, f"missed the building in: {txt!r}"


def test_a_label_can_lie_about_what_it_labels():
    """Blanca's spec sheet reads "Building Size: 625,800 SF ... Typical Floor Size: 17,881
    SF" and sends you to a third party for the actual availabilities. EVERY square footage
    on that page is a building spec. _SIZE_LABEL matches the word "Size" INSIDE "Building
    Size", so the labelled branch handed back 625,800 — the very number _building_sf had
    just excluded as the tower. Excluding only the tower then left the FLOORPLATE as the
    last candidate standing, and a floorplate is not an availability either."""
    txt = ("1450 Brickell. Year Built: 2010 Building Height: 35 Stories "
           "Building Size: 625,800 SF Building Class: A Typical Floor Size: 17,881 SF")
    assert extract._building_sf(txt) == 625800
    assert extract._size_of(txt) is None, \
        "a page that states no leasable size must not produce one"


def test_lot_size_is_not_leasable_size():
    assert extract._size_of("Lot Size: 12,000 SF") is None
    assert extract._size_of("Land Size: 40,000 SF") is None
    # ...but a real availability still reads
    assert extract._size_of("Space Available: 2,400 SF. 2,400 SF of retail.") == 2400


def test_a_suite_cannot_be_bigger_than_its_building():
    """1355 Alton came out as a 7,000 SF space inside a 3,500 SF building. When that
    happens we have mixed up two figures and don't know which — so the inferred one goes
    and the labelled one stays."""
    txt = ("1355 Alton Road. Building Size: 3,500 SF. 7,000 SF 7,000 SF of frontage "
           "across the block.")
    d = extract.from_html_facts(txt, "https://blancacre.com/properties/1355-alton",
                                SRC, "mia")
    assert d, "the listing should survive on its building size"
    assert d.get("total_building_sf") == 3500
    assert d.get("size_sf") is None, "kept a suite larger than the building it sits in"


def test_a_tower_with_no_stated_availability_is_still_a_listing():
    """Requiring a SUITE size dropped 1450 Brickell on the floor entirely. It is a real
    625,800 SF Class A tower in Brickell that a tenant searching "office in Brickell"
    should see — with a link to the broker for what's actually free inside it."""
    txt = "1450 Brickell Avenue. Building Size: 625,800 SF Building Class: A"
    d = extract.from_html_facts(txt, "https://blancacre.com/properties/1450-brickell",
                                SRC, "mia")
    assert d and d["total_building_sf"] == 625800
    assert d.get("size_sf") is None
    assert d["our_description"]


def test_a_building_that_lists_its_units_one_by_one():
    """RIPCO publishes the leasable spaces under a header instead of as a range:
      "Total Square Feet ±9,113 SF ... Proposed Divisions Retail A: 1,608 SF
       Retail B: 2,450 SF Retail C: 2,286 SF Retail D: 2,769 SF"
    Those four ARE what a tenant is on the page for. But no figure repeats and there is more
    than one, so _size_of correctly refused to guess — and the listing went in with NO size
    at all, invisible to every size filter, across ~294 RIPCO listings. The refusal was
    right; the answer was sitting under a header saying what these numbers are."""
    txt = ("Key Details Available Spaces 4 Total Square Feet ±9,113 SF Asking Rent Upon "
           "request Proposed Divisions Retail A: 1,608 SF Retail B: 2,450 SF "
           "Retail C: 2,286 SF Retail D: 2,769 SF All logical divisions considered")
    d = extract.from_html_facts(
        txt, "https://www.ripcony.com/property-listings/1150-ne-125th-st-north-miami-fl/",
        SRC, "mia")
    assert d, "a page listing four leasable suites produced no listing"
    assert d["total_building_sf"] == 9113, "the ± total is the BUILDING"
    assert d["size_sf"] == 2769, "size_sf is the largest unit a tenant could take"
    assert d["divisible_min_sf"] == 1608 and d["divisible_max_sf"] == 2769


def test_divisions_needs_at_least_two_units_and_its_own_header():
    # one figure under the header is not a division list — fall back to the usual rules
    assert extract._divisions("Proposed Divisions Retail A: 1,608 SF") is None
    # and figures with no header are not divisions at all
    assert extract._divisions("1,608 SF 2,450 SF 2,286 SF") is None


def test_the_neighbourhood_is_not_the_suite():
    """362 Van Brunt Street is a Red Hook storefront whose page sells the AREA around it:
    "a rebuilt port, 28 acres of public open space, and more than 275,000 SF of new
    development". The only other figure on the page is a LOT size — so once that was
    correctly decoyed out, the district's development pipeline was the sole surviving
    candidate, and a 275,000 SF SUITE went into the database."""
    txt = ("362 Van Brunt Street. Key Details Gross Lot Sq. Ft. 1,050 SF. The waterfront is "
           "being reborn with a rebuilt port, 28 acres of public open space, and more than "
           "275,000 SF of new development.")
    assert extract._size_of(txt) is None, "stored a whole district's pipeline as one suite"


def test_the_area_guard_does_not_eat_real_listings():
    """The markers are narrow ON PURPOSE. A real listing says "in the Design District" all
    the time, and decoying on that would throw away good listings to catch a rare bad one."""
    assert extract._size_of(
        "A 2,400 SF ground-floor space in the Design District. 2,400 SF available.") == 2400
    assert extract._size_of(
        "Prime waterfront retail. Space Available: 3,000 SF") == 3000


def test_a_building_is_never_one_of_its_own_units():
    """90 Broad Street lists "Space A Ground Floor 700 SF, Space B Ground Floor 650 SF" and
    then, still inside the divisions window, the 420,000 SF tower they sit in. The largest
    "unit" came out as the whole building, and a 700 SF storefront was filed as 420,000."""
    txt = ("90 Broad Street. Key Details Available Spaces 2 Asking Rent Upon Request "
           "Available Spaces Space A Ground Floor 700 SF Frontage 25 FT Space B Ground "
           "Floor 650 SF Frontage 27 FT Building Size: 420,000 SF")
    assert extract._divisions(txt) == [650, 700]
    d = extract.from_html_facts(
        txt, "https://www.ripcony.com/property-listings/90-broad-street-new-york-ny/",
        SRC, "nyc")
    assert d["size_sf"] == 700 and d["total_building_sf"] == 420000


def test_a_sale_price_per_sf_is_not_a_rent_per_sf():
    """WestMac writes "1025 Westwood For Sale – $14,995,000 ($810/SF)". That $810/SF is what
    the BUILDING costs to BUY, divided by its area. Read as an asking rent it became
    $810/SF/yr — about ten times Rodeo Drive, on a Westwood office block. A $15,000,000
    building was listed as a rental at a price nobody has ever paid."""
    txt = "1018 – 1025 Westwood For Sale – $14,995,000 ($810/SF) 12,000 SF"
    assert extract._rent_of(txt, "la", "office") is None, "a sale price became an asking rent"
    d = extract.from_html_facts(txt, "https://www.westmac.com/listings/1018-1025-westwood/",
                                SRC, "la")
    assert d and d["transaction_type"] == "sale"
    assert d["sale_price"] == 14995000
    assert not d.get("asking_rent")


def test_a_real_westmac_lease_still_reads():
    """The decoy must not eat the ask on a page that IS a lease."""
    txt = "540 Rose Avenue For Lease - $10.00/SF/Mo. NNN. 2,400 SF available."
    assert extract._rent_of(txt, "la", "retail") == (10.0, "sf_mo")


def test_another_suites_size_never_becomes_this_suites_size():
    """A suite page carries a "Suites Available" module advertising the OTHER suites in its
    building — precisely the hazard _size_of's docstring defends against, and precisely what
    _DIVISIONS_HDR matches. Consulting _divisions FIRST meant this page stored 20,600 SF:
    another tenant's space, in this listing's size field, and repeated back in the
    description we write. The branch that exists to stop this reintroduced it."""
    txt = ("90 Broad Street, Suite 401. Size: 3,305 SF. This 3,305 SF office suite is "
           "available immediately. Suites Available Suite 900: 12,000 SF "
           "Suite 1100: 20,600 SF")
    assert extract._size_of(txt) == 3305
    d = extract.from_html_facts(txt, "https://x.com/listings/90-broad-suite-401", SRC, "nyc")
    assert d["size_sf"] == 3305, f"stored another suite's size: {d['size_sf']}"
    assert "3,305 SF" in d["our_description"]
    assert not d.get("divisible_max_sf"), "the other suites are not this one's divisibility"


def test_a_building_with_no_size_of_its_own_still_uses_its_divisions():
    """...and the fix costs nothing on the pages _divisions exists for: RIPCO's building
    pages state no size of their own, so the divisions branch still runs for them."""
    txt = ("1150 NE 125th St. Key Details Available Spaces 4 Total Square Feet ±9,113 SF "
           "Proposed Divisions Retail A: 1,608 SF Retail B: 2,450 SF Retail C: 2,286 SF "
           "Retail D: 2,769 SF")
    assert extract._size_of(txt) is None       # the page states no size of its own
    d = extract.from_html_facts(txt, "https://www.ripcony.com/property-listings/1150-ne/",
                                SRC, "mia")
    assert d["size_sf"] == 2769 and d["divisible_min_sf"] == 1608


def test_a_street_name_never_eats_the_size():
    """_SIZE_DECOY had no word boundaries, so "land" matched inside HighLAND / PortLAND /
    OakLAND / CleveLAND, "lot" inside PiLOT and CharLOTte, "site" inside webSITE. A listing
    on any such street silently lost its size whenever the street name fell in the 22-char
    run-up. A guard that eats good data on a street-name coincidence is worse than the decoy
    it defends against."""
    for txt in ["4000 Highland Avenue 2,400 SF 2,400 SF of retail.",
                "1200 Portland Street 5,000 SF 5,000 SF available.",
                "77 Charlotte Road 1,800 SF 1,800 SF ground floor."]:
        assert extract._size_of(txt) is not None, f"a street name ate the size: {txt!r}"
    # ...and the real decoys still fire
    assert extract._size_of("Lot Size: 12,000 SF") is None
    assert extract._size_of("Building Size: 300,000 SF") is None


def test_a_dual_marketed_building_keeps_its_lease_rate():
    """"For Sale or Lease" is a real building offered both ways, and the $/SF is a real ASK.
    The "for sale" rent-decoy (added to stop a sale PRICE per SF becoming a rent) would
    otherwise delete the rent from every dual-marketed listing — one silent error traded for
    another."""
    assert extract._rent_of("123 Main St For Sale or Lease – $45.00/SF/yr. 5,000 SF",
                            "nyc", "office") == (45.0, "sf_yr")
    # ...but a pure sale page still refuses (the price-per-SF is not a rent)
    assert extract._rent_of("1025 Westwood For Sale – $14,995,000 ($810/SF)",
                            "la", "office") is None


def test_a_feed_price_is_not_assumed_to_be_yearly():
    """Both feed rungs stamped rent_unit="sf_yr" on whatever price the feed carried. A feed's
    price is not self-describing: a $2.25/SF/mo LA industrial ask called "sf_yr" is the same
    12x error in a different costume, and it reads as a bargain."""
    la = extract.from_wp_json(
        {"title": {"rendered": "1200 E Slauson Ave"}, "slug": "1200-e-slauson-ave-los-angeles-ca",
         "link": "https://x.com/p/1200", "acf": {"address": "1200 E Slauson Ave",
                                                 "property_type": "industrial", "rent": "2.25"}},
        SRC, "la")
    assert la["rent_unit"] == "sf_mo", "an LA industrial ask is quoted MONTHLY"
    # a figure that fits no band at all leaves NO rent rather than a confident wrong one
    weird = extract.from_wp_json(
        {"title": {"rendered": "9 Test Ave"}, "slug": "9-test-ave-new-york-ny",
         "link": "https://x.com/p/9", "acf": {"address": "9 Test Ave", "rent": "6500000"}},
        SRC, "nyc")
    assert not weird.get("asking_rent"), "a sale-sized number became an asking rent"


# --- second adversarial audit: 10 more findings ------------------------------

def test_a_sale_price_per_sf_labelled_as_such_is_not_a_rent():
    """WestMac's spec sheet puts "For Sale" in the headline and the figure a hundred
    characters later, so the 60-char decoy run-up never reached it. The LABEL beside the
    figure is the tell, and it is unambiguous."""
    txt = ("1025 Westwood Boulevard. For Sale. Building Size 18,500 SF | Lot Size 12,000 SF "
           "| Year Built 1962 | Zoning C2-1VL | Parking 24 spaces | Price $14,995,000 | "
           "Price Per Square Foot $810.54 / SF")
    assert extract._rent_of(txt, "la", "office") is None, "a sale price/SF became a rent"


def test_a_leased_page_whose_slug_is_clean_still_gets_caught():
    """_headline splits "105 Miracle Mile – Leased" on the dash and hands back a clean
    address, so by the time _clean runs the word "Leased" is gone — and a page whose URL
    carries no marker sailed straight in. The page says it in its own <title>."""
    html = ("<title>105 Miracle Mile &#8211; Leased | Terranova</title>"
            "<h1>105 Miracle Mile &#8211; Leased</h1>"
            "<p>Size: 2,400 SF. Asking Rent: $85/SF/YR.</p>")
    assert extract.from_html_facts(
        html, "https://terranovacorp.com/property/105-miracle-mile/", SRC, "mia") is None


def test_a_whole_building_offered_for_lease_keeps_its_size():
    """"For Lease: 25,000 SF industrial building" had its 25,000 SF read as THE BUILDING and
    excluded — leaving size_sf empty and the listing invisible to every SF filter. A building
    that is itself for lease IS the space."""
    txt = ("1234 Warehouse Way. For Lease: 25,000 SF industrial building in Vernon. "
           "Asking Rent: $1.50/SF/Mo.")
    d = extract.from_html_facts(txt, "https://x.com/listings/1234-warehouse-way", SRC, "la")
    assert d and d["size_sf"] == 25000, f"the leasable building lost its size: {d}"


def test_a_listing_that_states_its_acreage_keeps_its_building_size():
    """A real industrial listing says "situated on 2.5 acres, this property offers 5,000 SF
    of warehouse space" — decoying on acreage alone deleted its size and then the listing."""
    txt = ("1234 Warehouse Way. Situated on 2.5 acres, this industrial property offers "
           "5,000 SF of warehouse space available for lease in Vernon, CA. 5,000 SF.")
    d = extract.from_html_facts(txt, "https://x.com/listings/1234-warehouse", SRC, "la")
    assert d and d["size_sf"] == 5000


def test_the_neighbourhood_pipeline_is_still_refused():
    """...but the guard that mattered still fires: what marks 362 Van Brunt's sentence as
    being about the DISTRICT is public open space, not the acreage."""
    txt = ("362 Van Brunt Street. Key Details Gross Lot Sq. Ft. 1,050 SF. Red Hook is being "
           "remade: a rebuilt port, 28 acres of public open space, and more than 275,000 SF "
           "of new commercial development is planned.")
    assert extract._size_of(txt) is None
    assert extract._building_sf(txt) is None, "a district's pipeline became the building"


def test_a_single_tenant_listing_may_call_its_own_size_total():
    """"Total", "min" and "max" were decoys — and they reject the ordinary labels a
    single-tenant listing uses for its OWN size."""
    assert extract._size_of("Total Size: 2,400 SF of ground floor retail.") == 2400
    assert extract._size_of("Maximum contiguous: 12,000 SF available.") == 12000


def test_miami_industrial_is_quoted_per_year_not_per_month():
    """The monthly convention is LA's, not industrial's. A Miami-Dade warehouse asking
    "$16.50/SF" means a YEAR; calling it sf_mo overstates the ask 12x."""
    assert extract._unit_for(16.5, "", "mia", "industrial") == "sf_yr"
    assert extract._unit_for(16.5, "", "nyc", "industrial") == "sf_yr"
    assert extract._unit_for(1.95, "", "la", "industrial") == "sf_mo"   # LA still monthly


def test_describe_says_the_building_size_and_the_divisible_range():
    """Facts we store and never say are dead weight."""
    d = extract.describe({"address": "1450 Brickell", "total_building_sf": 625800,
                          "property_type": "office"})
    assert "625,800 SF" in d
    d = extract.describe({"address": "1150 NE 125th", "size_sf": 2769, "property_type": "retail",
                          "divisible_min_sf": 1608, "divisible_max_sf": 2769})
    assert "divisible 1,608-2,769 SF" in d


# --- third audit: the FIXES reintroduced the bugs they replaced -----------------
#
# Both of these were live at HEAD after the previous fix batch. The lesson is not that the
# guards were wrong — it is that a guard which NULLS the value other guards depend on is not a
# narrowing, it is a disarming.

def test_the_divisions_header_does_not_disarm_the_building_guard():
    """_LEASE_CTX matched the bare word "available" — the first word of "Available Spaces",
    which is the DIVISIONS MODULE'S OWN HEADER. So _building_sf skipped the only building
    match and returned None for the whole page. `building` is the single value BOTH _size_of
    and _divisions exclude by, so nulling it did not merely decline to call 420,000 the
    building: it disabled the guard everywhere, and the tower became one of its own units
    again — a 700 SF storefront filed as a 420,000 SF one, for the second time."""
    txt = ("90 Broad Street. Available Spaces in this 420,000 SF office tower: "
           "Space A Ground Floor 700 SF. Space B Ground Floor 650 SF. "
           "Space C Lower Level 1,200 SF. Asking $45.00/SF.")
    assert extract._building_sf(txt) == 420000, "the divisions header disarmed the guard"
    d = extract.from_html_facts(txt, "https://x.com/listings/90-broad-street", SRC, "nyc")
    assert d["size_sf"] == 1200, f"published a 35-story tower as the suite: {d['size_sf']}"
    assert d["total_building_sf"] == 420000


def test_a_size_filter_dropdown_cannot_outrank_the_suites_own_size():
    """Dropping "min"/"max" from the decoys reopened the size-filter form. "Min Size 1,000 SF"
    is BLESSED by _SIZE_LABEL (the word "Size" sits inside it), and a filter's two <select>s
    share one option list — so the value REPEATS, passes the "a label must repeat" guard, and
    beats the suite's own thrice-stated size.

    The subtle half: a _SIZE_LABEL match STARTS at the word "Size", so the decoy's backward
    window ended at "...Min " with "size" cut off on the far side of the boundary. The
    raw-figure path starts at the DIGIT and saw the whole phrase — which is why the same
    dropdown was caught there and waved through here, on the path that WINS."""
    txt = ("500 W 7th Street. Min Size 1,000 SF 2,500 SF 10,000 SF Max Size 1,000 SF "
           "2,500 SF 10,000 SF. Suite 400 - 3,305 SF of creative office. The 3,305 SF suite "
           "has views. Divisible: 3,305 SF. Asking $3.25/SF/mo.")
    d = extract.from_html_facts(txt, "https://x.com/listings/500-w-7th-street", SRC, "la")
    assert d["size_sf"] == 3305, f"a filter dropdown became the suite's size: {d['size_sf']}"


def test_the_narrowed_lease_context_still_frees_a_whole_building_lease():
    """...and the case _LEASE_CTX exists for still works: a building offered FOR LEASE is the
    space, not "the building, never the suite"."""
    d = extract.from_html_facts(
        "1234 Warehouse Way. For Lease: 25,000 SF industrial building in Vernon. "
        "Asking Rent: $1.50/SF/Mo.",
        "https://x.com/listings/1234-warehouse-way", SRC, "la")
    assert d["size_sf"] == 25000


def test_a_parcel_is_not_leasable_space():
    """"Site Size: 43,560 SF" is one acre of DIRT. Dropping "site" from the decoys outright
    published the parcel as leasable space; keeping it outright would have eaten "Total Size".
    Like min/max, it is a decoy only when what FOLLOWS says it measures the ground."""
    assert extract._size_of(
        "2732 East 15th Street. Site Size: 43,560 SF. Industrial land for lease.") is None
    assert extract._size_of("Total Size: 2,400 SF of retail.") == 2400   # still reads


def test_the_divisible_range_is_not_overwritten_by_the_rent():
    """Both are tails, and the rent used to OVERWRITE the range — so the fact we added it for
    only ever appeared on listings with no ask, which is almost never the multi-tenant
    buildings it was written for."""
    s = extract.describe({"address": "90 Broad St", "property_type": "retail", "size_sf": 2769,
                          "divisible_min_sf": 1608, "divisible_max_sf": 2769,
                          "asking_rent": 60.0, "rent_unit": "sf_yr"})
    assert "divisible 1,608-2,769 SF" in s and "$60/SF/yr" in s


def test_a_feed_price_that_states_its_own_unit_is_believed():
    """An isdigit() gate threw away every FORMATTED price — including "$2.25/SF/Mo", which
    states its own unit and is the one case we never have to guess about."""
    assert extract._feed_rent("$2.25/SF/Mo", "la", "industrial") == \
        {"asking_rent": 2.25, "rent_unit": "sf_mo"}
    assert extract._feed_rent("58", "chi", "office") == {"asking_rent": 58.0, "rent_unit": "sf_yr"}
    # ...and a sale-sized number still yields no rent at all
    assert extract._feed_rent("6500000", "nyc", "office") == {}


# --- rung 3d: __NEXT_DATA__ / Apollo state ------------------------------------

_NEXT = """<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"__APOLLO_STATE__":{
 "Building:1":{"__typename":"Building","id":"1","address":"230 Park Avenue",
               "slug":"/ny/new-york/230-park-avenue",
               "listings":[{"__ref":"Listing:10"},{"__ref":"Listing:11"},{"__ref":"Listing:12"},
                           {"__ref":"Listing:13"}]},
 "Building:2":{"__typename":"Building","id":"2","address":"999 Nearby Plaza",
               "slug":"/ny/new-york/999-nearby-plaza","listings":[{"__ref":"Listing:99"}]},
 "Listing:10":{"__typename":"Listing","id":"10","status":"AVAILABLE","squareFeet":10862,
               "floorAndSuite":"19th Floor","displayPSF":"~$65","estimatedPsf":65,
               "accuratePsf":0,"details":{"Property Type":"Office"}},
 "Listing:11":{"__typename":"Listing","id":"11","status":"AVAILABLE","squareFeet":5438,
               "floorAndSuite":"4th Floor","displayPSF":"~$65","estimatedPsf":65},
 "Listing:12":{"__typename":"Listing","id":"12","status":"UNAVAILABLE","squareFeet":22087,
               "floorAndSuite":"31st Floor"},
 "Listing:13":{"__typename":"Listing","id":"13","status":"AVAILABLE","squareFeet":0,
               "floorAndSuite":"Executive Suites Space"},
 "Listing:99":{"__typename":"Listing","id":"99","status":"AVAILABLE","squareFeet":7777}
}}}}</script>"""

_URL = "https://www.squarefoot.com/building/ny/new-york/230-park-avenue"


def test_next_data_emits_one_row_per_suite():
    """A building page carries every SUITE in the building, and a suite is what a tenant
    rents. Emit one row per building and 86 of 87 suites vanish on upsert — the BBL bug
    again."""
    rows = extract.from_next_data(_NEXT, _URL, SRC, "nyc")
    assert len(rows) == 2, f"expected 2 available, sized suites; got {len(rows)}"
    assert {r["size_sf"] for r in rows} == {10862, 5438}
    assert len({r["source_url"] for r in rows}) == 2, "suites collapsed onto one source_url"


def test_next_data_refuses_an_estimated_rent():
    """Every suite in the building shows the SAME "~$65", with accuratePsf=0 and
    estimatedPsf=65. That is SquareFoot's MODEL ESTIMATE — tilde and all — not a landlord's
    ask. Storing it is this project's very first bug reproduced at scale: a market average
    stamped onto every listing."""
    for r in extract.from_next_data(_NEXT, _URL, SRC, "nyc"):
        assert not r.get("asking_rent"), "stored an aggregator's estimate as an asking rent"
        assert "65" not in (r.get("our_description") or "")


def test_next_data_skips_unavailable_and_sizeless_suites():
    rows = extract.from_next_data(_NEXT, _URL, SRC, "nyc")
    assert 22087 not in {r["size_sf"] for r in rows}, "imported an UNAVAILABLE suite"
    assert all(r["size_sf"] > 0 for r in rows), "imported a suite with no stated size"


def test_next_data_takes_only_this_pages_building():
    """The page also carries every NEARBY building it links to. Take them and the whole
    neighbourhood gets filed under one address."""
    rows = extract.from_next_data(_NEXT, _URL, SRC, "nyc")
    assert all(r["address"] == "230 Park Avenue" for r in rows)
    assert 7777 not in {r["size_sf"] for r in rows}, "imported a neighbouring building's suite"
