"""AI layer — one BYO Anthropic key powers NL search, the conversational reply, LLM
extraction (crawl.py), listing descriptions, highlights, and per-listing chat.

With no key, `nl_to_query` falls back to a rules parser and `reply` to a deterministic
summary, so search still works — but the fallback understands FAR less of the query, so
it is LOUDLY logged. (A silent fallback hid a 400 for OpenProp's entire life and made
every AI search quietly drop half the user's constraints while still looking like it
worked.)

Both paid calls (`nl_to_query`'s `messages.parse`, `reply`'s `messages.create`) route
through `cache.cached()` — the only paid surfaces in the app, so this is the only place the
monthly budget cap (spec §6, §8) can be enforced. A refused-by-budget call is exactly
another degraded-mode fallback: it is LOUDLY logged, same as a parse failure."""
import logging
import re

from pydantic import BaseModel

from . import cache
from .config import settings
from .models import METROS, ListingQuery

log = logging.getLogger("openlease")

_TYPES = ("retail", "office", "industrial", "flex", "land")
_BBOX_FIELDS = ("min_lat", "max_lat", "min_lng", "max_lng")

# Anthropic pricing for the default `llm_model` (claude-opus-4-8): $5/1M input tokens,
# $25/1M output tokens (i.e. $0.0005c/input-tok, $0.0025c/output-tok).
#
# nl_to_query (messages.parse, max_tokens=1024): system prompt (~250 tok) + the QueryExtract
# schema definition sent with the request (~200 tok) + an optional prior-turn JSON blob on
# follow-ups (~100 tok) + the user's message (~50-150 tok) -> ~600-700 input tokens. The
# parsed-JSON output is normally 150-250 tokens (well under the 1024 cap).
#   ~700 * 0.0005c + ~250 * 0.0025c = 0.35c + 0.625c =~ 1c -> rounded up to 2c for headroom.
_PARSE_COST_CENTS = 2

# reply (messages.create, max_tokens=600): system prompt (~150 tok) + up to 8 listings'
# facts (~150 tok) + the user's message (~50 tok) -> ~350 input tokens. The reply is 2-3
# sentences plus 3 suggestions, normally 150-300 tokens (well under the 600 cap).
#   ~350 * 0.0005c + ~250 * 0.0025c = 0.175c + 0.625c =~ 1c -> rounded up to 2c for headroom.
_REPLY_COST_CENTS = 2


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

    def to_query(self, *, default_transaction_type: str = "lease") -> ListingQuery:
        """Sentinels -> absent, so unmentioned fields never become real filters.

        Two fields get special handling instead of the flat per-field drop below:

        - The four bbox fields are ATOMIC: a real bounding box needs all four corners, so if
          even one is still at its 0/0.0 sentinel the WHOLE group is dropped. A partial bbox
          (e.g. a real minLat/maxLat/maxLng next to a sentinel minLng=0) is a geographically
          nonsensical filter that looks like it worked — worse than no bbox at all.
        - transaction_type resolves to `default_transaction_type` here rather than being
          dropped by the flat filter below. `nl_to_query` passes "" (not "lease") whenever
          there's a PRIOR turn to merge against, so the "unstated" sentinel survives into
          `_merge()` instead of being baked into a concrete "lease" before `_merge()` can
          tell "the user restated lease" apart from "the user didn't mention it" — which
          would otherwise silently flip a prior 'sale' search back to 'lease' on any
          follow-up that doesn't repeat the word.
        """
        dumped = self.model_dump()
        has_full_bbox = all(dumped[f] not in (0, 0.0) for f in _BBOX_FIELDS)
        # NOTE: `v not in (...)` uses `==`, and `False == 0` is True in Python — a bool field
        # added to this schema later would be silently dropped whenever it's False. No bool
        # fields exist today; if one is added, guard this with `type(v) is not bool and ...`.
        d = {
            k: v for k, v in dumped.items()
            if k not in _BBOX_FIELDS and k != "transaction_type"
            and v not in ("", 0, 0.0, [])
        }
        if has_full_bbox:
            d.update({f: dumped[f] for f in _BBOX_FIELDS})
        d["transaction_type"] = self.transaction_type or default_transaction_type
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
    bigger, drop the rent cap' has to know what 'it' was.

    The `messages.parse` call is the paid step, so it goes through `cache.cached()` —
    identical repeated queries never re-bill, and a paid call past the monthly cap raises
    `BudgetExceeded` instead of silently spending. Either that or any other parse/API
    failure degrades to the rules parser, loudly logged (never silently)."""
    prior = ListingQuery(**prior_state) if prior_state else None
    if prior:
        prior = _drop_foreign_geo(prior, metro)
    # A fresh, no-prior search resolves an unstated transactionType to "lease" right here
    # (SpaceFinder's own default). A follow-up turn instead passes "" through unresolved, so
    # _merge() below can tell "the new turn restated lease" apart from "the new turn didn't
    # mention it" and keep the prior turn's own transaction_type (e.g. "sale") intact.
    default_txn = "" if prior else "lease"
    if not available():
        q = _rules_parse(message, metro, default_transaction_type=default_txn)
        return _merge(prior, q) if prior else q
    m = METROS.get(metro, {})
    req = {
        "message": message,
        "metro": metro,
        "prior": prior.model_dump(by_alias=True) if prior else None,
        "model": settings.llm_model,
    }

    def fetch():
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
        return resp.parsed_output.model_dump()

    try:
        extracted = cache.cached("anthropic", "messages.parse", req, fetch, cost_cents=_PARSE_COST_CENTS)
        q = QueryExtract(**extracted).to_query(default_transaction_type=default_txn)
        return _merge(prior, q) if prior else q
    except cache.BudgetExceeded as e:
        log.warning(
            "AI query extraction skipped — monthly paid-spend cap reached (%s); falling "
            "back to the rules parser, which understands far less of the query.", e
        )
    except Exception as e:  # noqa: BLE001 — any parse/API failure degrades to rules
        log.warning(
            "AI query extraction failed (%s) — falling back to the rules parser, which "
            "understands far less of the query: %s", type(e).__name__, e
        )
    q = _rules_parse(message, metro, default_transaction_type=default_txn)
    return _merge(prior, q) if prior else q


def _drop_foreign_geo(prior: ListingQuery, metro: str) -> ListingQuery:
    """Defensive guard, independent of the UI's own metro-switch reset: a prior turn's
    neighborhood/bbox/boroughs were resolved against ITS metro's geography (a named
    neighborhood sets both `neighborhood` AND a full 4-corner bbox, per `_SYSTEM` above).
    If `metro` is now something else — the UI failed to reset session_id/prior_state, or
    a non-UI API client just never bothered to — blindly merging that geography in would
    silently intersect one city's coordinates against another's listings, a combination
    `filter_listings` can never satisfy. Worse, `_relax`'s "neighborhood" stage (see
    routes_search.py) clears `neighborhood`/`boroughs` but never the bbox, so the ladder
    can't rescue it either: every subsequent turn in the session would return zero rows,
    forever, with a message that hides the real cause. Drop the geography wholesale
    instead of merging it in as if it still applied."""
    meta = METROS.get(metro, {})
    bbox = meta.get("bbox")   # [min_lat, min_lng, max_lat, max_lng], per metros.yml
    has_bbox = all([prior.min_lat, prior.max_lat, prior.min_lng, prior.max_lng])
    bbox_is_foreign = has_bbox and bbox and not (
        bbox[0] <= prior.min_lat <= bbox[2] and bbox[0] <= prior.max_lat <= bbox[2] and
        bbox[1] <= prior.min_lng <= bbox[3] and bbox[1] <= prior.max_lng <= bbox[3]
    )
    boroughs_are_foreign = bool(prior.boroughs) and not any(
        b in meta.get("boroughs", []) for b in prior.boroughs
    )
    if not (bbox_is_foreign or boroughs_are_foreign):
        return prior
    return prior.model_copy(update={
        "min_lat": 0.0, "max_lat": 0.0, "min_lng": 0.0, "max_lng": 0.0,
        "boroughs": [], "neighborhood": "",
    })


def _merge(prior: ListingQuery, new: ListingQuery) -> ListingQuery:
    """Follow-up refinement: the new turn's stated fields win; unstated fields keep the
    prior turn's value. Sentinels mean 'unstated' for every field, including
    transaction_type — nl_to_query passes default_transaction_type="" (instead of resolving
    to "lease" before this runs) specifically so this dict update sees a real sentinel here
    too, not a concrete "lease" that would silently overwrite a prior 'sale' search."""
    base = prior.model_dump()
    for k, v in new.model_dump().items():
        if v not in ("", 0, 0.0, []):
            base[k] = v
    return ListingQuery(**base)


def _rules_parse(message: str, metro: str, *, default_transaction_type: str = "lease") -> ListingQuery:
    """Keyword fallback — covers the common tenant-rep phrasings. Deliberately dumb.

    `default_transaction_type` is what "the message didn't mention it" resolves to. A fresh
    search (no prior turn — see nl_to_query) defaults to "lease". A follow-up turn passes ""
    instead, so the unstated sentinel survives into `_merge()` rather than silently flipping
    a prior 'sale' search back to 'lease'."""
    q = message.lower()
    out = ListingQuery(transaction_type=default_transaction_type)
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

def reply(message: str, q: ListingQuery, results: list[dict], is_near_miss: bool,
          relaxed_what: str = "") -> tuple[str, list[str]]:
    """(reply, suggestions). Keyless: a deterministic summary. Keyed: the LLM writes it.

    `reply` is the ONE place the near-miss sentence gets composed — it is part of the
    JSON API contract (`POST /api/search`), read by non-UI clients too, so it must be
    SELF-CONTAINED: a caller reading only `reply` still has to learn a search was a near
    miss and exactly which of their stated constraints were dropped (a near-miss result
    VIOLATES something the user asked for, e.g. a $95/SF listing against a $64/SF cap —
    silently handing that back is worse than returning nothing). `routes_search.py` used
    to prepend this same sentence again on top of what this function already said, and
    the HTML banner said it a third time — so this function says it exactly once, and
    every other layer either stops repeating it (routes_search.py) or shrinks to a
    non-repeating label (the `_results.html` banner)."""
    if not results:
        return ("Nothing matches those constraints in this market yet. Try widening the "
                "size range or the rent cap.",
                ["Widen the size range", "Raise the rent cap", "Try a nearby neighborhood"])
    if not available():
        head = results[0]
        if is_near_miss:
            near = (f"Nothing matched exactly — I relaxed {relaxed_what}. "
                     if relaxed_what else "Nothing matched exactly, so here are the closest misses. ")
        else:
            near = ""
        return (f"{near}{len(results)} match{'es' if len(results) != 1 else ''}. "
                f"The closest is {head.get('address')} — {head.get('rationale', '')}.",
                ["Show only ground floor", "Raise the size cap", "Drop the rent cap"])
    facts = "\n".join(
        f"- {r.get('address')} | {r.get('sizeSf')} SF | {r.get('propertyType')} | "
        f"${r.get('askingRent')} {r.get('rentUnit')} | {r.get('rationale')}"
        for r in results[:8]
    )
    req = {
        "message": message, "is_near_miss": is_near_miss, "relaxed_what": relaxed_what,
        "facts": facts, "model": settings.llm_model,
    }

    def fetch():
        resp = _client().messages.create(
            model=settings.llm_model, max_tokens=600,
            system=("You are a commercial leasing broker replying to a tenant rep. In 2-3 "
                    "sentences, summarize what these listings offer against what they asked "
                    "for, and call out the single best fit by address. If isNearMiss is true, "
                    "open by saying plainly that nothing matched exactly and name exactly "
                    "what was relaxed (given below as relaxedWhat) — say it only ONCE, don't "
                    "repeat the disclosure later in the reply. Then give exactly 3 short "
                    "follow-up refinements, one per line, prefixed '- '. No preamble, no "
                    "markdown headers."),
            messages=[{"role": "user", "content":
                       f"They asked: {message}\nisNearMiss: {is_near_miss}\n"
                       f"relaxedWhat: {relaxed_what}\nMatches:\n{facts}"}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        lines = [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")]
        body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("- ")).strip()
        return {"body": body, "lines": lines[:3]}

    try:
        out = cache.cached("anthropic", "messages.create", req, fetch, cost_cents=_REPLY_COST_CENTS)
        return out["body"], out["lines"]
    except cache.BudgetExceeded as e:
        log.warning(
            "AI reply skipped — monthly paid-spend cap reached (%s); returning the "
            "deterministic summary.", e
        )
    except Exception as e:  # noqa: BLE001
        log.warning("AI reply failed (%s) — returning the deterministic summary: %s",
                    type(e).__name__, e)
    settings_backup = settings.anthropic_api_key
    try:
        settings.anthropic_api_key = ""      # force the keyless branch, once
        return reply(message, q, results, is_near_miss, relaxed_what)
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
