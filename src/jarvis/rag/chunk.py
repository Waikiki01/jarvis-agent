"""Load files and split them into chunks."""
import re
from dataclasses import dataclass, field
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter

SUPPORTED = {".md", ".txt", ".pdf"}


@dataclass
class Chunk:
    index: int
    content: str
    metadata: dict = field(default_factory=dict)


def load_text(path: Path) -> list[tuple[str, dict]]:
    """Return (text, metadata) parts: one per PDF page, one for a text file."""
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return [(p.extract_text() or "", {"page": i + 1})
                for i, p in enumerate(PdfReader(path).pages)]
    return [(path.read_text(encoding="utf-8", errors="replace"), {})]


_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*$")


def sections(text: str, title: str) -> list[tuple[str, str]]:
    """Split Markdown into (heading path, body) sections, e.g.
    ("Home Network Notes > Devices with fixed IPs", "- NAS ...").
    Text without headings is one section under the document title."""
    path: list[str] = []
    out: list[tuple[str, list[str]]] = [(title, [])]
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            path = path[: level - 1] + [m.group(2)]
            out.append((" > ".join(path), []))
        else:
            out[-1][1].append(line)
    return [(head, "\n".join(body).strip()) for head, body in out if "\n".join(body).strip()]


def split(parts: list[tuple[str, dict]], title: str,
          chunk_size: int = 500, overlap: int = 80) -> list[Chunk]:
    """Split by Markdown section, then paragraphs, lines, sentences; keep page numbers.

    Each chunk starts with its heading path ("contextual header"), so a chunk like
    "Printer: 192.168.0.20" still says it belongs to "Home Network Notes > Devices with
    fixed IPs". Small, single-topic chunks score much better with the reranker than
    long mixed ones.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks: list[Chunk] = []
    for text, meta in parts:
        for head, body in sections(text, title):
            for piece in splitter.split_text(body):
                if piece.strip():
                    chunks.append(Chunk(len(chunks), f"[{head}]\n{piece.strip()}", dict(meta)))
    return chunks


def iter_files(root: Path):
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    for p in paths:
        if p.is_file() and p.suffix.lower() in SUPPORTED:
            yield p
