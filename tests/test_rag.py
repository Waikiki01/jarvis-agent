"""RAG tests.

Chunking tests are pure unit tests. Store/retrieval tests use a throwaway database
`jarvis_test` in the docker Postgres and a deterministic fake embedder (no Ollama);
they are skipped when Postgres isn't running.
"""
import hashlib
import math
import re
from pathlib import Path

import psycopg
import pytest

from jarvis.rag import Retriever, format_hits
from jarvis.rag.chunk import split
from jarvis.rag.ingest import ingest_path
from jarvis.rag.store import VectorStore

ADMIN_DSN = "postgresql://jarvis:jarvis@localhost:5433/jarvis"
TEST_DSN = "postgresql://jarvis:jarvis@localhost:5433/jarvis_test"
SAMPLES = Path(__file__).parent.parent / "samples"
DIM = 64


class HashEmbedder:
    """Bag-of-words hashed into DIM buckets: shared words -> similar vectors."""
    dim = DIM

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


# ---------- chunking ----------

def test_split_adds_title_and_keeps_pages():
    parts = [("First page text. " * 10, {"page": 1}), ("Second page. " * 10, {"page": 2})]
    chunks = split(parts, "My Doc", chunk_size=100, overlap=10)
    assert len(chunks) > 2
    assert all(c.content.startswith("[My Doc]\n") for c in chunks)
    assert {c.metadata["page"] for c in chunks} == {1, 2}
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_split_prefers_heading_boundaries():
    text = "## A\n" + "alpha " * 100 + "\n## B\n" + "beta " * 100
    chunks = split([(text, {})], "t", chunk_size=700, overlap=0)
    assert not any("alpha" in c.content and "beta" in c.content for c in chunks)


# ---------- store + retrieval (needs docker Postgres) ----------

@pytest.fixture(scope="module")
def store():
    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=2) as conn:
            conn.execute("DROP DATABASE IF EXISTS jarvis_test")
            conn.execute("CREATE DATABASE jarvis_test")
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (docker compose up -d)")
    s = VectorStore(TEST_DSN, DIM)
    s.ensure_schema()
    yield s
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS jarvis_test WITH (FORCE)")


@pytest.fixture(scope="module")
def ingested(store):
    counts = ingest_path(SAMPLES, store, HashEmbedder())
    assert counts["ingested"] == 3
    return store


def test_reingest_skips_unchanged(ingested):
    counts = ingest_path(SAMPLES, ingested, HashEmbedder())
    assert counts == {"ingested": 0, "skipped": 3, "chunks": 0}


def test_hybrid_finds_exact_keyword(ingested):
    """Full-text search catches exact tokens like an IP address or model number."""
    r = Retriever(ingested, HashEmbedder(), rerank_model=None)
    hits = r.search("Synology NAS backups")
    assert hits and "home_network" in hits[0].source
    assert hits[0].txt_rank is not None      # matched by the keyword side too


def test_hybrid_ranks_right_document(ingested):
    r = Retriever(ingested, HashEmbedder(), rerank_model=None)
    assert "car_maintenance" in r.search("when is the next car service due")[0].source
    assert "japan_trip" in r.search("which flight do we take to Osaka")[0].source


def test_format_hits_numbers_and_cites(ingested):
    hits = Retriever(ingested, HashEmbedder(), rerank_model=None, top_n=2).search("router firmware")
    text = format_hits(hits)
    assert text.startswith("[1] (source: samples/")
    assert "[2]" in text
    assert format_hits([]).startswith("No matching documents")


def test_keyword_side_matches_any_word(ingested):
    """'time' is not in the text; an AND query would miss, the OR query must not."""
    hits = Retriever(ingested, HashEmbedder(), rerank_model=None).search("TeamLab booking time")
    top = next(h for h in hits if "japan_trip" in h.source)
    assert top.txt_rank is not None


def test_chunks_carry_heading_path():
    text = "# Home Notes\n\n## Printer\n- IP 192.168.0.20\n\n## Router\n- Admin page 192.168.0.1\n"
    chunks = split([(text, {})], "home_notes")
    assert [c.content for c in chunks] == [
        "[Home Notes > Printer]\n- IP 192.168.0.20",
        "[Home Notes > Router]\n- Admin page 192.168.0.1",
    ]


def test_documents_are_per_owner(ingested):
    assert ingest_path(SAMPLES, ingested, HashEmbedder(), owner="alice")["ingested"] == 3  # same files, own copy
    r = Retriever(ingested, HashEmbedder(), rerank_model=None)
    assert r.search("Synology NAS backups", owner="alice")
    assert r.search("Synology NAS backups", owner="carol") == []          # nothing of anyone else's
    assert ingest_path(SAMPLES, ingested, HashEmbedder())["skipped"] == 3  # local's copy untouched
