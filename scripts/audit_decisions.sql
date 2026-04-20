-- NIZAM Karar Audit Trail — PostgreSQL sorguları
--
-- Kullanım:
--   psql $NIZAM_DB_DSN -f scripts/audit_decisions.sql
-- veya tek sorgu:
--   psql $NIZAM_DB_DSN -c "SELECT ... "

-- ── Tablo yapısı (ilk çalıştırmada otomatik oluşur, llm_graph.finalize'de) ──
-- decisions (
--   id SERIAL,
--   track_id TEXT,
--   action TEXT,
--   threat_level TEXT,
--   confidence REAL,
--   reasoning TEXT,
--   source TEXT,
--   roe_reference TEXT,
--   requires_operator_approval BOOLEAN,
--   timestamp_iso TIMESTAMPTZ,
--   llm_provider TEXT,
--   llm_model TEXT,
--   llm_raw_response JSONB,
--   guardrails_triggered TEXT[]
-- );

-- ══ Son 1 saatteki tüm kararlar ══
SELECT
    track_id,
    action,
    threat_level,
    confidence,
    source,
    roe_reference,
    requires_operator_approval AS requires_approval,
    guardrails_triggered,
    timestamp_iso
FROM decisions
WHERE timestamp_iso >= NOW() - INTERVAL '1 hour'
ORDER BY timestamp_iso DESC
LIMIT 50;

-- ══ Bir track'in tüm geçmişi (kararlar nasıl değişti) ══
-- :track_id parametresini değiştir
SELECT
    timestamp_iso,
    action,
    threat_level,
    confidence,
    source,
    reasoning,
    guardrails_triggered
FROM decisions
WHERE track_id = 't-abc-123'
ORDER BY timestamp_iso ASC;

-- ══ ENGAGE kararları (hepsi operatör onayı istemiş mi?) ══
SELECT
    track_id, timestamp_iso, threat_level, confidence,
    requires_operator_approval,
    llm_provider, llm_model,
    reasoning
FROM decisions
WHERE action = 'engage'
ORDER BY timestamp_iso DESC;
-- GÜVENLİK: requires_operator_approval FALSE olan ENGAGE çıkarsa ALARM

-- ══ Guardrails istatistiği — hangileri ne kadar tetikleniyor ══
SELECT
    unnest(guardrails_triggered) AS guardrail,
    COUNT(*) AS trigger_count
FROM decisions
WHERE guardrails_triggered IS NOT NULL AND cardinality(guardrails_triggered) > 0
GROUP BY guardrail
ORDER BY trigger_count DESC;

-- ══ LLM vs Rule Engine karar oranı ══
SELECT
    source,
    COUNT(*) AS n,
    AVG(confidence) AS avg_conf
FROM decisions
WHERE timestamp_iso >= NOW() - INTERVAL '1 day'
GROUP BY source;

-- ══ Anthropic vs Ollama başarı oranı (fallback kullanım metriği) ══
SELECT
    llm_provider,
    llm_model,
    COUNT(*) AS n
FROM decisions
WHERE llm_provider IS NOT NULL
GROUP BY llm_provider, llm_model;

-- ══ LLM raw response inspector — bir karara bakıp "LLM tam ne dedi" ══
-- :decision_id değiştir
SELECT
    track_id, action, threat_level, confidence,
    reasoning,
    jsonb_pretty(llm_raw_response) AS llm_ham_response,
    guardrails_triggered
FROM decisions
WHERE id = 42;

-- ══ Son 7 gün ENGAGE kararları kimin tarafından onaylandı? ══
-- (approvals tablosu ayrı, bu örnek)
-- SELECT d.track_id, d.timestamp_iso, a.approved_by, a.approved_at
-- FROM decisions d
-- LEFT JOIN approvals a ON a.decision_id = d.id
-- WHERE d.action = 'engage'
--   AND d.timestamp_iso >= NOW() - INTERVAL '7 days';
