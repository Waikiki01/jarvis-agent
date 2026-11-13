"""Cross-encoder reranking with FlashRank (small local ONNX model, CPU, free).

Hybrid search is fast but coarse; a cross-encoder reads the question and each candidate
chunk together and scores how well the chunk answers it. We rerank the top ~10-20
candidates and keep the best few. If the model can't load (e.g. offline on first run),
the hybrid order is kept.
"""
import logging
from functools import lru_cache
from pathlib import Path

from .store import Hit

log = logging.getLogger(__name__)


@lru_cache
def _ranker(model: str):
    from flashrank import Ranker

    cache = Path.home() / ".cache" / "flashrank"
    cache.mkdir(parents=True, exist_ok=True)
    return Ranker(model_name=model, cache_dir=str(cache))


def rerank(query: str, hits: list[Hit], top_n: int, model: str) -> list[Hit]:
    if not hits:
        return hits
    try:
        from flashrank import RerankRequest

        results = _ranker(model).rerank(RerankRequest(
            query=query, passages=[{"id": i, "text": h.content} for i, h in enumerate(hits)]))
    except Exception as e:  # keep working without the reranker
        log.warning("rerank unavailable, keeping hybrid order: %s", e)
        return hits[:top_n]
    ordered = []
    for r in results[:top_n]:
        h = hits[r["id"]]
        h.score = float(r["score"])
        ordered.append(h)
    return ordered
