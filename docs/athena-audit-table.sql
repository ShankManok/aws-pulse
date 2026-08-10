-- Athena DDL for Pulse audit trail query layer
-- Creates the external table pointing to S3 audit exports (JSON Lines format)
-- Partitioned by year/month/day/hour using Hive-style paths

CREATE DATABASE IF NOT EXISTS pulse_audit;

CREATE EXTERNAL TABLE IF NOT EXISTS pulse_audit.delivery_records (
    deliveryId      STRING,
    signalId        STRING,
    personaId       STRING,
    recipientId     STRING,
    channel         STRING,
    contentVersion  STRING,
    deliveredAt     STRING,
    acknowledgedAt  STRING,
    escalated       BOOLEAN,
    escalatedAt     STRING,
    feedback        STRING,
    actionAt        STRING
)
PARTITIONED BY (
    year  STRING,
    month STRING,
    day   STRING,
    hour  STRING
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES (
    'ignore.malformed.json' = 'true'
)
STORED AS TEXTFILE
LOCATION 's3://${AUDIT_BUCKET}/audit/'
TBLPROPERTIES ('has_encrypted_data'='true');

-- After adding new partitions (hourly exports), run:
-- MSCK REPAIR TABLE pulse_audit.delivery_records;

-- Example queries:

-- Total deliveries in a date range
-- SELECT COUNT(*) FROM pulse_audit.delivery_records
-- WHERE year = '2026' AND month = '08' AND day = '10';

-- Acknowledged vs unacknowledged
-- SELECT
--   personaId,
--   COUNT(*) AS total,
--   COUNT(acknowledgedAt) AS acknowledged,
--   COUNT(*) - COUNT(acknowledgedAt) AS unacknowledged
-- FROM pulse_audit.delivery_records
-- WHERE year = '2026' AND month = '08'
-- GROUP BY personaId;

-- Escalation rate by persona
-- SELECT
--   personaId,
--   COUNT(*) AS total,
--   SUM(CASE WHEN escalated = true THEN 1 ELSE 0 END) AS escalated_count,
--   ROUND(SUM(CASE WHEN escalated = true THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS escalation_pct
-- FROM pulse_audit.delivery_records
-- WHERE year = '2026'
-- GROUP BY personaId;
