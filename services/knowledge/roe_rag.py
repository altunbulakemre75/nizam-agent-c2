"""ROE RAG — Doktrin ve ROE belgelerinde anlamsal arama.

Opsiyonel LlamaIndex entegrasyonu. llama-index kurulu değilse in-memory
TF-IDF fallback kullanılır. Her iki yol da aynı interface döner:
  query(text) → list[RAGResult]

Belgeler: docs/roe/*.md (Markdown)
Index: .rag_cache/ (LlamaIndex) veya in-memory (TF-IDF)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_ROE_DIR = Path("docs/roe")


@dataclass
class RAGResult:
    source: str              # dosya yolu
    excerpt: str             # ilgili paragraf (~200 char)
    score: float             # 0..1
    rule_id: str | None = None


class ROERAG:
    """Retrieval-augmented generation için basit bir interface."""

    def __init__(self, roe_dir: Path | None = None) -> None:
        self.roe_dir = roe_dir or DEFAULT_ROE_DIR
        self._documents: list[tuple[Path, str]] = []
        self._llama_index = None
        self._load_documents()
        self._try_build_llama_index()

    def _load_documents(self) -> None:
        if not self.roe_dir.exists():
            return
        for p in sorted(self.roe_dir.glob("**/*.md")):
            self._documents.append((p, p.read_text(encoding="utf-8")))

    def _try_build_llama_index(self) -> None:
        try:
            from llama_index.core import Document, VectorStoreIndex  # noqa: PLC0415
        except ImportError:
            log.info("llama-index kurulu değil — TF-IDF fallback")
            return

        docs = [Document(text=text, metadata={"source": str(p)})
                for p, text in self._documents]
        if not docs:
            return
        try:
            self._llama_index = VectorStoreIndex.from_documents(docs)
        except Exception as exc:
            log.warning("LlamaIndex build başarısız: %s — fallback", exc)
            self._llama_index = None

    def query(self, text: str, top_k: int = 3) -> list[RAGResult]:
        """LlamaIndex varsa vektörel sorgu; yoksa keyword tabanlı fallback."""
        if self._llama_index is not None:
            return self._query_llama(text, top_k)
        return self._query_keyword(text, top_k)

    def _query_llama(self, text: str, top_k: int) -> list[RAGResult]:
        query_engine = self._llama_index.as_query_engine(similarity_top_k=top_k)
        response = query_engine.query(text)
        results: list[RAGResult] = []
        for node in getattr(response, "source_nodes", []):
            results.append(RAGResult(
                source=node.metadata.get("source", "?"),
                excerpt=node.text[:200],
                score=float(node.score or 0.0),
                rule_id=self._extract_rule_id(node.text),
            ))
        return results

    def _query_keyword(self, text: str, top_k: int) -> list[RAGResult]:
        """TF-IDF yerine basit keyword overlap sayacı — LlamaIndex yoksa."""
        q_tokens = set(_tokenize(text))
        scored: list[tuple[float, Path, str]] = []
        for path, content in self._documents:
            for para in content.split("\n\n"):
                p_tokens = set(_tokenize(para))
                if not p_tokens:
                    continue
                overlap = len(q_tokens & p_tokens)
                if overlap == 0:
                    continue
                score = overlap / max(len(q_tokens), 1)
                scored.append((score, path, para))
        scored.sort(reverse=True, key=lambda x: x[0])
        return [
            RAGResult(
                source=str(path),
                excerpt=para[:200],
                score=min(score, 1.0),
                rule_id=self._extract_rule_id(para),
            )
            for score, path, para in scored[:top_k]
        ]

    @staticmethod
    def _extract_rule_id(text: str) -> str | None:
        m = re.search(r"ROE-\d+", text)
        return m.group(0) if m else None


_WORD = re.compile(r"[a-zA-ZğüşıöçĞÜŞİÖÇ]+")


def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text) if len(w) > 2]
