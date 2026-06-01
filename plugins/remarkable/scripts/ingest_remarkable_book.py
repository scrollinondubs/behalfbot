#!/usr/bin/env python3
"""Download a book from the reMarkable tablet, chunk it, embed it, and
store it in Postgres + pgvector for semantic query.

Usage:
  python3 plugins/remarkable/scripts/ingest_remarkable_book.py --id <doc-id> [--title <title>]
  python3 plugins/remarkable/scripts/ingest_remarkable_book.py --name <partial-name>

Uses the tablet's direct HTTP API (10.11.99.1) for download + local Ollama
nomic-embed-text for embeddings + pgvector (HNSW) for storage.

The HTTP API requires the tablet to be on the local network (USB or WiFi).
The cloud MCP handles browse/read; book download still needs the direct API.

Env vars:
  CHASSIS_HOME    chassis root (set by dispatcher)
  CHASSIS_PG_DSN      Postgres DSN (or USE_PG=false for SQLite fallback)
  OLLAMA_BASE     Ollama endpoint (default: http://localhost:11434)
  TABLET_BASE     reMarkable local API base (default: http://10.11.99.1)

Ported from <v1-reference-install>#554 (<v1-reference-install> scripts/ingest_remarkable_book.py).
Generalized: REPO references replaced with CHASSIS_HOME/plugin-relative paths,
Sean-specific comments removed, Ollama base made configurable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_CHASSIS_HOME = Path(os.environ.get("CHASSIS_HOME", str(_PLUGIN_DIR.parent.parent)))

if str(_CHASSIS_HOME / "chassis" / "scripts") not in sys.path:
    sys.path.insert(0, str(_CHASSIS_HOME / "chassis" / "scripts"))
if str(_PLUGIN_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR / "scripts"))

from remarkable_denylist import classify_path  # noqa: E402

try:
    import fitz  # pymupdf
except ImportError:
    print("pymupdf not installed. Run: pip3 install --user pymupdf", file=sys.stderr)
    sys.exit(1)

TABLET_BASE = os.environ.get("TABLET_BASE", "http://10.11.99.1")
CACHE_DIR = _CHASSIS_HOME / "temp" / "remarkable-cache"
OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("REMARKABLE_EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_DIM = 768

CHUNK_TARGET_CHARS = 2000
CHUNK_OVERLAP_CHARS = 250
MIN_CHUNK_CHARS = 200


def _curl_get(url: str, timeout: int = 60) -> bytes:
    r = subprocess.run(
        ["/usr/bin/curl", "-sS", "-m", str(timeout), "-o", "-", url],
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"curl {url} failed: rc={r.returncode} stderr={r.stderr.decode(errors='replace')[:500]}")
    return r.stdout


def _curl_get_json(url: str, timeout: int = 60) -> object:
    return json.loads(_curl_get(url, timeout))


def connect_db():
    """Return a Postgres connection via the chassis _chassis_db selector."""
    sys.path.insert(0, str(_CHASSIS_HOME / "chassis" / "scripts"))
    from _chassis_db import connect as _connect  # noqa
    return _connect()


def pgvector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


def find_doc_by_name(name: str) -> dict | None:
    docs = _curl_get_json(f"{TABLET_BASE}/documents/", timeout=10)
    needle = name.lower()
    matches = [d for d in docs if needle in (d.get("VisibleName") or "").lower()]
    if not matches:
        return None
    if len(matches) > 1:
        print(f"Multiple matches for {name!r}:", file=sys.stderr)
        for m in matches:
            print(f"  {m['ID']}  {m['VisibleName']}", file=sys.stderr)
        raise SystemExit("Ambiguous match - use --id instead")
    return matches[0]


def get_doc_metadata(doc_id: str) -> dict:
    root_docs = _curl_get_json(f"{TABLET_BASE}/documents/", timeout=10)
    for d in root_docs:
        if d.get("ID") == doc_id:
            return d
    raise SystemExit(f"Document {doc_id} not found at root.")


def download_book(doc_id: str, dest: Path) -> None:
    url = f"{TABLET_BASE}/download/{doc_id}/placeholder"
    print(f"Downloading {url} ...", file=sys.stderr)
    start = time.time()
    r = subprocess.run(
        ["/usr/bin/curl", "-sS", "-m", "300", "-D", "-", "-o", str(dest), url],
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"curl download failed: rc={r.returncode} stderr={r.stderr.decode(errors='replace')[:500]}")
    headers = r.stdout.decode(errors="replace")
    content_type = ""
    for line in headers.splitlines():
        if line.lower().startswith("content-type:"):
            content_type = line.split(":", 1)[1].strip()
            break
    size = dest.stat().st_size
    elapsed = time.time() - start
    print(f"  downloaded {size:,} bytes ({content_type}) in {elapsed:.1f}s -> {dest}", file=sys.stderr)
    if size < 10_000 and "json" in content_type.lower():
        raise SystemExit(f"Download returned JSON error: {dest.read_bytes()[:500]!r}")


def extract_text(pdf_path: Path) -> list[tuple[int, str]]:
    doc = fitz.open(pdf_path)
    return [(i + 1, page.get_text()) for i, page in enumerate(doc)]


def chunk_pages(
    pages: list[tuple[int, str]],
    target_chars: int = CHUNK_TARGET_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
    min_chars: int = MIN_CHUNK_CHARS,
) -> list[dict]:
    paragraphs: list[tuple[int, str]] = []
    for page_num, text in pages:
        for para in text.split("\n\n"):
            para = " ".join(para.split())
            if len(para) >= 20:
                paragraphs.append((page_num, para))

    chunks: list[dict] = []
    buf: list[tuple[int, str]] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        text = "\n\n".join(p for _, p in buf)
        if len(text) < min_chars:
            buf, buf_len = [], 0
            return
        page_start = buf[0][0]
        page_end = buf[-1][0]
        chunks.append({"text": text, "page_start": page_start, "page_end": page_end, "char_count": len(text)})
        tail: list[tuple[int, str]] = []
        tail_len = 0
        for item in reversed(buf):
            tail.insert(0, item)
            tail_len += len(item[1])
            if tail_len >= overlap_chars:
                break
        buf = tail
        buf_len = sum(len(p) for _, p in buf)

    for page_num, para in paragraphs:
        if buf_len + len(para) > target_chars and buf_len > 0:
            flush()
        buf.append((page_num, para))
        buf_len += len(para)

    if buf:
        text = "\n\n".join(p for _, p in buf)
        if len(text) >= min_chars:
            chunks.append({"text": text, "page_start": buf[0][0], "page_end": buf[-1][0], "char_count": len(text)})

    return chunks


def _embed_once(text: str) -> list[float]:
    payload = json.dumps({"model": EMBEDDING_MODEL, "prompt": text})
    r = subprocess.run(
        ["/usr/bin/curl", "-sS", "-m", "60", "-X", "POST", "-H", "Content-Type: application/json",
         "-d", payload, f"{OLLAMA_BASE}/api/embeddings"],
        capture_output=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ollama curl failed: rc={r.returncode} stderr={r.stderr.decode(errors='replace')[:200]}")
    return json.loads(r.stdout).get("embedding", []) or []


def embed(text: str, max_retries: int = 2) -> list[float] | None:
    for attempt in range(max_retries + 1):
        try:
            vec = _embed_once(text)
        except Exception as e:
            if attempt == max_retries:
                print(f"  embed error after {attempt + 1} tries: {e}", file=sys.stderr)
                return None
            time.sleep(0.5)
            continue
        if len(vec) == EMBEDDING_DIM:
            return vec
        if attempt < max_retries:
            time.sleep(0.5)
            continue
        print(f"  embed returned {len(vec)} dims (expected {EMBEDDING_DIM}) after {attempt + 1} tries", file=sys.stderr)
        return None
    return None


def ingest(doc_id: str, title: str, cache_path: Path) -> None:
    rm_path = f"/{title}"
    decision = classify_path(rm_path)
    if decision.classification != "allow":
        raise SystemExit(
            f"Refusing to ingest {rm_path!r}: classification={decision.classification} "
            f"reason={decision.reason}"
        )

    print(f"Ingesting {title!r} (id={doc_id})", file=sys.stderr)

    if not cache_path.exists():
        download_book(doc_id, cache_path)
    else:
        print(f"  using cached {cache_path} ({cache_path.stat().st_size:,} bytes)", file=sys.stderr)

    print("Extracting text with pymupdf...", file=sys.stderr)
    pages = extract_text(cache_path)
    total_chars = sum(len(t) for _, t in pages)
    print(f"  {len(pages)} pages, {total_chars:,} chars", file=sys.stderr)

    print("Chunking...", file=sys.stderr)
    chunks = chunk_pages(pages)
    print(f"  {len(chunks)} chunks", file=sys.stderr)

    print("Embedding + storing...", file=sys.stderr)
    db = connect_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO documents (title, source_path, mime, inserted_at)
            VALUES (%s, %s, 'application/pdf', now())
            ON CONFLICT(source_path) DO UPDATE SET title = excluded.title
            RETURNING id
            """,
            (title, str(cache_path)),
        )
        document_id = cur.fetchone()[0]
        cur.execute("DELETE FROM document_chunks WHERE document_id = %s", (document_id,))
        cur.execute(
            "UPDATE remarkable_documents SET document_id = %s WHERE remarkable_path = %s",
            (document_id, rm_path),
        )

        start = time.time()
        skipped = 0
        for i, chunk in enumerate(chunks):
            vec = embed(chunk["text"])
            if vec is None:
                skipped += 1
                continue
            token_estimate = chunk["char_count"] // 4
            cur.execute(
                """
                INSERT INTO document_chunks (document_id, chunk_idx, text, token_count, embedding)
                VALUES (%s, %s, %s, %s, %s::vector)
                """,
                (document_id, i, chunk["text"], token_estimate, pgvector_literal(vec)),
            )
            if (i + 1) % 25 == 0 or i == len(chunks) - 1:
                elapsed = time.time() - start
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                print(f"  {i + 1}/{len(chunks)} chunks ({rate:.1f}/s, skipped={skipped})", file=sys.stderr)

        db.commit()
        print(f"  inserted {len(chunks) - skipped} chunks, skipped {skipped}", file=sys.stderr)
    finally:
        db.close()

    print(f"Done. document_id={document_id}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="reMarkable document ID")
    parser.add_argument("--name", help="Partial name match (case-insensitive)")
    parser.add_argument("--title", help="Override display title")
    args = parser.parse_args(argv)

    if not args.id and not args.name:
        parser.error("--id or --name required")

    if args.id:
        meta = get_doc_metadata(args.id)
    else:
        meta = find_doc_by_name(args.name)
        if not meta:
            raise SystemExit(f"No document matched {args.name!r}")

    doc_id = meta["ID"]
    title = args.title or meta.get("VisibleName") or doc_id
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{doc_id}.pdf"

    ingest(doc_id, title, cache_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
