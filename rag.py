"""ReplaySense semantic RAG over the Morphling corpus.

Real embeddings RAG, fully local:
  * Embed every corpus chunk with nomic-embed-text via Ollama
    (http://localhost:11434/api/embeddings).
  * Store vectors in a persistent ChromaDB at ./chroma_store/.
  * A corpus fingerprint (content hash) is cached; we only re-embed when the
    corpus actually changes, so repeat runs are instant.
  * retrieve(query, phase, top_k) returns the most semantically relevant chunks.

Degrades gracefully: if Ollama embeddings or ChromaDB are unreachable, we fall
back to a keyword heuristic so the live demo never hard-fails.
"""

import hashlib
import json
import os
from pathlib import Path

import requests

try:
    import openshell_sandbox as _sandbox
except Exception:
    _sandbox = None

REPO_ROOT = Path(__file__).parent
CORPUS_DIR = REPO_ROOT / "corpus"
CHROMA_DIR = REPO_ROOT / "chroma_store"
COLLECTION_NAME = "morphling_corpus"

EMBED_URL = os.environ.get("REPLAYSENSE_EMBED_URL", "http://localhost:11434/api/embeddings")
EMBED_MODEL = os.environ.get("REPLAYSENSE_EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT = int(os.environ.get("REPLAYSENSE_EMBED_TIMEOUT", "60"))


# ============================================================================
# CORPUS LOADING + CHUNKING
# ============================================================================
def _strip_frontmatter(text: str):
    """Return (body, frontmatter_dict-ish). We only care about the body + phase."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].lstrip("\n")
    return text


def load_chunks():
    """Load corpus and split each file into section-level chunks.

    Each chunk: {id, phase, name, title, text}. Sections are split on '## '
    headers, with the file's H1 title prepended for context. Files with no
    sections become a single chunk.
    """
    chunks = []
    for md_file in sorted(CORPUS_DIR.rglob("*.md")):
        phase = md_file.parent.name
        name = md_file.stem
        raw = md_file.read_text(encoding="utf-8")
        body = _strip_frontmatter(raw)

        # H1 title for context
        title = name
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break

        # Split on level-2 headers, keeping the lead paragraph as section 0.
        sections, current = [], []
        for line in body.splitlines():
            if line.startswith("## ") and current:
                sections.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("\n".join(current).strip())

        for i, sec in enumerate(sections):
            if not sec.strip():
                continue
            text = f"# {title}\n\n{sec}" if not sec.startswith("#") else sec
            chunks.append({
                "id": f"{phase}/{name}#{i}",
                "phase": phase,
                "name": name,
                "title": title,
                "text": text,
            })
    return chunks


def corpus_fingerprint(chunks) -> str:
    """Stable hash of corpus content; changes only when text changes."""
    h = hashlib.sha256()
    for c in sorted(chunks, key=lambda c: c["id"]):
        h.update(c["id"].encode("utf-8"))
        h.update(b"\0")
        h.update(c["text"].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


# ============================================================================
# EMBEDDINGS (Ollama / nomic-embed-text)
# ============================================================================
def embed_text(text: str) -> list:
    """Embed a single string via the local Ollama embeddings endpoint."""
    # Verify endpoint is local and audit the call
    if _sandbox is not None:
        try:
            _sandbox.verify_local_endpoint(EMBED_URL)
            _sandbox.log_embed_call(EMBED_URL, EMBED_MODEL, len(text))
        except ValueError:
            raise  # re-raise endpoint violations — never silently skip
        except Exception:
            pass   # never crash on audit failure
    r = requests.post(
        EMBED_URL,
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=EMBED_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    # /api/embeddings -> {"embedding": [...]}; newer /api/embed -> {"embeddings": [[...]]}
    if "embedding" in data:
        return data["embedding"]
    if "embeddings" in data and data["embeddings"]:
        return data["embeddings"][0]
    raise ValueError(f"Unexpected embeddings response: {list(data)[:5]}")


def _embeddings_available() -> bool:
    try:
        v = embed_text("ping")
        return isinstance(v, list) and len(v) > 0
    except Exception:
        return False


# ============================================================================
# CHROMA INDEX
# ============================================================================
def _get_collection():
    import chromadb  # imported lazily so the keyword fallback works without it
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    # We supply our own embeddings, so no embedding_function is needed.
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def _stored_fingerprint(collection) -> str:
    try:
        got = collection.get(ids=["__fingerprint__"], include=["metadatas"])
        metas = got.get("metadatas") or []
        if metas and metas[0]:
            return metas[0].get("fingerprint", "")
    except Exception:
        pass
    return ""


def ensure_index(verbose: bool = False) -> bool:
    """Build/refresh the vector index if the corpus changed. Returns True on success."""
    chunks = load_chunks()
    fp = corpus_fingerprint(chunks)
    collection = _get_collection()

    if _stored_fingerprint(collection) == fp and collection.count() > 1:
        if verbose:
            print(f"[rag] index up to date ({collection.count() - 1} chunks)")
        return True

    if verbose:
        print(f"[rag] (re)embedding {len(chunks)} chunks via {EMBED_MODEL}...")

    # Fresh build: clear and re-add so deletions/edits are reflected.
    try:
        existing = collection.get().get("ids", [])
        if existing:
            collection.delete(ids=existing)
    except Exception:
        pass

    ids, embeddings, documents, metadatas = [], [], [], []
    for c in chunks:
        ids.append(c["id"])
        embeddings.append(embed_text(c["text"]))
        documents.append(c["text"])
        metadatas.append({"phase": c["phase"], "name": c["name"], "title": c["title"]})

    # Sentinel row carrying the fingerprint (zero vector, never retrieved by content).
    dim = len(embeddings[0]) if embeddings else 768
    ids.append("__fingerprint__")
    embeddings.append([0.0] * dim)
    documents.append("")
    metadatas.append({"phase": "_meta", "name": "_meta", "fingerprint": fp})

    collection.add(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
    if verbose:
        print(f"[rag] indexed {len(chunks)} chunks at {CHROMA_DIR}")
    return True


# ============================================================================
# KEYWORD FALLBACK (used when embeddings / chroma are unavailable)
# ============================================================================
def _keyword_retrieve(query: str, phase: str, top_k: int):
    """Cheap lexical scoring over corpus chunks — the never-fail safety net."""
    chunks = load_chunks()
    terms = {t for t in query.lower().replace(",", " ").split() if len(t) > 2}
    scored = []
    for c in chunks:
        text_l = c["text"].lower()
        score = sum(text_l.count(t) for t in terms)
        if phase and (c["phase"] == phase or c["phase"].startswith(str(phase))):
            score += 5
        if c["phase"] == "general":
            score += 1
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]


# ============================================================================
# PUBLIC RETRIEVE
# ============================================================================
def retrieve(query: str, phase: str = None, top_k: int = 5, verbose: bool = False):
    """Return the top_k most relevant corpus chunks for `query`.

    Tries real semantic RAG (Ollama embeddings + ChromaDB). On ANY failure
    falls back to keyword retrieval so the demo never hard-fails. Each result
    is {id, phase, name, title, text} — the same shape agent.py expects.
    """
    biased_query = f"[{phase}] {query}" if phase else query
    try:
        import chromadb  # noqa: F401  (fail fast to fallback if missing)
        if not _embeddings_available():
            raise RuntimeError("ollama embeddings unreachable")
        ensure_index(verbose=verbose)
        collection = _get_collection()
        qvec = embed_text(biased_query)
        res = collection.query(
            query_embeddings=[qvec],
            n_results=top_k + 1,  # +1 in case the sentinel sneaks in
            include=["documents", "metadatas"],
        )
        out = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        for cid, doc, meta in zip(ids, docs, metas):
            if cid == "__fingerprint__":
                continue
            meta = meta or {}
            out.append({
                "id": cid,
                "phase": meta.get("phase", ""),
                "name": meta.get("name", ""),
                "title": meta.get("title", meta.get("name", "")),
                "text": doc,
            })
            if len(out) >= top_k:
                break
        if out:
            return out
        raise RuntimeError("empty semantic result")
    except Exception as e:
        if verbose:
            print(f"[rag] semantic retrieval unavailable ({e}); using keyword fallback")
        return _keyword_retrieve(query, phase, top_k)


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "morphling laning last hits and deaths vs viper"
    ph = sys.argv[2] if len(sys.argv) > 2 else None
    hits = retrieve(q, phase=ph, top_k=5, verbose=True)
    print(f"\nTop {len(hits)} chunks for: {q!r} (phase={ph})\n")
    for h in hits:
        print(f"  - {h['id']}  [{h['phase']}]  {h['title']}")
