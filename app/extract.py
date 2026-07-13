"""Feed/HTML -> a normalized Listing dict. TWO fast paths and one fallback, in order:

  1. WordPress REST  — the big win. RIPCO alone publishes 833 listings as clean JSON at
     /wp-json/wp/v2/property-listings. No scraping at all.
  2. JSON-LD         — <script type="application/ld+json"> on the detail page.
  3. HTML + LLM      — last resort. The listing container is stripped to plain text and
                       ONE prompt maps it to the schema. NO per-site CSS parsers: a
                       redesign costs nothing, and a new site is a URL in sources.yml.

Whatever the path, we store FACTS, never expression:
  - `our_description` is written by US from the facts. The broker's marketing prose is
    never persisted — we link `source_url` for the original.
  - `photo_urls` are the broker's own URLs, referenced. Never downloaded, never re-hosted.
"""
import json
import logging
import re
from html import unescape

from pydantic import BaseModel

from . import ai, cache
from .config import settings

log = logging.getLogger("openlease")

_TYPES = ("retail", "office", "industrial", "flex", "land")

# Anthropic pricing for the default `llm_model` (claude-opus-4-8): $5/1M input tokens,
# $25/1M output tokens (i.e. $0.0005c/input-tok, $0.0025c/output-tok) — same rates and
# same derivation style as ai.py's own `_PARSE_COST_CENTS`/`_REPLY_COST_CENTS`.
#
# from_html_llm (messages.parse, max_tokens=2048): system prompt (~150 tok) + the
# ListingExtract schema definition sent with the request (~350 tok — nearly twice
# QueryExtract's field count) + up to 20,000 CHARS of page markdown (~5,000 tok at
# ~4 chars/token) -> ~5,500 input tokens. The parsed-JSON output is normally 200-500
# tokens (well under the 2048 cap).
#   ~5500 * 0.0005c + ~500 * 0.0025c = 2.75c + 1.25c =~ 4c -> rounded up to 5c for headroom.
_HTML_LLM_COST_CENTS = 5

# A WP post TITLE is marketing copy ("280 Broadway – Ground Floor Retail!!"), not
# structured data — it is only a safe address fallback when it actually LOOKS like a
# street address (starts with a house number). See from_wp_json's address fallback below.
_ADDR_LIKE = re.compile(r"^\d+\s+\S")

# US state codes, for reading a WP slug's trailing "-city-st".
_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv",
    "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn",
    "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
}
_SLUG_ADDR = re.compile(r"^\d")


def _slug_address(slug: str) -> str | None:
    """A WP post slug that ends in a state code is a normalized full address:
    "2446-broadway-new-york-ny" -> "2446 broadway new york ny". A post title usually is not
    ("2446 Broadway" has no city). Returns None when the slug doesn't start with a house
    number or doesn't end in a state code."""
    parts = _slug_parts(slug)
    if len(parts) < 3 or parts[-1] not in _STATES or not _SLUG_ADDR.match(parts[0]):
        return None
    return " ".join(parts)


def _slug_state(slug: str) -> str | None:
    """The US state a WP slug names, if any. This is the ONLY reliable signal that a
    national feed's listing is out of market, and it has to be read BEFORE geocoding.

    A metro-scoped geocoder does not decline: NYC GeoSearch, handed "302 south colonial
    drive cleburne TX", confidently returns coordinates in Brooklyn. So a bbox check after
    the fact cannot save us — the wrong answer is already inside the bbox. RIPCO's feed is
    national (Cleburne TX, Panama City FL, Freehold NJ), and without this every one of them
    was filed as New York and handed a New York Walk Score.
    """
    parts = _slug_parts(slug)
    return parts[-1] if len(parts) >= 3 and parts[-1] in _STATES else None


def _slug_parts(slug: str) -> list[str]:
    """WordPress appends `-2`, `-3`… to a duplicate slug, so the real last token isn't
    always last: `2732-e-15th-st-panama-city-fl-2` ends in "2", not "fl". Reading the state
    off the raw tail let a Panama City, Florida property through as New York."""
    parts = slug.lower().split("-")
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    return parts


class ListingExtract(BaseModel):
    """Same two rules as ai.QueryExtract, for the same two reasons: no `| None` (>16
    union params = 400) and no defaults (any optional param = a 2^N grammar = the request
    HANGS). Sentinels: "" / 0 mean the page didn't say."""
    address: str
    neighborhood: str
    property_type: str        # retail | office | industrial | flex | land | ""
    transaction_type: str     # lease | sale | ""
    size_sf: int
    divisible_min_sf: int
    divisible_max_sf: int
    floor: str
    ceiling_height_ft: float
    asking_rent: float
    rent_unit: str            # sf_yr | sf_mo | mo | ""
    lease_type: str
    sale_price: int
    availability_date: str
    broker_name: str
    broker_firm: str
    broker_phone: str
    broker_email: str
    features: list[str]
    our_description: str      # OUR words, from the facts — NOT the page's marketing copy

    def to_listing(self) -> dict:
        d = {k: v for k, v in self.model_dump().items() if v not in ("", 0, 0.0, [])}
        if "features" in d:
            d["features_json"] = json.dumps(d.pop("features"))
        # A rent with no unit is not a rent. The card would render it "/SF/yr" while
        # db.filter_listings' CASE has no ELSE and yields NULL — so the row is shown at one
        # price and silently excluded from every search that caps on it. Display and filter
        # must never disagree: if we don't know the unit, we don't have the rent.
        if d.get("asking_rent") and not d.get("rent_unit"):
            d.pop("asking_rent")
        return d


# Space that is already gone. Brokers keep a leased deal on the site as a trophy — five of
# Terranova's nine Miami "listings" were titled "... – Leased" — and a corpus that answers
# "what can I rent in Wynwood" with space somebody else already rented is not a search
# engine, it is a scrapbook. The marker shows up in the title AND in the URL slug
# (/property/300-miracle-mile-leased), so both are checked. We drop the row rather than
# storing a status nobody would remember to filter on.
_OFF_MARKET = re.compile(
    r"\b(leased|sold|off[\s\-]?market|no longer available|under contract|"
    r"in contract|rented|withdrawn)\b", re.I)


def off_market(*texts: str | None) -> bool:
    return any(_OFF_MARKET.search(t) for t in texts if t)


def _clean(d: dict, src: dict, url: str, metro: str) -> dict | None:
    if not d.get("address"):
        return None
    # A feed hands us its title HTML-escaped ("105 Miracle Mile &#8211; Leased"), and an
    # address is a FACT: it does not get to carry &#8211; into the database, the map pin, or
    # the geocoder query (which is one reason these rows never pinned).
    for k in ("address", "neighborhood", "borough", "floor"):
        if isinstance(d.get(k), str):
            d[k] = unescape(d[k]).strip()
    if off_market(d.get("address"), url):
        return None
    d["source"] = src["key"]
    d["source_url"] = url
    d["metro"] = metro
    d.setdefault("transaction_type", "lease")
    if d.get("property_type") not in _TYPES:
        d.pop("property_type", None)
    return d


# --- rung 3a: WordPress REST --------------------------------------------------

def from_wp_json(item: dict, src: dict, metro: str) -> dict | None:
    """WP custom-post-type listing. Field names vary by theme, so we look in the usual
    places and let the LLM description step fill the gaps — never a per-site parser."""
    meta = item.get("acf") or item.get("meta") or {}
    title = (item.get("title") or {}).get("rendered", "") if isinstance(item.get("title"), dict) \
        else (item.get("title") or "")
    title = unescape(re.sub(r"<[^>]+>", "", title)).strip()
    if off_market(title, item.get("slug") or ""):
        return None      # the deal is done — see _OFF_MARKET

    def pick(*keys):
        for k in keys:
            v = meta.get(k) or item.get(k)
            if v not in (None, "", []):
                return v
        return None

    def num(v, cast=int):
        if v is None:
            return None
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        try:
            return cast(m.group()) if m else None
        except (TypeError, ValueError):
            return None

    raw_addr = pick("address", "property_address", "street_address")
    if not raw_addr and _ADDR_LIKE.match(title):
        # Some WP themes really do put the address in the post title verbatim ("280
        # Broadway, 2nd Floor"). But a title is marketing copy by default ("280 Broadway
        # — Ground Floor Retail!!"), so this fallback only fires when the title actually
        # LOOKS like a street address (starts with a house number) — never a bare
        # "or title", which would silently write the headline into a FACT field.
        raw_addr = title
    d = {
        "address": raw_addr,
        # The WP slug is a normalized FULL address ("2446-broadway-new-york-ny"); the title
        # usually isn't ("2446 Broadway"). A bare street name with no city is not a
        # resolvable address — and a metro-scoped geocoder will confidently "find" it
        # anyway. That is how "2732 East 15th Street | Panama City Commercial Parcel" got
        # matched to a same-named street in Brooklyn, filed under NYC, and handed a New York
        # Walk Score. So: hand the geocoder the slug when the slug carries a city and state,
        # and let crawl._place drop whatever falls outside the four metros.
        "geo_hint": _slug_address(item.get("slug") or ""),
        "geo_state": _slug_state(item.get("slug") or ""),
        "neighborhood": pick("neighborhood", "submarket"),
        "property_type": (str(pick("property_type", "type") or "").lower() or None),
        "size_sf": num(pick("size", "square_feet", "sf", "total_sf")),
        "divisible_min_sf": num(pick("divisible_min", "min_sf")),
        "divisible_max_sf": num(pick("divisible_max", "max_sf")),
        "asking_rent": num(pick("asking_rent", "rent", "price_per_sf"), float),
        "rent_unit": "sf_yr" if pick("asking_rent", "rent", "price_per_sf") else None,
        "broker_name": pick("broker", "agent", "contact_name"),
        "broker_phone": pick("phone", "contact_phone"),
        "broker_email": pick("email", "contact_email"),
        "broker_firm": src["name"],
        # photos: the broker's own URLs, hot-linked. NEVER downloaded.
        "photo_urls_json": json.dumps([
            u for u in [pick("featured_image", "image", "thumbnail")] if isinstance(u, str)
        ]) or None,
    }
    d = {k: v for k, v in d.items() if v is not None}
    d = _clean(d, src, item.get("link") or item.get("guid", {}).get("rendered", ""), metro)
    if d:
        d["our_description"] = describe(d)      # our words, not the post's content field
    return d


# --- rung 3b: JSON-LD ---------------------------------------------------------

_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                 re.S | re.I)


# The JSON-LD node has to BE a property. Every one of these sites also emits an
# Organization / RealEstateAgent node carrying the BROKERAGE'S OWN OFFICE address, on every
# page including the blog — and a rung that takes an address from any node with an address
# stored Metro 1's Wynwood headquarters five times and called it Miami's inventory. An
# allowlist, not a denylist: an unrecognized @type is not a listing, and refusing costs us
# nothing because the caller then descends to from_html_facts, which reads the page itself.
_LISTING_TYPES = {
    "realestatelisting", "product", "offer", "place", "accommodation", "apartment",
    "house", "residence", "singlefamilyresidence", "room", "suite", "commercialproperty",
    "landmarksorhistoricalbuildings", "selfstorage", "warehouse",
}


def _is_listing_node(node: dict) -> bool:
    t = node.get("@type") or node.get("type") or ""
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower() in _LISTING_TYPES for x in types)


def from_jsonld(html: str, url: str, src: dict, metro: str) -> dict | None:
    for blob in _LD.findall(html):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            nodes = data["@graph"]          # Yoast wraps everything in a @graph
        for node in nodes:
            if not isinstance(node, dict) or not _is_listing_node(node):
                continue
            addr = node.get("address")
            if isinstance(addr, dict):
                street = addr.get("streetAddress")
                city = addr.get("addressLocality")
            else:
                street, city = (addr if isinstance(addr, str) else None), None
            if not street:
                continue
            offer = node.get("offers") or {}
            size = node.get("floorSize") or {}
            d = {
                "address": f"{street}, {city}" if city else street,
                "neighborhood": city,
                "size_sf": int(size.get("value")) if str(size.get("value", "")).isdigit() else None,
                "asking_rent": float(offer["price"]) if str(offer.get("price", "")).replace(".", "").isdigit() else None,
                "rent_unit": "sf_yr" if offer.get("price") else None,
                "photo_urls_json": json.dumps(
                    [node["image"]] if isinstance(node.get("image"), str) else (node.get("image") or [])
                ),
                "broker_firm": src["name"],
            }
            d = {k: v for k, v in d.items() if v is not None}
            d = _clean(d, src, url, metro)
            if d:
                d["our_description"] = describe(d)
            return d
    return None


# --- rung 3c: facts out of the page text, keyless (no per-site parsers) -------
#
# The rung that makes the product usable without a key. Most broker sites publish no
# structured feed and no real-estate JSON-LD — their listings are prose on an HTML page —
# so before this rung the ONLY way to get a size or an asking rent was the LLM, and a
# keyless crawl produced a link directory: addresses with no SF and no rent, which the hard
# filter ("~1,500 SF under $8k/mo") cannot filter on at all.
#
# Reading "1,500 SF" out of a page is not a per-site CSS parser — there is no selector here,
# it works on any site, and a redesign costs nothing. And a number is a FACT: we are not
# copying anyone's expression. This is the "widen the generic key lists" instruction, taken
# to the page text.

_TAGS = re.compile(r"<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<footer.*?</footer>",
                   re.S | re.I)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_OG_TITLE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

# "1,500 SF" / "1500 sq ft" / "1,500 square feet". Requires a unit — a bare number is not a size.
_SIZE = re.compile(r"([\d][\d,]{1,8})\s*(?:\+/-\s*)?(?:SF\b|sq\.?\s?ft\b|square\s+feet)", re.I)
# "$95/SF/YR", "$4.75 per SF" (LA quotes monthly), "$95.00/SF"
_RENT_SF = re.compile(r"\$\s?([\d][\d,]*\.?\d*)\s*(?:/|\s+per\s+)\s*(?:SF|sq\.?\s?ft)", re.I)
_RENT_SF_MO = re.compile(r"\$\s?([\d][\d,]*\.?\d*)\s*(?:/|\s+per\s+)\s*(?:SF|sq\.?\s?ft)\s*/?\s*(?:mo\b|month)", re.I)
# "$8,000/mo", "$8,000 per month"
_RENT_MO = re.compile(r"\$\s?([\d][\d,]{2,})\s*(?:/|\s+per\s+)\s*(?:mo\b|month)", re.I)
_SALE = re.compile(r"\$\s?([\d][\d,]{5,})(?!\s*(?:/|\s+per\s+))", re.I)

# Sanity bounds. A "size" of 3 SF or 40,000,000 SF is a parse artifact, not a listing.
# "…, Venice, CA 90291" / "…, Oxnard, CA". The city + state, which is what turns a bare
# street name into a geocodable address — and what reveals a listing that is not in this
# market at all.
_STATE_RE = (r"A[LKZR]|C[AOT]|DE|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]"
             r"|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY]|DC")
_CITY = re.compile(r"([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3}),\s*(" + _STATE_RE + r")\b(?!\w)")
# Words that belong to the STREET, not the city — "540 Rose Avenue Venice, CA" has no comma
# before the city, so the capture runs back through the street name.
_STREET_WORDS = {
    "street", "st", "avenue", "ave", "boulevard", "blvd", "drive", "dr", "road", "rd",
    "place", "pl", "court", "ct", "lane", "ln", "parkway", "pkwy", "highway", "hwy",
    "way", "terrace", "circle", "square", "north", "south", "east", "west", "suite", "ste",
    "floor", "unit",
    # CRE boilerplate that sits right where a city would ("… Avenue NNN Venice, CA")
    "nnn", "mg", "fs", "igg", "lease", "sale", "sf", "available", "for", "space",
}


def _city_of(text: str, address: str = "") -> tuple[str, str] | None:
    """The city and state a page names.

    Pages write "540 Rose Avenue Venice, CA 90291" — no comma before the city — so the
    capture runs back through the street name. We already KNOW the street (it's `address`),
    so subtract its words: what's left is the city. "Santa Monica" survives; "Rose" doesn't.
    """
    m = _CITY.search(text)
    if not m:
        return None
    known = {w.lower() for w in re.findall(r"[\w.\-]+", address)}
    words = [w for w in m.group(1).split()
             if w.lower() not in _STREET_WORDS and w.lower() not in known and not w.isdigit()]
    if not words:
        return None
    return " ".join(words[-2:]) if len(words) > 1 else words[0], m.group(2)


_MIN_SF, _MAX_SF = 100, 2_000_000
_MIN_SF_YR, _MAX_SF_YR = 5.0, 1_000.0        # $/SF/yr
_MIN_SF_MO, _MAX_SF_MO = 0.5, 90.0           # $/SF/mo (LA/industrial convention)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def page_text(html: str) -> str:
    """The page with its chrome stripped. No CSS selector — nav/footer/script go by tag."""
    t = _TAGS.sub(" ", html)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _headline(html: str) -> str | None:
    for pat in (_H1, _OG_TITLE, _TITLE):
        m = pat.search(html)
        if m:
            h = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            # Split on a plain hyphen as well as the pipes and dashes. Without it,
            # "<title>540 Rose Avenue - WESTMAC Commercial Brokerage</title>" became the
            # ADDRESS — a fact field — and "280 Broadway - Ground Floor Retail!! - Prime
            # Corner" persisted the broker's own marketing headline, which is the one thing
            # we never store.
            h = re.split(r"\s+[|\u2013\u2014\-\u00b7\u2022]\s+", h)[0].strip()
            if h:
                return h[:120]
    return None


# A broker page is FULL of numbers that are not this listing's. Metro Manhattan's pages
# quote the Midtown market ("asking rents held flat at $78.23/SF — Cushman & Wakefield"),
# list a size-filter dropdown (1,000 / 1,999 / 4,999 / 9,999), and give the whole building's
# footprint (807,000 SF). Taking min() of everything that looked like a size or a rent
# INVENTED facts: every Metro Manhattan listing came out at "$78/SF/yr", which is a market
# statistic, not an ask.
#
# So: anchor on the page's own LABEL. "Asking Rent: $78" is this listing's rent; a number
# floating in a paragraph about the market is not. If nothing is labelled, we do not guess —
# a wrong rent is worse than no rent, and a search that filters on a fabricated number is
# worse than one that filters on nothing.
_RENT_LABEL = re.compile(
    r"(?:asking\s+rent|rent\s*/\s*sf|rent\s+per\s+sf|monthly\s+rent|asking\s+price"
    r"|asking|rent|rate|price)\s*[:\-\u2013/]*\s*"
    r"\$\s?([\d][\d,]*\.?\d*)\s*"
    r"(?:\s*/\s*|\s+per\s+)?(sf|sq\.?\s?ft)?(?:\s*/\s*(mo\b|month|yr\b|year))?",
    re.I)

# The page's OWN decoys, all seen live. Each one sits right where a rent would:
#   "Max Rent/Month  $5,000 $10,000 $15,000"   a filter dropdown (metro-manhattan)
#   "Triple net charges ±$1.41/SF/Mo."         the NNN charge, NOT the rent (westmac)
#   "Related Listings ... $4.50/SF/Mo."        a DIFFERENT building (westmac)
#   "asking rents held flat at $78.23/SF"      a market statistic (metro-manhattan)
_RENT_DECOY = re.compile(
    r"max\s+rent|min\s+rent|triple\s+net|nnn\s+charge|cam\s+charge|filter"
    r"|related\s+listing|similar|nearby|market|average|comparable|held\s+flat", re.I)
_DECOY_WINDOW = 60          # chars of run-up to check for a decoy word


def _decoyed(text: str, at: int) -> bool:
    return bool(_RENT_DECOY.search(text[max(0, at - _DECOY_WINDOW):at]))


# The UNIT is not optional — it is the only thing separating a size from any other number
# that happens to follow a label. Without it, "Availability: 2026" made a DATE into a
# 2,026 SF listing, so "availability" is out of the label set too.
_SIZE_LABEL = re.compile(
    r"(?:size|space available|sf\s+available|square\s+footage|divisible)"
    r"\s*[:\-\u2013]?\s*([\d][\d,]{1,8})\s*(?:\+/-\s*)?"
    r"(?:SF\b|sq\.?\s?ft|square\s+feet)", re.I)
# ...but the word "size" is not always THIS SPACE's size. "Building Size", "Typical Floor
# Size" and "Lot Size" all contain it, and all three name something a tenant cannot lease.
_SIZE_DECOY = re.compile(
    r"building|typical\s+floor|floor\s+plate|floorplate|lot|land|site|total|"
    r"min(?:imum)?|max(?:imum)?", re.I)
_SIZE_DECOY_WINDOW = 22


def _size_decoyed(text: str, at: int) -> bool:
    return bool(_SIZE_DECOY.search(text[max(0, at - _SIZE_DECOY_WINDOW):at]))


# A square footage inside a sentence about the NEIGHBOURHOOD is not this space. 362 Van Brunt
# Street is a Red Hook storefront whose page sells the area around it: "a rebuilt port, 28
# acres of public open space, and more than 275,000 SF of..." — the only other figure on the
# page is a LOT size, so once that was (correctly) decoyed out, the district's development
# pipeline was the sole surviving candidate and a 275,000 SF SUITE went into the database.
#
# The markers are deliberately narrow. "District", "waterfront" and "neighborhood" are NOT
# here: a real listing says "this 2,400 SF space in the Design District" all the time, and
# decoying on those would throw away good listings to catch a rare bad one. Acreage and
# public open space, in the same breath as a square footage, is area prose — a suite is not
# measured in acres.
_AREA_PROSE = re.compile(r"\bacres?\b|open\s+space|parkland|esplanade|public\s+plaza", re.I)
_SENTENCE_BACK = 240


def _in_area_prose(text: str, at: int) -> bool:
    start = text.rfind(".", max(0, at - _SENTENCE_BACK), at) + 1
    end = text.find(".", at)
    end = end if 0 < end < at + 90 else at + 90
    return bool(_AREA_PROSE.search(text[start:end]))


# A broker page for a MULTI-TENANT BUILDING has no single "size", and Rexford's pages say so
# in as many words:
#     "Property Total SF: 125,514"        <- the BUILDING, not a suite
#     "Available Unit(s) SF 5,961-9,358"  <- a RANGE of what you can actually lease
#     "21 tenant spaces ranging from 1,700 to 15,000"
# Reading any one of those as "the size" is wrong in a different way each time — and the
# repeated one is the BUILDING, so "most repeated" picked 125,514 SF as the size of a
# 9,358 SF unit. These labels are ordinary CRE vocabulary, not one site's markup.
_TOTAL_LABEL = re.compile(
    r"(?:property\s+total\s+sf|total\s+sf|total\s+square\s+f(?:eet|ootage)"
    r"|building\s+(?:size|sf)|total\s+building)"
    r"\s*[:\-\u2013]?\s*(?:\u00b1\s*)?([\d][\d,]{2,9})", re.I)
# The building said in PROSE rather than in a stats table. Blanca's pages are Class A office
# TOWERS, and their availabilities live off-site \u2014 so the biggest number on the page is the
# whole tower: "1450 Brickell is a 35-story, 625,800 RSF Class A office tower". Nothing
# labels that "Total SF", so the labelled pattern above sailed past it and we filed a
# 625,800 SF SUITE. A figure that the sentence itself calls a tower/building/campus is the
# BUILDING, and is never what a tenant is renting. (Note RSF \u2014 "rentable square feet" \u2014 which
# _SIZE deliberately does not match, but which appears alongside a plain "SF" restatement.)
_BUILDING_DESC = re.compile(
    r"([\d][\d,]{2,9})\s*(?:\+/-\s*)?(?:R?SF\b|sq\.?\s?ft\b|square[\s-]?f(?:oot|eet))"
    r"[\s,\-\u2013]*(?:\w+[\s,\-\u2013]+){0,3}?"
    r"(?:building|tower|property|centre|center|campus|complex|development|facility)\b", re.I)
_AVAIL_RANGE = re.compile(
    r"(?:available[^.]{0,24}?|spaces?\s+available[^.]{0,12}?)"
    r"([\d][\d,]{2,8})\s*[-\u2013]\s*([\d][\d,]{2,8})\s*(?:SF\b|sq)?", re.I)

# A multi-tenant building that lists its units one by one instead of as a range. RIPCO's
# pages read:
#   "Total Square Feet \u00b19,113 SF ... Proposed Divisions Retail A: 1,608 SF Retail B: 2,450
#    SF Retail C: 2,286 SF Retail D: 2,769 SF"
# Those four ARE the leasable spaces \u2014 the whole reason a tenant is on the page. But no
# figure repeats and there is more than one, so _size_of correctly refused to guess and the
# listing went in with no size at all: invisible to every size filter, across ~294 RIPCO
# listings. The refusal was right; the answer was sitting under a header saying what these
# numbers are.
_DIVISIONS_HDR = re.compile(
    r"(?:proposed\s+divisions?|available\s+spaces?|spaces?\s+available|suites?\s+available"
    r"|divisions?)\b", re.I)
_DIVISIONS_WINDOW = 340


def _building_sf(text: str) -> int | None:
    for pat in (_TOTAL_LABEL, _BUILDING_DESC):
        for m in pat.finditer(text):
            v = _num(m.group(1))
            if _MIN_SF <= v <= _MAX_SF:
                return int(v)
    return None


def _available_range(text: str) -> tuple[int, int] | None:
    """"Available Unit(s) SF 5,961-9,358" -> (5961, 9358). What you can actually lease."""
    for m in _AVAIL_RANGE.finditer(text):
        lo, hi = _num(m.group(1)), _num(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        if _MIN_SF <= lo <= hi <= _MAX_SF:
            return int(lo), int(hi)
    return None


def _divisions(text: str) -> list[int] | None:
    """The units a multi-tenant building lists one by one. See _DIVISIONS_HDR.

    Only figures INSIDE the divisions block count — the header tells us what these numbers
    are, which is exactly the knowledge _size_of lacks when it refuses to pick between four
    one-off candidates. Outside that block we go back to refusing.

    The BUILDING is never one of its own units. 90 Broad Street lists "Space A Ground Floor
    700 SF, Space B Ground Floor 650 SF" and then, still inside the window, the 420,000 SF
    tower they sit in — so the largest "unit" came out as the whole building and a 700 SF
    storefront was filed as a 420,000 SF one.
    """
    m = _DIVISIONS_HDR.search(text)
    if not m:
        return None
    building = _building_sf(text)
    block = text[m.end():m.end() + _DIVISIONS_WINDOW]
    sizes = [_num(x.group(1)) for x in _SIZE.finditer(block)
             if not _size_decoyed(block, x.start())]
    sizes = sorted({int(s) for s in sizes
                    if _MIN_SF <= s <= _MAX_SF and s != building})
    return sizes if len(sizes) >= 2 else None


def _size_of(text: str) -> int | None:
    """This listing's size, or None.

    A broker page is full of square footages that are not this suite's: the whole BUILDING's
    footprint ("1250 Broadway is a 807,000 SF tower"), a size-filter dropdown, and a "nearby
    availabilities" module listing OTHER suites in the same building.

    A labelled figure wins. Otherwise the most REPEATED one — a listing states its own size
    several times, while a tower's footprint and each dropdown option are stated once. And if
    NOTHING repeats and there is more than one candidate, we refuse: taking "the first in
    document order" is exactly how a 807,000 SF tower became the size of a 3,305 SF suite.
    """
    from collections import Counter
    building = _building_sf(text)          # explicitly NOT this listing's size
    # A figure introduced by "Building Size", "Typical Floor Size" or "Lot Size" describes
    # the property, not the space — and it is excluded HERE, before it can be counted, not
    # just from the labelled shortlist below. Blanca's towers publish a spec sheet
    # ("Building Size: 625,800 SF ... Typical Floor Size: 17,881 SF") and send you to a
    # third party for the actual availabilities, so EVERY square footage on the page is a
    # building spec. Excluding only the tower left the floorplate as the last candidate
    # standing, and a floorplate is not an availability either. When a page states no
    # leasable size, the honest answer is that we don't have one.
    found = [_num(m.group(1)) for m in _SIZE.finditer(text)
             if not _size_decoyed(text, m.start()) and not _in_area_prose(text, m.start())]
    found = [s for s in found if _MIN_SF <= s <= _MAX_SF and s != building]
    if not found:
        return None
    counts = Counter(found)

    # A LABEL can lie about what it labels. Blanca's spec sheet reads
    #   "Building Size: 625,800 SF ... Typical Floor Size: 17,881 SF"
    # and _SIZE_LABEL matches the word "Size" INSIDE "Building Size" — so the labelled
    # branch confidently handed back 625,800, the very number _building_sf had just
    # excluded as the tower. Two guards, and both are needed:
    #   1. the building is not a candidate here either (it was already dropped from `found`);
    #   2. a size labelled as the BUILDING's, the LOT's, or a TYPICAL FLOOR's is not the
    #      size of the space you can lease. A floorplate is not an availability.
    labelled = [_num(m.group(1)) for m in _SIZE_LABEL.finditer(text)
                if not _size_decoyed(text, m.start())]
    labelled = [s for s in labelled if _MIN_SF <= s <= _MAX_SF and s != building]
    if labelled:
        v = labelled[0]
        # A label is not proof. "Size  1,000 SF  1,999 SF  4,999 SF" is a DROPDOWN whose
        # first option happens to sit behind the word "Size" — the same bug in a hat. Trust
        # the label only when the value repeats, or when it is the only size on the page.
        if counts[v] >= 2 or len(counts) == 1:
            return int(v)
        return None

    top, n = counts.most_common(1)[0]
    if n < 2 and len(counts) > 1:
        return None          # nothing repeats and several candidates — we cannot tell which
    return int(top)


def _rent_of(text: str, metro: str = "", ptype: str = "") -> tuple[float, str] | None:
    """This listing's ask, and the unit it is quoted in.

    Every real broker page we crawl puts the rent behind a label and surrounds it with
    decoys, all of which are shaped exactly like a rent:

      metro-manhattan  "Size: 3,305 SF  Rent/SF: $60  Monthly Rent: $16,525"   <- the ask
                       "Max Rent/Month  $5,000 $10,000 $15,000 $20,000"        <- a filter
                       "asking rents held flat at $78.23/SF (Cushman)"         <- the market
      westmac          "540 Rose Avenue For Lease - $10.00/SF/Mo. NNN"         <- the ask
                       "Triple net charges +/-$1.41/SF/Mo."                    <- the NNN charge
                       "Related Listings ... 1702 Lincoln Blvd $4.50/SF/Mo."   <- another building
      ripco            "Asking Rent Upon Request"                              <- no rent, honestly

    (3,305 SF x $60/SF/yr = $16,525/month. The page is internally consistent; the naive
    reader is not.)

    So: take a LABELLED figure that isn't preceded by a decoy word. Failing that, take an
    unlabelled one only if the page quotes exactly one $/SF figure. Otherwise refuse — a
    wrong rent is worse than no rent.
    """
    per_sf_hits: list[tuple[float, str]] = []
    gross_hits: list[float] = []

    for m in _RENT_LABEL.finditer(text):
        if _decoyed(text, m.start()):
            continue
        val = _num(m.group(1))
        per_sf = bool(m.group(2)) or bool(re.search(r"rent\s*/\s*sf|rent\s+per\s+sf",
                                                    m.group(0), re.I))
        period = (m.group(3) or "").lower()
        monthly = period.startswith("mo")

        if per_sf:
            unit = _unit_for(val, period, metro, ptype)
            if unit:
                per_sf_hits.append((val, unit))
        elif val >= 500:
            # A gross figure. "Monthly Rent: $16,525" is a rent; "Asking Price $6,500,000"
            # is a SALE and is handled separately — never as a rent.
            if re.search(r"monthly\s+rent|rent\s*/\s*month|per\s+month", m.group(0), re.I) \
                    or monthly:
                gross_hits.append(val)

    if per_sf_hits:
        return per_sf_hits[0]            # the per-SF ask is the one a broker filters on
    if gross_hits:
        return gross_hits[0], "mo"

    # Nothing labelled. An unlabelled figure is only safe when it is the ONLY one on the
    # page AND nothing decoy-shaped introduces it.
    mo = [_num(m.group(1)) for m in _RENT_SF_MO.finditer(text)
          if not _decoyed(text, m.start()) and _MIN_SF_MO <= _num(m.group(1)) <= _MAX_SF_MO]
    if len(set(mo)) == 1:
        return mo[0], "sf_mo"
    if mo:
        return None
    bare = [_num(m.group(1)) for m in _RENT_SF.finditer(text)
            if not _decoyed(text, m.start())]
    if len(set(bare)) != 1:
        return None
    unit = _unit_for(bare[0], "", metro, ptype)
    return (bare[0], unit) if unit else None


# The yearly and monthly bands OVERLAP: $5-$90/SF is plausible as either. LA and industrial
# quote per MONTH; everyone else quotes per YEAR — and a page that states the convention once
# in a header and then just writes "$5.75/SF" is common. Defaulting an unqualified figure to
# yearly turned a $69/SF/yr West LA office into a $5.75/SF/yr bargain, which then surfaced in
# every "cheap space" search a broker ran. A 12x error that READS AS A BARGAIN is the worst
# possible way to be wrong. So if the page doesn't say, and the market and the asset class
# don't settle it, we refuse.
_AMBIGUOUS_LO, _AMBIGUOUS_HI = _MIN_SF_YR, _MAX_SF_MO        # [5.0, 90.0]


def _unit_for(val: float, period: str, metro: str, ptype: str) -> str | None:
    if period.startswith("mo"):                    # the page said monthly
        return "sf_mo" if _MIN_SF_MO <= val <= _MAX_SF_MO else None
    if period:                                     # the page said yearly
        return "sf_yr" if _MIN_SF_YR <= val <= _MAX_SF_YR else None

    if val > _AMBIGUOUS_HI:                        # too big to be a monthly per-SF rate
        return "sf_yr" if val <= _MAX_SF_YR else None
    if val < _AMBIGUOUS_LO:                        # too small to be a yearly per-SF rate
        return "sf_mo" if val >= _MIN_SF_MO else None

    if metro == "la" or ptype in ("industrial", "flex"):
        return "sf_mo"                             # the LA / industrial convention
    if metro in ("nyc", "mia"):
        return "sf_yr"
    return None                                    # we do not know, so we do not say


_SALE_LABEL = re.compile(
    r"(?:asking\s+price|sale\s+price|offered\s+at|price)\s*[:\-\u2013]?\s*"
    r"\$\s?([\d][\d,]{5,})", re.I)


def _sale_of(text: str) -> int | None:
    """RIPCO says "Asking Rent Upon Request" and "Asking Price $6,500,000" on the same page:
    it is FOR SALE, and refusing the rent (correctly) left us with nothing at all."""
    for m in _SALE_LABEL.finditer(text):
        if _decoyed(text, m.start()):
            continue
        val = _num(m.group(1))
        if 50_000 <= val <= 5_000_000_000:
            return int(val)
    return None


def from_html_facts(html: str, url: str, src: dict, metro: str) -> dict | None:
    """Rung 3c. Facts (size, ask, type) out of the page's own text — keyless, generic.

    Returns None unless it finds an address AND at least one hard fact (size or rent).
    A page we can only get an address off is not worth a row: the search filters on SF and
    rent, so a listing with neither is invisible to every query that matters.
    """
    txt = page_text(html)
    addr = _headline(html)
    if not addr or not _ADDR_LIKE.match(addr):
        # No street-address-looking headline. Fall back to the URL slug, which is where
        # these sites put it ("/listings/1234-w-fulton-market/").
        slug = re.sub(r"[/?#].*$", "", url.rstrip("/").rsplit("/", 1)[-1])
        cand = slug.replace("-", " ").strip()
        # A slug is lowercase ("2235-sepulveda"), and an address is a fact we show a broker.
        # Title-case it — but leave a directional prefix alone ("nw", "se") rather than
        # writing "Nw 2nd Ave".
        def _cap(w: str) -> str:
            if w.lower() in ("nw", "ne", "sw", "se"):
                return w.upper()
            if w[:1].isdigit():
                return w.lower()          # str.title() turns "2nd" into "2Nd"
            return w.title()

        cand = " ".join(_cap(w) for w in cand.split())
        addr = cand if _ADDR_LIKE.match(cand) else None
    if not addr:
        return None

    d: dict = {"address": addr, "broker_firm": src["name"]}

    # The city, if the page names one. This is what makes the address GEOCODABLE — and
    # what lets us tell that a listing isn't in this market at all. Rexford is a SoCal-wide
    # REIT: crawled under `la`, it hands us buildings in Oxnard (Ventura County) and
    # Carlsbad (San Diego County). Their pages say so; a bare street name does not.
    city = _city_of(txt, addr)
    if city:
        d["geo_hint"] = f"{addr}, {city[0]}, {city[1]}"
        d["geo_state"] = city[1].lower()      # lets _out_of_market reject it before geocoding

    total = _building_sf(txt)
    if total:
        d["total_building_sf"] = total     # the BUILDING. Never the suite.

    rng = _available_range(txt)
    divs = _divisions(txt)
    if rng:
        # A multi-tenant building leases a RANGE. The largest contiguous unit is what a
        # tenant with a size in mind is actually shopping for, so that is `size_sf` — and the
        # range itself is kept, because "divisible to 5,961 SF" is the answer to a different
        # question a broker asks constantly.
        d["divisible_min_sf"], d["divisible_max_sf"] = rng
        d["size_sf"] = rng[1]
    elif divs:
        # Same building, units listed one by one instead of as a range. Same answer.
        d["divisible_min_sf"], d["divisible_max_sf"] = divs[0], divs[-1]
        d["size_sf"] = divs[-1]
    else:
        size = _size_of(txt)
        if size:
            d["size_sf"] = size

    # The TYPE has to be read before the rent: LA and industrial quote per MONTH, and an
    # unqualified "$5.75/SF" cannot be resolved without knowing which.
    #
    # And it is the type the page actually TALKS about, not the first one it happens to
    # mention. Taking the first hit in _TYPES order typed every Metro Manhattan listing
    # "retail" — their pages say "office" twenty times and "retail" twice, and "retail" is
    # first in the tuple. A 1-vs-1 tie must not fall back to that same bias either
    # ("ground-floor retail below" on an office page), so a tie goes to the non-retail term.
    counts = {t: len(re.findall(r"\b" + t + r"\b", txt, re.I)) for t in _TYPES}
    best = max(counts, key=lambda t: (counts[t], t != "retail"))
    if counts[best]:
        d["property_type"] = best

    rent = _rent_of(txt, metro, d.get("property_type", ""))
    if rent:
        d["asking_rent"], d["rent_unit"] = rent

    # A sale, not a lease. RIPCO's 57 West 38th St says "Asking Rent Upon Request" and
    # "Asking Price $6,500,000" on the same page: correctly refusing the rent left us with
    # nothing at all, when the page was telling us plainly what it was.
    if not d.get("asking_rent"):
        # A SALE — but only on the page's own evidence. `page_text` strips <nav> and
        # <footer>, not a <header> menu, and every one of these sites has a "Properties For
        # Sale" link in it. Reading "for sale" off the site's own navigation retyped LEASE
        # listings as sales, hiding them from every lease search. So a price is the proof;
        # bare "for sale" prose only counts when it is nowhere near the menu.
        price = _sale_of(txt)
        if price:
            d["transaction_type"] = "sale"
            d["sale_price"] = price
        elif re.search(r"\b(?:for\s+sale|sale\s+price|offered\s+for\s+sale)\b",
                       txt[:4000], re.I) and not re.search(r"\bfor\s+lease\b", txt[:4000], re.I):
            d["transaction_type"] = "sale"

    # A suite cannot be larger than the building it is in. When it comes out that way we have
    # mixed up two different figures on the page (1355 Alton reported a 7,000 SF space inside
    # a 3,500 SF building), and we do not know WHICH one is wrong — so we drop the one we are
    # less sure of. The building size is stated under a label; the space size was inferred.
    if (d.get("size_sf") and d.get("total_building_sf")
            and d["size_sf"] > d["total_building_sf"]):
        log.info("%s: %s SF space inside a %s SF building is impossible — dropping the size",
                 url, d["size_sf"], d["total_building_sf"])
        d.pop("size_sf", None)
        d.pop("divisible_min_sf", None)
        d.pop("divisible_max_sf", None)

    # An address alone is not a listing — the hard filter runs on SF and rent, and a page
    # that yields neither is a link directory entry, not supply.
    #
    # A BUILDING size counts, though. Blanca's towers publish a spec sheet and send you to a
    # third party for the suite-level availabilities, so we can honestly say "1450 Brickell,
    # a 625,800 SF Class A office tower in Brickell" and nothing about what's free inside it.
    # That is a real property a tenant searching "office in Brickell" should see, with a link
    # to the broker for the availabilities. Requiring a SUITE size dropped it on the floor.
    # It simply cannot match "~1,500 SF", which is correct: we don't know that it does.
    if not (d.get("size_sf") or d.get("asking_rent") or d.get("sale_price")
            or d.get("total_building_sf")):
        return None

    d = _clean(d, src, url, metro)
    if d:
        d["our_description"] = describe(d)      # our words, from the facts. Never the page's.
    return d


# --- rung 4: HTML + one LLM prompt (no per-site parsers) ----------------------

def from_html_llm(markdown: str, url: str, src: dict, metro: str) -> dict | None:
    """The one paid rung. Routed through `cache.cached()` — identical repeated pages
    (a re-crawl within the TTL, or two sources.yml entries hitting the same URL) never
    re-bill, and a paid call past `settings.monthly_budget_cents` raises `BudgetExceeded`
    instead of silently spending. Either that or any other parse/API failure degrades to
    "no listing from this page" — loudly logged, never a crash (ai.py's own pattern)."""
    if not ai.available():
        log.warning("HTML rung needs ANTHROPIC_API_KEY — skipping %s. (The wp-json and "
                    "JSON-LD rungs still work keyless.)", url)
        return None
    page_text = markdown[:20000]
    req = {"url": url, "markdown": page_text, "model": settings.llm_model}

    def fetch():
        resp = ai._client().messages.parse(
            model=settings.llm_model, max_tokens=2048,
            system=("Extract the ONE commercial space listed on this page into the schema. "
                    "Every field is required: use \"\" for text and 0 for numbers the page "
                    "does not state. Never invent a value.\n\n"
                    "our_description: write ONE original sentence describing the space FROM "
                    "THE FACTS (size, type, floor, location, features). Do NOT copy, quote, "
                    "or paraphrase the page's marketing copy — write your own.\n\n"
                    "features: SHORT FACTUAL TAGS ONLY — e.g. \"corner\", \"loading dock\", "
                    "\"ground floor\", \"corner unit\", \"elevator\". Two or three words each. "
                    "Do NOT copy the page's own bullet list, headlines, or selling phrases. "
                    "If a bullet reads like marketing (\"Incredible flagship opportunity!\"), "
                    "reduce it to the underlying fact or omit it entirely. We store facts, "
                    "never the broker's expression."),
            messages=[{"role": "user", "content": page_text}],
            output_format=ListingExtract,
        )
        return resp.parsed_output.model_dump()

    try:
        parsed = cache.cached("anthropic", "messages.parse.listing", req, fetch,
                               cost_cents=_HTML_LLM_COST_CENTS)
        return _clean(ListingExtract(**parsed).to_listing(), src, url, metro)
    except cache.BudgetExceeded as e:
        log.warning(
            "HTML+LLM rung SKIPPED for %s — monthly paid-spend cap reached (%s); this "
            "page will not be extracted until MONTHLY_BUDGET_CENTS is raised or the "
            "month rolls over. (The wp-json and JSON-LD rungs are unaffected — free and "
            "keyless.)", url, e,
        )
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("LLM extraction failed for %s (%s): %s", url, type(e).__name__, e)
        return None


def describe(d: dict) -> str:
    """One sentence, from the facts. Deterministic and keyless: this IS `our_description`
    for the wp-json and JSON-LD rungs — key or no key, there is no later LLM rewrite pass
    over an already-deterministic description. (The HTML+LLM rung is different: there,
    the LLM writes `our_description` itself, as part of `ListingExtract`, at extraction
    time — `describe()` is never called for that rung.) This exists so we NEVER need the
    broker's prose."""
    bits = []
    if d.get("size_sf"):
        bits.append(f"{d['size_sf']:,} SF")
    bits.append(d.get("property_type") or "commercial space")
    if d.get("floor"):
        bits.append(f"on floor {d['floor']}")
    if d.get("neighborhood"):
        bits.append(f"in {d['neighborhood']}")
    tail = ""
    if d.get("asking_rent"):
        # Say the unit the listing actually quotes. LA and industrial quote $/SF/MONTH:
        # rendering $3.20/SF/mo as "$3/SF/yr" is off by 12x AND rounds the cents away,
        # which reads as a plausible number and is simply false.
        rent, unit = d["asking_rent"], d.get("rent_unit") or "sf_yr"
        if unit == "sf_mo":
            tail = f", asking ${rent:,.2f}/SF/mo"
        elif unit == "mo":
            tail = f", asking ${rent:,.0f}/mo"
        else:
            tail = f", asking ${rent:,.2f}/SF/yr".replace(".00/", "/")
    elif d.get("sale_price"):
        tail = f", asking ${d['sale_price']:,}"
    sentence = " ".join(bits)
    # NOTE: str.capitalize() upper-cases the first char AND lower-cases every other
    # char — it would turn "2,100 SF ... Wicker Park" into "2,100 sf ... wicker park",
    # destroying the unit abbreviation and any proper noun. Upper-case only the first
    # character instead, and only if it's a letter (a leading digit, e.g. "2,100 SF...",
    # has no case to change).
    if sentence:
        sentence = sentence[0].upper() + sentence[1:]
    return f"{sentence} at {d['address']}{tail}."
