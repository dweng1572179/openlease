"""Normalized domain models + the camelCase API boundary.

SQLite stores snake_case. SpaceFinder's wire contract is camelCase. `to_api()` is the
only place the two meet, so nothing else in the app has to think about it.

Two deliberate divergences from SpaceFinder's schema (spec §5, CoStar v. CREXi):
  1. no `description` column — the broker's marketing prose is NEVER persisted. We store
     `our_description` (LLM-written) and serialize it AS `description`.
  2. `photo_urls` are the broker's own URLs, referenced and hot-linked — never downloaded
     or re-hosted. They serialize as `photos[]`.
The client sees SpaceFinder's object either way.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

METROS: dict[str, dict] = yaml.safe_load(
    (Path(__file__).parent / "data" / "metros.yml").read_text()
)
METRO_KEYS = tuple(METROS)  # ("nyc", "mia", "la", "chi")


class Listing(BaseModel):
    """The ~35-field observed SpaceFinder schema, minus the copyright traps."""
    id: int | None = None
    source: str | None = None            # sources.yml key, "csv", or "nyc_storefront"
    source_url: str                      # UNIQUE — the dedup key
    status: str = "available"
    metro: str                           # nyc | mia | la | chi

    property_type: str | None = None     # retail | office | industrial | flex | land
    subtype: str | None = None
    transaction_type: str = "lease"      # lease | sale

    address: str
    neighborhood: str | None = None
    borough: str | None = None
    lat: float | None = None
    lng: float | None = None

    size_sf: int | None = None
    divisible_min_sf: int | None = None
    divisible_max_sf: int | None = None
    total_building_sf: int | None = None
    floor: str | None = None
    ceiling_height_ft: float | None = None

    asking_rent: float | None = None
    rent_unit: str | None = None         # "sf_yr" | "sf_mo" | "mo"
    lease_type: str | None = None        # NNN | modified gross | gross
    sale_price: int | None = None
    availability_date: str | None = None
    lease_term_months: int | None = None
    condition: str | None = None

    broker_name: str | None = None
    broker_firm: str | None = None
    broker_phone: str | None = None
    broker_email: str | None = None

    features_json: str | None = None
    brochure_url: str | None = None
    our_description: str | None = None   # LLM-written; NEVER the broker's prose
    highlights_json: str | None = None   # LLM
    photo_urls_json: str | None = None   # external references only — never downloaded

    parcel_id: str | None = None
    walk_score: int | None = None
    transit_score: int | None = None
    score_breakdown_json: str | None = None
    semantic_score: float | None = None
    score: float | None = None
    rationale: str | None = None

    first_seen: str | None = None
    last_seen: str | None = None


class ListingQuery(BaseModel):
    """`query.mustHaves` — SpaceFinder's field names, verbatim. Serialize with
    `model_dump(by_alias=True)` to hit the wire contract."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    property_types: list[str] = Field(default_factory=list)
    transaction_type: str = "lease"
    boroughs: list[str] = Field(default_factory=list)
    neighborhood: str = ""
    min_size_sf: int = 0
    max_size_sf: int = 0
    max_rent_per_sf_yr: float = 0
    min_lat: float = 0
    max_lat: float = 0
    min_lng: float = 0
    max_lng: float = 0
    exclude_addr_states: list[str] = Field(default_factory=list)
    exclude_zip3: list[str] = Field(default_factory=list)
    exclude_cities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)   # feeds BM25; not a hard filter


class Parcel(BaseModel):
    """`None` means THIS METRO DOES NOT PUBLISH THIS FIELD — never 'lookup failed',
    never 0. `missing_reason` carries the why, straight to the UI."""
    parcel_id: str              # metro-prefixed, e.g. "nyc:1000160100"
    metro: str
    owner_name: str | None = None
    zoning: str | None = None
    far_built: float | None = None
    far_allowed: float | None = None
    year_built: int | None = None
    lot_sqft: int | None = None
    bldg_sqft: int | None = None
    floors: int | None = None
    units: int | None = None
    use_code: str | None = None
    missing_reason: dict[str, str] = Field(default_factory=dict)  # field -> why it's null
    raw_json: str | None = None


# --- the camelCase boundary ---------------------------------------------------

_JSON_COLS = {
    "features_json": "features",
    "highlights_json": "highlights",
    "photo_urls_json": "photos",
    "score_breakdown_json": "scoreBreakdown",
}
_RENAME = {"our_description": "description"}   # we serve OUR prose under their key


def to_api(row: dict) -> dict:
    """DB row (snake_case, JSON-as-text) -> SpaceFinder's listing object (camelCase)."""
    out: dict = {}
    for k, v in dict(row).items():
        if k in _JSON_COLS:
            out[_JSON_COLS[k]] = json.loads(v) if v else []
        elif k in _RENAME:
            out[_RENAME[k]] = v
        else:
            out[to_camel(k)] = v
    return out
