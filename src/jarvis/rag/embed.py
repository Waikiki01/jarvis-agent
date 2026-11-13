"""Embeddings. Default: nomic-embed-text on local Ollama (free, 768 dims).

nomic-embed-text was trained with task prefixes; using "search_document:" for stored
text and "search_query:" for questions measurably improves retrieval.
"""
from typing import Protocol

from ..config import get_settings


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class OllamaEmbedder:
    def __init__(self, model: str, base_url: str, dim: int):
        from langchain_ollama import OllamaEmbeddings

        self._emb = OllamaEmbeddings(model=model, base_url=base_url)
        self._nomic = "nomic" in model
        self.dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._nomic:
            texts = [f"search_document: {t}" for t in texts]
        return self._emb.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._emb.embed_query(f"search_query: {text}" if self._nomic else text)


def get_embedder() -> Embedder:
    s = get_settings()
    return OllamaEmbedder(s.embed_model, s.ollama_base_url, s.embed_dim)
