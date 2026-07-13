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
        return d


def _clean(d: dict, src: dict, url: str, metro: str) -> dict | None:
    if not d.get("address"):
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
    title = re.sub(r"<[^>]+>", "", title).strip()

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


def from_jsonld(html: str, url: str, src: dict, metro: str) -> dict | None:
    for blob in _LD.findall(html):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
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
            h = re.split(r"\s+[|–—]\s+", h)[0].strip()     # drop "| Site Name"
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
    r"(?:asking\s+rent|asking\s+price|rent|rate|price|ask)\s*[:\-–]?\s*"
    r"\$\s?([\d][\d,]*\.?\d*)\s*(?:/|\s+per\s+)?\s*"
    r"(sf|sq\.?\s?ft|square\s+foot|mo\b|month)?\s*/?\s*(yr|year|mo\b|month)?", re.I)
# "(?<!filter by )" — "Filter by size: 1,000 SF" is a DROPDOWN, not this listing's size.
_SIZE_LABEL = re.compile(
    r"(?<!filter by )(?:size|space available|availability|sf\s+available|square\s+footage"
    r"|divisible)\s*[:\-–]?\s*([\d][\d,]{1,8})\s*(?:\+/-\s*)?"
    r"(?:SF\b|sq\.?\s?ft|square\s+feet)", re.I)


def _size_of(text: str) -> int | None:
    """This listing's size. Prefers a LABELLED value ("Size: 3,305 SF"); otherwise the most
    REPEATED one — a listing page says its own size several times, while a filter dropdown
    says each of its options exactly once."""
    labelled = [_num(s) for s in _SIZE_LABEL.findall(text)]
    labelled = [s for s in labelled if _MIN_SF <= s <= _MAX_SF]
    if labelled:
        return int(labelled[0])

    from collections import Counter
    found = [_num(s) for s in _SIZE.findall(text)]
    found = [s for s in found if _MIN_SF <= s <= _MAX_SF]
    if not found:
        return None
    counts = Counter(found)
    top, n = counts.most_common(1)[0]
    if n < 2 and len(counts) > 3:
        return None          # many one-off numbers and nothing repeated: a dropdown, not a listing
    return int(top)


def _rent_of(text: str) -> tuple[float, str] | None:
    """This listing's ask, and the unit it is quoted in. LABELLED ONLY — see the note above.
    LA and industrial quote $/SF/MONTH; everyone else quotes $/SF/YEAR; some quote a gross
    monthly figure. The page says which."""
    for m in _RENT_LABEL.finditer(text):
        val = _num(m.group(1))
        per_sf = bool(m.group(2) and not re.fullmatch(r"mo\b|month", m.group(2), re.I))
        period = (m.group(3) or (m.group(2) if not per_sf else "") or "").lower()
        monthly = period.startswith("mo") or period.startswith("month")
        if per_sf:
            if monthly and _MIN_SF_MO <= val <= _MAX_SF_MO:
                return val, "sf_mo"
            if not monthly and _MIN_SF_YR <= val <= _MAX_SF_YR:
                return val, "sf_yr"
        elif monthly and val >= 500:            # a gross monthly rent
            return val, "mo"

    # No label. Accept an unlabelled rent ONLY when the page quotes exactly one — then it is
    # unambiguous. Metro Manhattan's pages quote four ($78.23 and $77.55 market averages,
    # $85.28, $320) and none of them is the listing's ask; a page with a single $/SF figure
    # is telling you the ask. More than one, and we do not guess.
    mo = [v for v in (_num(r) for r in _RENT_SF_MO.findall(text)) if _MIN_SF_MO <= v <= _MAX_SF_MO]
    yr = [v for v in (_num(r) for r in _RENT_SF.findall(text)) if _MIN_SF_YR <= v <= _MAX_SF_YR]
    if len(set(mo)) == 1:
        return mo[0], "sf_mo"
    if not mo and len(set(yr)) == 1:
        return yr[0], "sf_yr"
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

    size = _size_of(txt)
    if size:
        d["size_sf"] = size

    rent = _rent_of(txt)
    if rent:
        d["asking_rent"], d["rent_unit"] = rent

    # The type is the one the page TALKS about, not the first one it happens to mention.
    # Taking the first hit in _TYPES order typed every Metro Manhattan listing "retail" —
    # their pages say "office" twenty times and "retail" twice, and "retail" is first in
    # the tuple.
    counts = {t: len(re.findall(r"\b" + t + r"\b", txt, re.I)) for t in _TYPES}
    best = max(counts, key=lambda t: counts[t])
    if counts[best]:
        d["property_type"] = best

    if re.search(r"\bfor\s+sale\b", txt, re.I) and not d.get("asking_rent"):
        d["transaction_type"] = "sale"
        sale = [_num(s) for s in _SALE.findall(txt)]
        if sale:
            d["sale_price"] = int(min(sale))

    # An address alone is not a listing — the hard filter runs on SF and rent.
    if not (d.get("size_sf") or d.get("asking_rent") or d.get("sale_price")):
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
