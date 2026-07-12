"""The two keyless fast paths (wp-json, JSON-LD), and the two copyright invariants: we
never persist the page's prose, and we never download a photo."""
import json
import logging
import os
import pathlib

os.environ["ANTHROPIC_API_KEY"] = ""      # keyless: the LLM rung must be skipped, loudly

import pytest  # noqa: E402

from app import extract  # noqa: E402

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
