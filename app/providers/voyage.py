"""Voyage embeddings — `voyage-4-lite`, 1024-dim. The free tier is 200M tokens; a
5,000-listing corpus is ~0.5M, so this is free forever in practice. Every call goes
through cache.cached(), so a listing (or a repeated query phrase) is ever billed once."""
import httpx

from ..cache import cached
from ..config import settings

URL = "https://api.voyageai.com/v1/embeddings"
MODEL = "voyage-4-lite"
DIM = 1024

# Voyage's lite-tier rate is ~$0.02 per 1M tokens. embed_listings batches up to 64
# listings; each one's address+neighborhood+type+description is roughly 100-150 tokens,
# so a full batch is ~8,000 tokens -> ~$0.00016. A query embed (cosine_ids, one short
# phrase) is ~15 tokens -> effectively free. Both round up to a flat 1c so the cache-dedup
# + monthly-budget ledger only needs one constant, with generous headroom either way — the
# free 200M-token tier covers the whole corpus 400x over regardless.
_EMBED_COST_CENTS = 1


class VoyageEmbedder:
    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        """input_type is 'document' at ingest and 'query' at search — Voyage encodes them
        differently, and mixing them measurably degrades retrieval."""
        def fetch():
            r = httpx.post(
                URL,
                headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
                json={"input": texts, "model": MODEL, "input_type": input_type},
                timeout=60.0,
            )
            r.raise_for_status()
            return r.json()

        # Voyage IS a paid surface (spec §6/§8) -- cache.BudgetExceeded propagates to the
        # caller (rank.embed_listings / rank.cosine_ids), which degrades gracefully and
        # logs loudly rather than crashing search.
        data = cached("voyage", input_type, {"texts": texts, "model": MODEL}, fetch,
                      cost_cents=_EMBED_COST_CENTS)
        return [d["embedding"] for d in data["data"]]
