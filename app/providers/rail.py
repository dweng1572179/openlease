"""Rail/ferry stations as BUNDLED STATIC JSON — ~800 points across four metros, <100KB.
Zero API calls at runtime and therefore zero runtime failure modes. Regenerate with
`python -m app.data.rail.refresh` when an agency opens a station."""
import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent.parent / "data" / "rail"


@lru_cache
def stations(metro: str) -> list[dict]:
    """[{name, lat, lng, mode, routes: []}] — mode is 'rail' or 'ferry'."""
    p = _DIR / f"{metro}.json"
    return json.loads(p.read_text()) if p.exists() else []
