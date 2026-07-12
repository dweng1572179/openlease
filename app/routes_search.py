"""POST /api/search — SpaceFinder's contract, verbatim.

  request  {message, priorState, sessionId, metro}
  response {query, results[], reply, isNearMiss, suggestions[]}

Pipeline (spec Layer 3): LLM parse -> HARD filter -> hybrid rank -> LLM reply.
Near-miss: when the hard filter returns nothing, relax the SOFTEST constraint (the rent
cap, then the size band) and re-run — but say so, rather than pretending the results
matched."""
import uuid

from fastapi import Depends
from pydantic import BaseModel

from . import ai, db, rank
from .app import app, require_auth
from .models import METRO_KEYS, ListingQuery, to_api


class SearchRequest(BaseModel):
    message: str
    priorState: dict | None = None     # the prior turn's query.mustHaves, camelCase
    sessionId: str | None = None
    metro: str = "nyc"


# Named stages, tried in this order, EACH AT MOST ONCE. A stage whose constraint isn't
# set is simply skipped (not a no-op retry of itself) — see _relax's docstring for why
# that "at most once" has to be enforced by the caller, not inferred from field state.
_LADDER = (
    ("rent", "the rent cap"),
    ("size", "the size range"),
    ("neighborhood", "the neighborhood"),
)


def _relax(q: ListingQuery, stage: str) -> ListingQuery | None:
    """Apply ONE named stage of the softness ladder to `q`. Returns None when this
    stage's constraint isn't set (caller moves on to the next stage).

    Each stage must be tried AT MOST ONCE per request. The rent-cap and neighborhood
    stages fully clear their constraint in one shot, so re-deriving "what to relax next"
    from field state would have been safe for them — but the size stage only WIDENS
    (min_size_sf shrinks toward 0, max_size_sf only ever grows), so it never fully
    clears itself. A caller that re-inspected field state to pick the next stage (as an
    earlier version of this function did) would keep re-entering "size" forever whenever
    widening it alone can never satisfy some OTHER hard constraint (e.g. a property_type
    with zero inventory at any size) — an unbounded loop that eventually overflows
    SQLite's INTEGER bind. Naming the stage explicitly and having the caller iterate the
    fixed _LADDER exactly once makes "at most once per stage" a caller invariant instead
    of something this function has to infer and get right."""
    r = q.model_copy(deep=True)
    if stage == "rent":
        if not r.max_rent_per_sf_yr:
            return None
        r.max_rent_per_sf_yr = 0
        return r
    if stage == "size":
        if not (r.min_size_sf or r.max_size_sf):
            return None
        r.min_size_sf = int(r.min_size_sf * 0.6) if r.min_size_sf else 0
        r.max_size_sf = int(r.max_size_sf * 1.6) if r.max_size_sf else 0
        return r
    if stage == "neighborhood":
        if not (r.neighborhood or r.boroughs):
            return None
        r.neighborhood, r.boroughs = "", []
        return r
    return None


@app.post("/api/search")
def api_search(body: SearchRequest, _=Depends(require_auth)):
    metro = body.metro if body.metro in METRO_KEYS else "nyc"
    session_id = body.sessionId or uuid.uuid4().hex

    q = ai.nl_to_query(body.message, body.priorState, metro)

    rows = db.filter_listings(q, metro)
    is_near_miss = False
    relaxed_what = ""
    q_used = q
    if not rows:
        # Cumulative: each stage relaxes ON TOP of whatever the prior stage already
        # relaxed. Each stage is tried EXACTLY once, in softness order, and the ladder
        # stops the instant a relaxed query produces results — or gives up honestly
        # (0 results, isNearMiss stays False) once every stage has been tried.
        #
        # `applied` accumulates EVERY stage in force, not just the one that finally
        # produced rows. Reporting only the last would be a half-truth: when the rent
        # cap is dropped and only the later size widening yields a hit, the results are
        # still uncapped on rent, and saying "I relaxed the size range" hands the user a
        # listing 95x over the ceiling they stated while claiming that ceiling held.
        applied: list[str] = []
        for stage, label in _LADDER:
            step = _relax(q_used, stage)
            if step is None:
                continue
            q_used = step
            applied.append(label)
            candidate_rows = db.filter_listings(q_used, metro)
            if candidate_rows:
                rows = candidate_rows
                relaxed_what = " and ".join(applied)
                is_near_miss = True
                break
        else:
            q_used = q      # ladder gave up: nothing matched, so nothing was relaxed

    ranked = rank.rank_listings([r["id"] for r in rows], q_used)
    by_id = {r["id"]: r for r in rows}
    results = []
    for r in ranked:
        item = to_api(by_id[r["id"]])
        item["semanticScore"] = r["semantic_score"]
        item["score"] = r["score"]
        item["rationale"] = r["rationale"]
        results.append(item)

    # ai.reply() is the ONE place that composes the near-miss sentence (it's part of the
    # JSON API contract, read by non-UI clients too, so it must be self-contained). Do
    # NOT re-prepend the disclosure here — that used to double it, and the HTML banner
    # tripled it on top.
    text, suggestions = ai.reply(body.message, q_used, results, is_near_miss, relaxed_what)

    must_haves = q.model_dump(by_alias=True)
    db.save_turn(session_id, metro, body.message, must_haves, text)

    return {
        "query": {"mustHaves": must_haves, "relaxed": relaxed_what or None},
        "results": results,
        "reply": text,
        "isNearMiss": is_near_miss,
        "suggestions": suggestions,
        "sessionId": session_id,
    }


@app.get("/api/sessions")
def api_sessions(_=Depends(require_auth)):
    return {"sessions": db.list_sessions()}


@app.get("/api/sessions/{session_id}")
def api_session(session_id: str, _=Depends(require_auth)):
    return {"turns": db.get_session_turns(session_id)}
