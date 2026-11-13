"""RAG: ingestion, hybrid retrieval, reranking, and the agent's search tool."""
from functools import lru_cache

from ..config import get_settings
from .embed import Embedder, get_embedder
from .rerank import rerank
from .store import Hit, VectorStore


class Retriever:
    """embed query -> hybrid search (vector + full-text, RRF) -> cross-encoder rerank."""

    def __init__(self, store: VectorStore, embedder: Embedder,
                 rerank_model: str | None, candidates: int = 20, top_n: int = 5,
                 min_score: float = 0.0):
        self.store, self.embedder = store, embedder
        self.rerank_model, self.candidates, self.top_n = rerank_model, candidates, top_n
        self.min_score = min_score

    def search(self, query: str) -> list[Hit]:
        hits = self.store.hybrid_search(self.embedder.embed_query(query), query,
                                        k=self.candidates, n=self.candidates)
        if self.rerank_model:
            ranked = rerank(query, hits, self.top_n, self.rerank_model)
            # Drop passages the cross-encoder judged irrelevant, so the LLM isn't
            # tempted to answer from them (a common source of hallucination).
            return [h for h in ranked if h.score >= self.min_score]
        return hits[: self.top_n]


@lru_cache
def get_retriever() -> Retriever:
    s = get_settings()
    return Retriever(VectorStore(s.database_url, s.embed_dim), get_embedder(),
                     s.rerank_model or None, s.rag_candidates, s.rag_top_n, s.rag_min_score)


def format_hits(hits: list[Hit]) -> str:
    """Numbered sources for the model to cite as [1], [2], ..."""
    if not hits:
        return "No matching documents found in the knowledge base."
    return "\n\n".join(f"[{i}] (source: {h.citation})\n{h.content}" for i, h in enumerate(hits, 1))
