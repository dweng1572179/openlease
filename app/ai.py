"""AI layer — one BYO Anthropic key powers NL search, the conversational reply, LLM
extraction (crawl.py), listing descriptions, highlights, and per-listing chat.

With no key, `nl_to_query` falls back to a rules parser and `reply` to a deterministic
summary, so search still works — but the fallback understands FAR less of the query, so
it is LOUDLY logged. (A silent fallback hid a 400 for OpenProp's entire life and made
every AI search quietly drop half the user's constraints while still looking like it
worked.)"""
import logging
import re

from pydantic import BaseModel

from .config import settings
from .models import METROS, ListingQuery

log = logging.getLogger("openlease")

_TYPES = ("retail", "office", "industrial", "flex", "land")


class QueryExtract(BaseModel):
    """The `messages.parse()` schema. Two rules are load-bearing, not style:

    1. NO `| None`. Structured outputs reject >16 union-typed (nullable) params
       ("too many parameters with union types ... limit: 16"). These fields as `X | None`
       are a hard 400.
    2. NO DEFAULTS — every field REQUIRED. A default makes a field *optional* in the JSON
       schema, and each optional field is a present/absent branch: N of them is 2^N shapes
       for the grammar compiler. The request doesn't 400 — it HANGS (>75s, times out) on
       every model, Haiku included. All-required = one shape = seconds.

    So: sentinels, not nulls. "" / 0 / [] mean "the query did not mention this," and are
    dropped in to_query() before they can become filters."""
    property_types: list[str]
    transaction_type: str          # "lease" | "sale" | "" (unmentioned -> lease)
    boroughs: list[str]
    neighborhood: str
    min_size_sf: int
    max_size_sf: int
    max_rent_per_sf_yr: float
    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float
    exclude_addr_states: list[str]
    exclude_zip3: list[str]
    exclude_cities: list[str]
    keywords: list[str]            # free-text terms for BM25 (e.g. "corner", "loading dock")

    def to_query(self) -> ListingQuery:
        """Sentinels -> absent, so unmentioned fields never become real filters."""
        d = {k: v for k, v in self.model_dump().items() if v not in ("", 0, 0.0, [])}
        d.setdefault("transaction_type", "lease")
        return ListingQuery(**d)


def _client():
    import anthropic
    # bounded: the SDK default is 10min, so a bad schema/outage would freeze the request
    # instead of degrading to the rules parser.
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0)


def available() -> bool:
    return bool(settings.anthropic_api_key)


# --- NL -> ListingQuery -------------------------------------------------------

_SYSTEM = """Convert a commercial-real-estate tenant's plain-English space search into
structured filters for the {metro_name} market.

EVERY field is required. Use "" for text, 0 for numbers, [] for lists the query does not
mention. Never invent a constraint the user did not state.

Rules:
- propertyTypes from: retail, office, industrial, flex, land.
- transactionType is "sale" only if the user is buying; otherwise "lease".
- Rent is normalized to DOLLARS PER SF PER YEAR. Convert: a monthly total budget divided
  by the size, times 12. "under $8k/mo for ~1,500 SF" -> 8000 * 12 / 1500 = 64.
  A monthly per-SF rate ("$6/SF/mo") x 12. If no size is given, leave maxRentPerSfYr 0.
- A named neighborhood goes in `neighborhood` AND its bounding box in min/max lat/lng.
  Use the metro's own geography; the metro bbox is {bbox}.
- excludeCities: when the user names a city/neighborhood inside this metro, list the
  suburbs that would otherwise leak in.
- keywords: the qualitative terms worth text-matching ("corner", "high ceilings",
  "loading dock", "second generation"). Not the numbers — those are filters.
"""


def nl_to_query(message: str, prior_state: dict | None, metro: str) -> ListingQuery:
    """Parse `message` into filters. `prior_state` carries the PRIOR turn's mustHaves
    (camelCase, off the wire) so a follow-up refines instead of restarting: 'make it
    bigger, drop the rent cap' has to know what 'it' was."""
    prior = ListingQuery(**prior_state) if prior_state else None
    if not available():
        q = _rules_parse(message, metro)
        return _merge(prior, q) if prior else q
    m = METROS.get(metro, {})
    try:
        resp = _client().messages.parse(
            model=settings.llm_model, max_tokens=1024,
            system=_SYSTEM.format(metro_name=m.get("name", metro), bbox=m.get("bbox")),
            messages=[
                *([{"role": "user", "content": f"Prior search: {prior.model_dump_json(by_alias=True)}. "
                                               f"Refine it with the next message."}] if prior else []),
                {"role": "user", "content": message},
            ],
            output_format=QueryExtract,
        )
        q = resp.parsed_output.to_query()
        return _merge(prior, q) if prior else q
    except Exception as e:  # noqa: BLE001 — any parse/API failure degrades to rules
        log.warning(
            "AI query extraction failed (%s) — falling back to the rules parser, which "
            "understands far less of the query: %s", type(e).__name__, e
        )
        q = _rules_parse(message, metro)
        return _merge(prior, q) if prior else q


def _merge(prior: ListingQuery, new: ListingQuery) -> ListingQuery:
    """Follow-up refinement: the new turn's stated fields win; unstated fields keep the
    prior turn's value. (Sentinels already mean 'unstated', so this is a dict update.)"""
    base = prior.model_dump()
    for k, v in new.model_dump().items():
        if v not in ("", 0, 0.0, []) or (k == "transaction_type" and v):
            base[k] = v
    return ListingQuery(**base)


def _rules_parse(message: str, metro: str) -> ListingQuery:
    """Keyword fallback — covers the common tenant-rep phrasings. Deliberately dumb."""
    q = message.lower()
    out = ListingQuery()
    out.property_types = [t for t in _TYPES if t in q]
    if "for sale" in q or "buy" in q or "purchase" in q:
        out.transaction_type = "sale"
    # size_hint is the user's actual stated number (e.g. the 1,500 in "~1,500 SF"). It's
    # kept separate from out.min/max_size_sf because the "~" branch below WIDENS those
    # into a range (1125/1875) for filtering purposes — but the rent-per-SF conversion
    # two blocks down needs the original figure the user typed, not the widened range,
    # or "$8k/mo for ~1,500 SF" silently converts against 1875 and comes out 51.2
    # instead of the correct 64.
    size_hint = 0
    if m := re.search(r"(?:under|below|less than|max|up to)\s*([\d,]+)\s*(?:sf|sq|square)", q):
        out.max_size_sf = int(m.group(1).replace(",", ""))
        size_hint = out.max_size_sf
    if m := re.search(r"(?:over|above|at least|min|minimum)\s*([\d,]+)\s*(?:sf|sq|square)", q):
        out.min_size_sf = int(m.group(1).replace(",", ""))
        size_hint = size_hint or out.min_size_sf
    if not out.min_size_sf and not out.max_size_sf:
        if m := re.search(r"([\d,]{3,})\s*(?:sf|sq ?ft|square feet)", q):   # "~1,500 SF"
            size_hint = int(m.group(1).replace(",", ""))
            out.min_size_sf, out.max_size_sf = int(size_hint * 0.75), int(size_hint * 1.25)
    # "$8k/mo" or "$8,000 a month" -> per-SF-per-year, but ONLY if we know the size
    if m := re.search(r"\$\s*([\d,.]+)\s*(k)?\s*(?:/|per |a )\s*mo", q):
        monthly = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
        if size_hint:
            out.max_rent_per_sf_yr = round(monthly * 12 / size_hint, 2)
    elif m := re.search(r"\$\s*([\d,.]+)\s*(?:/|per )\s*(?:sf|psf)", q):
        out.max_rent_per_sf_yr = float(m.group(1).replace(",", ""))
    for hood in METROS.get(metro, {}).get("boroughs", []):
        if hood.lower() in q:
            out.boroughs = [hood]
            break
    # every noun the filters didn't consume is a text-match candidate
    stop = {"in", "a", "an", "the", "for", "with", "under", "over", "sf", "space", "need",
            "looking", "want", "around", "near", "about", "at", "to", "of", "and", "or"}
    out.keywords = [w for w in re.findall(r"[a-z][a-z-]{2,}", q) if w not in stop][:8]
    return out


# --- conversational reply -----------------------------------------------------

def reply(message: str, q: ListingQuery, results: list[dict], is_near_miss: bool) -> tuple[str, list[str]]:
    """(reply, suggestions). Keyless: a deterministic summary. Keyed: the LLM writes it."""
    if not results:
        return ("Nothing matches those constraints in this market yet. Try widening the "
                "size range or the rent cap.",
                ["Widen the size range", "Raise the rent cap", "Try a nearby neighborhood"])
    if not available():
        head = results[0]
        near = "Nothing matched exactly, so here are the closest misses. " if is_near_miss else ""
        return (f"{near}{len(results)} match{'es' if len(results) != 1 else ''}. "
                f"The closest is {head.get('address')} — {head.get('rationale', '')}.",
                ["Show only ground floor", "Raise the size cap", "Drop the rent cap"])
    facts = "\n".join(
        f"- {r.get('address')} | {r.get('sizeSf')} SF | {r.get('propertyType')} | "
        f"${r.get('askingRent')} {r.get('rentUnit')} | {r.get('rationale')}"
        for r in results[:8]
    )
    try:
        resp = _client().messages.create(
            model=settings.llm_model, max_tokens=600,
            system=("You are a commercial leasing broker replying to a tenant rep. In 2-3 "
                    "sentences, summarize what these listings offer against what they asked "
                    "for, and call out the single best fit by address. If isNearMiss is true, "
                    "say plainly that nothing matched exactly and what was relaxed. Then give "
                    "exactly 3 short follow-up refinements, one per line, prefixed '- '. "
                    "No preamble, no markdown headers."),
            messages=[{"role": "user", "content":
                       f"They asked: {message}\nisNearMiss: {is_near_miss}\nMatches:\n{facts}"}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        lines = [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")]
        body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("- ")).strip()
        return body, lines[:3]
    except Exception as e:  # noqa: BLE001
        log.warning("AI reply failed (%s) — returning the deterministic summary: %s",
                    type(e).__name__, e)
        settings_backup = settings.anthropic_api_key
        try:
            settings.anthropic_api_key = ""      # force the keyless branch, once
            return reply(message, q, results, is_near_miss)
        finally:
            settings.anthropic_api_key = settings_backup


def demo() -> None:
    q = _rules_parse("retail in Wynwood ~1,500 SF under $8k/mo", "mia")
    assert q.property_types == ["retail"], q
    assert q.min_size_sf == 1125 and q.max_size_sf == 1875, q
    assert q.max_rent_per_sf_yr == 64.0, q.max_rent_per_sf_yr   # 8000*12/1500
    assert "wynwood" in " ".join(q.keywords), q.keywords

    # the schema rules that cost OpenProp its whole first life — enforced, not remembered
    for name, f in QueryExtract.model_fields.items():
        assert f.is_required(), f"{name} has a default -> optional param -> the request HANGS"
        assert "NoneType" not in str(f.annotation), f"{name} is nullable -> union-param 400"
    print("ai.demo (rules fallback + schema guards) OK")


if __name__ == "__main__":
    demo()
