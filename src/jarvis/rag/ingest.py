"""Ingest files into the knowledge base.

    python -m jarvis.rag.ingest samples/           # a folder (md, txt, pdf)
    python -m jarvis.rag.ingest notes.pdf --force  # re-embed even if unchanged

Re-running is cheap: a file whose content hash is unchanged is skipped.
"""
import argparse
import sys
from pathlib import Path

from ..config import get_settings
from .chunk import iter_files, load_text, split
from .embed import Embedder, get_embedder
from .store import VectorStore, sha256


def ingest_path(root: Path, store: VectorStore, embedder: Embedder, force: bool = False) -> dict:
    counts = {"ingested": 0, "skipped": 0, "chunks": 0}
    for path in iter_files(root):
        parts = load_text(path)
        digest = sha256("".join(t for t, _ in parts))
        source = str(path.resolve().relative_to(root.resolve().parent))
        if not force and store.document_hash(source) == digest:
            counts["skipped"] += 1
            continue
        title = path.stem.replace("_", " ").replace("-", " ")
        chunks = split(parts, title)
        if not chunks:
            continue
        vectors = embedder.embed_documents([c.content for c in chunks])
        store.upsert_document(source, title, digest, chunks, vectors)
        counts["ingested"] += 1
        counts["chunks"] += len(chunks)
        print(f"  + {source}: {len(chunks)} chunks")
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    s = get_settings()
    if not s.database_url:
        print("JARVIS_DATABASE_URL is not set (see .env.example)", file=sys.stderr)
        return 2
    store = VectorStore(s.database_url, s.embed_dim)
    store.ensure_schema()
    print(ingest_path(a.path, store, get_embedder(), a.force), "| totals:", store.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
