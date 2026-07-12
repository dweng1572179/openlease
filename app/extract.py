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
                    "or paraphrase the page's marketing copy — write your own."),
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
        tail = f", asking ${d['asking_rent']:,.0f}/SF/yr"
    sentence = " ".join(bits)
    # NOTE: str.capitalize() upper-cases the first char AND lower-cases every other
    # char — it would turn "2,100 SF ... Wicker Park" into "2,100 sf ... wicker park",
    # destroying the unit abbreviation and any proper noun. Upper-case only the first
    # character instead, and only if it's a letter (a leading digit, e.g. "2,100 SF...",
    # has no case to change).
    if sentence:
        sentence = sentence[0].upper() + sentence[1:]
    return f"{sentence} at {d['address']}{tail}."
