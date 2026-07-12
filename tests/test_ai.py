"""The rules fallback, the unit conversion, and the two schema rules. The schema
assertions are not paranoia: either mistake makes `messages.parse` 400 or HANG, the
caller silently falls back, and the AI search drops constraints the user typed.

Also covers the review-pass fixes on top of the original Task 4 implementation:
- the monthly budget cap actually gates the one paid surface, and a refused-by-budget
  call falls back loudly instead of crashing the search;
- a follow-up turn never silently flips a prior 'sale' search back to 'lease';
- a partial bbox is dropped as a whole atomic group instead of leaking through half-formed;
- the sentinel-drop test actually has detection power (the original version passed
  identically whether or not the sentinel-drop logic ran at all).
"""
import logging
import os

os.environ["ANTHROPIC_API_KEY"] = ""   # every test here runs the keyless path

from app import ai, db  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import ListingQuery  # noqa: E402


def test_schema_is_all_required_and_non_nullable():
    for name, f in ai.QueryExtract.model_fields.items():
        assert f.is_required(), f"{name} has a default -> optional param -> request HANGS"
        assert "NoneType" not in str(f.annotation), f"{name} is nullable -> union-param 400"


def test_sentinels_never_become_filters():
    """A realistic mix of stated and unstated fields. The original version of this test held
    identically whether or not to_query()'s sentinel-drop logic ran at all, because
    ListingQuery's own class defaults happen to equal the sentinel values for every field
    except transaction_type. This version adds real, non-default values (and a partial bbox)
    so the assertions have actual detection power -- confirmed by temporarily reverting
    to_query() to a bare `return ListingQuery(**self.model_dump())` passthrough and
    re-running: transaction_type comes back "" (not "lease") and the bbox comes back
    partially populated (not all-zero) -- this test fails either way with that reverted."""
    q = ai.QueryExtract(
        property_types=["retail"], transaction_type="", boroughs=[], neighborhood="Wynwood",
        min_size_sf=1000, max_size_sf=0, max_rent_per_sf_yr=64.0,
        min_lat=25.7, max_lat=25.8, min_lng=0, max_lng=-80.1,   # partial bbox: min_lng unstated
        exclude_addr_states=[], exclude_zip3=[], exclude_cities=["Hialeah"],
        keywords=["corner"],
    ).to_query()
    # real, stated values survive as real filters
    assert q.property_types == ["retail"]
    assert q.neighborhood == "Wynwood"
    assert q.min_size_sf == 1000
    assert q.max_rent_per_sf_yr == 64.0
    assert q.exclude_cities == ["Hialeah"]
    assert q.keywords == ["corner"]
    # unstated sentinels never become filters
    assert q.max_size_sf == 0
    assert q.transaction_type == "lease"      # the one sentinel with a real, non-empty default
    # the bbox is ATOMIC: 3 real corners + 1 sentinel corner drops the WHOLE group
    assert (q.min_lat, q.max_lat, q.min_lng, q.max_lng) == (0, 0, 0, 0)


def test_full_bbox_survives_when_all_four_corners_are_stated():
    """The bbox-atomicity fix must not zero out a genuinely complete bbox."""
    q = ai.QueryExtract(
        property_types=[], transaction_type="", boroughs=[], neighborhood="Wynwood",
        min_size_sf=0, max_size_sf=0, max_rent_per_sf_yr=0,
        min_lat=25.7, max_lat=25.8, min_lng=-80.2, max_lng=-80.1,
        exclude_addr_states=[], exclude_zip3=[], exclude_cities=[], keywords=[],
    ).to_query()
    assert (q.min_lat, q.max_lat, q.min_lng, q.max_lng) == (25.7, 25.8, -80.2, -80.1)


def test_rules_parse_monthly_budget_to_rent_per_sf_yr():
    q = ai._rules_parse("retail in Wynwood ~1,500 SF under $8k/mo", "mia")
    assert q.property_types == ["retail"]
    assert q.min_size_sf == 1125 and q.max_size_sf == 1875
    assert q.max_rent_per_sf_yr == 64.0          # 8000 * 12 / 1500
    assert q.transaction_type == "lease"


def test_rules_parse_sale_and_psf():
    q = ai._rules_parse("industrial building for sale under $30/sf", "chi")
    assert q.transaction_type == "sale" and q.max_rent_per_sf_yr == 30.0


def test_follow_up_refines_instead_of_restarting():
    prior = ListingQuery(property_types=["retail"], min_size_sf=1000, max_size_sf=2000,
                         max_rent_per_sf_yr=64.0)
    q = ai.nl_to_query("make it bigger — at least 5,000 sf", prior.model_dump(by_alias=True), "mia")
    assert q.min_size_sf == 5000              # the new constraint won
    assert q.property_types == ["retail"]     # the unstated one survived
    assert q.max_rent_per_sf_yr == 64.0


def test_follow_up_never_flips_a_sale_search_back_to_lease():
    """Reproduces the bug: a 'for sale' search, refined by a message that never mentions
    sale or lease, must NOT come back as 'lease'. Fails without the to_query()/
    _rules_parse()/_merge() fix -- the pre-fix code always came back "lease" here, because
    both to_query()'s setdefault and _rules_parse()'s ListingQuery() default resolved the ""
    sentinel to the concrete "lease" BEFORE _merge() ever ran, so _merge() had no way to
    tell "the user restated lease" apart from "the user didn't mention it"."""
    prior = ListingQuery(transaction_type="sale", property_types=["industrial"],
                         min_size_sf=10000, max_size_sf=20000)
    q = ai.nl_to_query("make it bigger, at least 25000 sf", prior.model_dump(by_alias=True), "mia")
    assert q.transaction_type == "sale"       # must survive the follow-up untouched
    assert q.min_size_sf == 25000             # the new constraint still won


def test_keyless_reply_is_deterministic_and_suggests():
    text, suggestions = ai.reply(
        "retail in wynwood", ListingQuery(),
        [{"address": "2618 NW 2nd Ave", "rationale": "1,500 SF retail in Wynwood"}], False,
    )
    assert "2618 NW 2nd Ave" in text and len(suggestions) == 3
    text, suggestions = ai.reply("x", ListingQuery(), [], False)
    assert "Nothing matches" in text and len(suggestions) == 3


# --- the monthly budget cap: paid calls route through cache.cached() ---------------------

def test_repeated_query_hits_cache_and_never_rebills(monkeypatch, tmp_path):
    """Never pay twice: an identical repeated query must not re-invoke the paid client."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_cache_hit.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 1000)
    calls = []

    class _FakeParsed:
        def model_dump(self):
            return {
                "property_types": ["retail"], "transaction_type": "", "boroughs": [],
                "neighborhood": "", "min_size_sf": 0, "max_size_sf": 0,
                "max_rent_per_sf_yr": 0, "min_lat": 0, "max_lat": 0, "min_lng": 0,
                "max_lng": 0, "exclude_addr_states": [], "exclude_zip3": [],
                "exclude_cities": [], "keywords": ["retail"],
            }

    class _FakeResp:
        parsed_output = _FakeParsed()

    class _FakeMessages:
        def parse(self, **kwargs):
            calls.append(1)
            return _FakeResp()

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(ai, "_client", lambda: _FakeClient())

    ai.nl_to_query("retail space in wynwood", None, "mia")
    ai.nl_to_query("retail space in wynwood", None, "mia")   # identical query -> cache hit

    assert len(calls) == 1, "the second, identical query must be a cache hit, not a re-fetch"


def test_budget_exceeded_falls_back_to_rules_parser_and_logs_loudly(monkeypatch, tmp_path, caplog):
    """A paid call refused by the monthly budget must fall back to the rules parser (not
    crash the search) and must log LOUDLY at WARNING naming the budget as the reason --
    same as every other fallback (a silent fallback hid a 400 for OpenProp's entire life)."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_budget.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 0)   # nothing left this month

    def _must_not_be_called():
        # deliberately avoids the word "budget" in this message -- the test's assertion
        # greps caplog for "budget", and that word must come from the real BudgetExceeded
        # path (cache.py's message names MONTHLY_BUDGET_CENTS), not leak in as a false
        # positive via this mock's own text if the code under test skips the cache/budget
        # check entirely and calls the client directly (the pre-fix bug).
        raise AssertionError("the Anthropic client must not run when there is nothing left to spend")
    monkeypatch.setattr(ai, "_client", _must_not_be_called)

    with caplog.at_level(logging.WARNING, logger="openlease"):
        q = ai.nl_to_query("retail space in wynwood", None, "mia")

    assert "budget" in caplog.text.lower()
    assert q.property_types == ["retail"]      # the rules parser still ran and understood this


def test_reply_budget_exceeded_falls_back_to_deterministic_summary(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_reply_budget.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 0)

    def _must_not_be_called():
        # see the matching comment in test_budget_exceeded_falls_back_to_rules_parser_and_
        # logs_loudly -- deliberately avoids the word "budget" so the assertion below can
        # only pass via the real BudgetExceeded message, not a coincidental text match.
        raise AssertionError("the Anthropic client must not run when there is nothing left to spend")
    monkeypatch.setattr(ai, "_client", _must_not_be_called)

    with caplog.at_level(logging.WARNING, logger="openlease"):
        text, suggestions = ai.reply(
            "retail in wynwood", ListingQuery(),
            [{"address": "2618 NW 2nd Ave", "rationale": "1,500 SF retail in Wynwood"}], False,
        )

    assert "budget" in caplog.text.lower()
    assert "2618 NW 2nd Ave" in text and len(suggestions) == 3


# --- Task 13: per-listing highlights + RAG chat -- same cache.cached()/budget pattern ----

_LISTING = {
    "id": 7, "address": "1 Main St, Miami, FL", "neighborhood": "Wynwood",
    "property_type": "retail", "transaction_type": "lease", "size_sf": 2400,
    "ceiling_height_ft": 14.0, "asking_rent": 95.0, "rent_unit": "sf_yr",
    "lease_type": "NNN", "broker_firm": "Demo Realty",
    "our_description": "Corner retail with 40 feet of frontage.",
}


def test_highlights_keyless_returns_none_never_invents_text():
    assert ai.highlights(_LISTING) is None


def test_ask_keyless_names_the_key_and_never_fakes_an_answer():
    out = ai.ask(_LISTING, "what's the ceiling height?", [])
    assert "ANTHROPIC_API_KEY" in out


def test_highlights_budget_exceeded_falls_back_to_none_and_logs_loudly(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_highlights_budget.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 0)   # nothing left this month

    def _must_not_be_called():
        raise AssertionError("the Anthropic client must not run when there is nothing left to spend")
    monkeypatch.setattr(ai, "_client", _must_not_be_called)

    with caplog.at_level(logging.WARNING, logger="openlease"):
        out = ai.highlights(_LISTING)

    assert out is None
    assert "budget" in caplog.text.lower()


def test_ask_budget_exceeded_falls_back_honestly_and_logs_loudly(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_ask_budget.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 0)

    def _must_not_be_called():
        raise AssertionError("the Anthropic client must not run when there is nothing left to spend")
    monkeypatch.setattr(ai, "_client", _must_not_be_called)

    with caplog.at_level(logging.WARNING, logger="openlease"):
        out = ai.ask(_LISTING, "what's the ceiling height?", [])

    assert "budget" in out.lower()
    assert "budget" in caplog.text.lower()


def test_highlights_grounded_in_our_facts_and_parses_bullets(monkeypatch, tmp_path):
    """A fake keyed client stands in for Anthropic (hermetic -- no live network calls).
    Confirms highlights() feeds the model _listing_facts() (never a broker-prose column --
    there isn't one, by design) and parses '- '-prefixed bullets back into a list."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_highlights_ok.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 1000)
    seen = {}

    class _Block:
        type = "text"
        text = "- 2,400 SF corner retail\n- 14 ft ceilings\n- NNN lease\nSome preamble line"

    class _Resp:
        content = [_Block()]

    class _FakeMessages:
        def create(self, **kwargs):
            seen["messages"] = kwargs["messages"]
            return _Resp()

    monkeypatch.setattr(ai, "_client", lambda: type("C", (), {"messages": _FakeMessages()})())

    out = ai.highlights(_LISTING)
    assert out == ["2,400 SF corner retail", "14 ft ceilings", "NNN lease"]
    grounding = seen["messages"][0]["content"]
    assert "1 Main St" in grounding and "Wynwood" in grounding


def test_ask_is_grounded_in_the_record(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_ask_ok.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 1000)
    seen = {}

    class _Block:
        type = "text"
        text = "The ceiling height is 14 ft, per the listing record."

    class _Resp:
        content = [_Block()]

    class _FakeMessages:
        def create(self, **kwargs):
            seen["system"] = kwargs["system"]
            seen["messages"] = kwargs["messages"]
            return _Resp()

    monkeypatch.setattr(ai, "_client", lambda: type("C", (), {"messages": _FakeMessages()})())

    history = [{"role": "user", "content": "any parking?"},
              {"role": "assistant", "content": "Not published in this record."}]
    out = ai.ask(_LISTING, "what's the ceiling height?", history)
    assert "14 ft" in out
    assert "1 Main St" in seen["system"]                     # the record is IN the prompt
    assert seen["messages"][0] == history[0]                 # prior turns replay in order
    assert seen["messages"][-1] == {"role": "user", "content": "what's the ceiling height?"}


def test_ask_repeated_identical_question_hits_cache_and_never_rebills(monkeypatch, tmp_path):
    """Same cache-through discipline as nl_to_query/reply: an identical repeated question
    (same history) must not re-invoke the paid client a second time."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_ask_cache.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 1000)
    calls = []

    class _Block:
        type = "text"
        text = "14 ft."

    class _Resp:
        content = [_Block()]

    class _FakeMessages:
        def create(self, **kwargs):
            calls.append(1)
            return _Resp()

    monkeypatch.setattr(ai, "_client", lambda: type("C", (), {"messages": _FakeMessages()})())

    ai.ask(_LISTING, "what's the ceiling height?", [])
    ai.ask(_LISTING, "what's the ceiling height?", [])
    assert len(calls) == 1
