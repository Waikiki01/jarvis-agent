"""Postgres + pgvector store with hybrid (vector + full-text) search.

Hybrid search: run a vector search (meaning) and a full-text search (exact words, names,
numbers) separately, then merge the two rankings with Reciprocal Rank Fusion (RRF):
    score = 1/(60 + rank_vector) + 1/(60 + rank_text)
RRF needs no score normalisation, which is why it is the common default.
"""
import hashlib
import json
import re
from dataclasses import dataclass

import psycopg
from pgvector.psycopg import register_vector

from .chunk import Chunk

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS documents (
    id          bigserial PRIMARY KEY,
    source      text UNIQUE NOT NULL,
    title       text NOT NULL,
    sha256      text NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chunks (
    id          bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index int NOT NULL,
    content     text NOT NULL,
    metadata    jsonb NOT NULL DEFAULT '{{}}',
    embedding   vector({dim}) NOT NULL,
    tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv);
"""

HYBRID_SQL = """
WITH vec AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> %(qvec)s) AS r
    FROM chunks ORDER BY embedding <=> %(qvec)s LIMIT %(k)s
), txt AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS r
    FROM chunks c, websearch_to_tsquery('english', %(qtext)s) q
    WHERE c.tsv @@ q ORDER BY ts_rank_cd(c.tsv, q) DESC LIMIT %(k)s
)
SELECT c.id, c.content, c.metadata, c.chunk_index, d.source, d.title,
       COALESCE(1.0 / (60 + vec.r), 0) + COALESCE(1.0 / (60 + txt.r), 0) AS score,
       vec.r AS vec_rank, txt.r AS txt_rank
FROM vec FULL OUTER JOIN txt ON vec.id = txt.id
JOIN chunks c ON c.id = COALESCE(vec.id, txt.id)
JOIN documents d ON d.id = c.document_id
ORDER BY score DESC
LIMIT %(n)s
"""


@dataclass
class Hit:
    id: int
    content: str
    source: str
    title: str
    chunk_index: int
    metadata: dict
    score: float
    vec_rank: int | None = None
    txt_rank: int | None = None

    @property
    def citation(self) -> str:
        page = self.metadata.get("page")
        return f"{self.source}" + (f" p.{page}" if page else f" #{self.chunk_index}")


class VectorStore:
    def __init__(self, dsn: str, dim: int):
        self.dsn, self.dim = dsn, dim

    def connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, autocommit=True)
        register_vector(conn)
        return conn

    def ensure_schema(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(SCHEMA.format(dim=self.dim))

    def upsert_document(self, source: str, title: str, text_hash: str,
                        chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        """Replace a document and its chunks in one transaction."""
        import numpy as np

        with self.connect() as conn, conn.transaction():
            conn.execute("DELETE FROM documents WHERE source = %s", (source,))
            doc_id = conn.execute(
                "INSERT INTO documents (source, title, sha256) VALUES (%s, %s, %s) RETURNING id",
                (source, title, text_hash)).fetchone()[0]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks (document_id, chunk_index, content, metadata, embedding)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    [(doc_id, c.index, c.content, json.dumps(c.metadata), np.array(e))
                     for c, e in zip(chunks, embeddings, strict=True)])

    def document_hash(self, source: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT sha256 FROM documents WHERE source = %s", (source,)).fetchone()
            return row[0] if row else None

    def hybrid_search(self, qvec: list[float], qtext: str, k: int = 20, n: int = 10) -> list[Hit]:
        import numpy as np

        # OR the words: websearch_to_tsquery ANDs them by default, so a question with one
        # word not in the text ("time") would miss entirely. ts_rank_cd still ranks
        # passages that match more words higher.
        qtext = " or ".join(re.findall(r"\w+", qtext)) or qtext

        with self.connect() as conn:
            rows = conn.execute(HYBRID_SQL, {"qvec": np.array(qvec), "qtext": qtext, "k": k, "n": n}).fetchall()
        return [Hit(id=r[0], content=r[1], metadata=r[2], chunk_index=r[3], source=r[4],
                    title=r[5], score=float(r[6]), vec_rank=r[7], txt_rank=r[8]) for r in rows]

    def stats(self) -> dict:
        with self.connect() as conn:
            d, c = conn.execute("SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks)").fetchone()
        return {"documents": d, "chunks": c}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
