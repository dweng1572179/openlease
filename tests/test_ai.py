"""The rules fallback, the unit conversion, and the two schema rules. The schema
assertions are not paranoia: either mistake makes `messages.parse` 400 or HANG, the
caller silently falls back, and the AI search drops constraints the user typed."""
import os

os.environ["ANTHROPIC_API_KEY"] = ""   # every test here runs the keyless path

from app import ai  # noqa: E402
from app.models import ListingQuery  # noqa: E402


def test_schema_is_all_required_and_non_nullable():
    for name, f in ai.QueryExtract.model_fields.items():
        assert f.is_required(), f"{name} has a default -> optional param -> request HANGS"
        assert "NoneType" not in str(f.annotation), f"{name} is nullable -> union-param 400"


def test_sentinels_never_become_filters():
    empty = ai.QueryExtract(
        property_types=[], transaction_type="", boroughs=[], neighborhood="",
        min_size_sf=0, max_size_sf=0, max_rent_per_sf_yr=0, min_lat=0, max_lat=0,
        min_lng=0, max_lng=0, exclude_addr_states=[], exclude_zip3=[], exclude_cities=[],
        keywords=[],
    ).to_query()
    assert empty.max_size_sf == 0 and empty.neighborhood == ""
    assert empty.transaction_type == "lease"      # the one sentinel with a real default


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


def test_keyless_reply_is_deterministic_and_suggests():
    text, suggestions = ai.reply(
        "retail in wynwood", ListingQuery(),
        [{"address": "2618 NW 2nd Ave", "rationale": "1,500 SF retail in Wynwood"}], False,
    )
    assert "2618 NW 2nd Ave" in text and len(suggestions) == 3
    text, suggestions = ai.reply("x", ListingQuery(), [], False)
    assert "Nothing matches" in text and len(suggestions) == 3
