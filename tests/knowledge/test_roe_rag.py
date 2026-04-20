"""ROE RAG tests (keyword fallback path)."""
from __future__ import annotations

from pathlib import Path

from services.knowledge.roe_rag import ROERAG


def test_loads_default_docs():
    rag = ROERAG()
    # docs/roe/ altında en az bir md olmalı
    assert len(rag._documents) >= 1


def test_query_finds_relevant_paragraph():
    rag = ROERAG()
    results = rag.query("sivil havalimanı ENGAGE otomatik")
    assert len(results) >= 1
    # İçinde "ROE-2" veya "sivil" geçmeli
    top = results[0]
    assert "ROE-2" in top.excerpt or "sivil" in top.excerpt.lower()


def test_query_empty_directory_returns_nothing(tmp_path):
    rag = ROERAG(roe_dir=tmp_path)
    assert rag.query("anything") == []


def test_rule_id_extraction():
    rag = ROERAG()
    assert rag._extract_rule_id("See ROE-42 for details") == "ROE-42"
    assert rag._extract_rule_id("no rule here") is None
