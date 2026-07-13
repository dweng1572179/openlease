"""Did the geocoder return the address we asked for, or just *an* address?

A metro-scoped geocoder does not decline. NYC GeoSearch, asked for "205 Hallock Road, Stony
Brook NY", answers "205 DAHILL ROAD, Brooklyn" — a different street, in a different place —
and reports `match_type: fallback` with confidence 0.8, the SAME values it reports for a
correct hit. Its own confidence signal cannot separate them. So we check the answer against
the question ourselves.

The first version of this compared only the first non-numeric token after the house number.
That is fine for "Hallock" and useless for the streets we actually crawl: across the whole
Manhattan grid ("West 38th Street") and all of Miami ("NW 2nd Ave") that token is a
DIRECTION, and the abbreviator even folds "south" to "s". So:

    asked   302 South Colonial Drive, Cleburne, TX     -> distinctive token "s"
    got     302 S DAHILL RD, BROOKLYN, NY              -> contains "s"
    passed  -> a Texas property, pinned in Brooklyn, accepted

Brooklyn really does have South 1st through South 11th Street. The docstring's own canonical
failure sailed through its own guard.

So: match the house number AND a token that actually distinguishes the street — skipping
directions, ordinals and street-type words, which every street shares. If the address has no
distinctive token at all, we REFUSE rather than accept unverified: an address we cannot check
is not an address we can trust.
"""
import re

_WORD = re.compile(r"[a-z0-9]+")
_ORDINAL = re.compile(r"^(\d+)(st|nd|rd|th)$")

# Words every street shares. None of them tells you WHICH street.
_GENERIC = {
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "north", "south", "east", "west", "northeast", "northwest", "southeast", "southwest",
    "st", "street", "ave", "avenue", "blvd", "boulevard", "rd", "road", "dr", "drive",
    "pl", "place", "ct", "court", "ln", "lane", "pkwy", "parkway", "hwy", "highway",
    "ter", "terrace", "cir", "circle", "sq", "square", "trl", "trail", "way", "loop",
    "suite", "ste", "unit", "floor", "fl", "apt", "usa",
}
_ABBREV = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr", "road": "rd",
    "place": "pl", "court": "ct", "lane": "ln", "parkway": "pkwy", "highway": "hwy",
    "terrace": "ter", "circle": "cir", "square": "sq", "trail": "trl",
    "north": "n", "south": "s", "east": "e", "west": "w",
}


def norm(tok: str) -> str:
    """"5th" -> "5", "Avenue" -> "ave". We ask "350 5th Ave"; the geocoder answers
    "350 5 AVENUE"."""
    m = _ORDINAL.match(tok)
    if m:
        return m.group(1)
    return _ABBREV.get(tok, tok)


def _tokens(s: str) -> list[str]:
    return [norm(t) for t in _WORD.findall(s.lower())]


_DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}
_STREET_TYPES = {
    "st", "ave", "blvd", "rd", "dr", "pl", "ct", "ln", "pkwy", "hwy", "ter", "cir",
    "sq", "trl", "way", "loop", "plaza", "broadway",
}


def distinctive(address: str) -> tuple[str, list[str], set[str]] | None:
    """(house number, the tokens that identify this STREET, its direction words).

    Only the street — everything after the first comma is city/state/zip, and including it
    was the whole bug: "57 West 38th Street, New York, NY" kept "new" and "york" as
    identifying tokens, so "57 W 41 ST, NEW YORK" matched on the CITY and a completely
    different avenue was accepted.

    None when there is no house number, or nothing after it but words every street shares —
    "302 South Drive" tells us nothing a thousand other streets don't, and an address we
    cannot check is not one we can trust.
    """
    toks = _tokens(address.split(",")[0])
    if not toks or not toks[0].isdigit():
        return None
    house, rest = toks[0], toks[1:]
    # A street ENDS at its type word. Without this, an address written with no commas —
    # "205 hallock rd stony brook ny", which is exactly what a URL slug gives us — kept
    # "stony", "brook" and "ny" as identifying tokens, and "205 DAHILL ROAD, Brooklyn, NY"
    # matched on the STATE. The city is not the street.
    for i, t in enumerate(rest):
        if t in _STREET_TYPES:
            rest = rest[: i + 1]
            break
    dirs = {t for t in rest if t in _DIRECTIONS}
    names = [t for t in rest if t not in _GENERIC and not t.isdigit()]
    nums = [t for t in rest if t.isdigit()]     # "5th Ave" -> "5": numbered streets
    if not names and not nums:
        return None
    return house, (names or nums), dirs


def matches(asked: str, got: str) -> bool:
    """Is `got` the address we asked for? The house number, the street's identifying token,
    and its direction must all survive in the answer. West 38th is not East 38th."""
    a = distinctive(asked)
    if not a:
        return False                     # cannot verify => do not accept
    house, marks, dirs = a
    got_toks = _tokens(got)
    if house not in got_toks:
        return False
    if dirs and not dirs.issubset(set(got_toks)):
        return False                     # W 38th != E 38th
    return any(m in got_toks for m in marks)
