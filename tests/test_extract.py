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
