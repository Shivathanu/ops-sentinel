-- Ops Sentinel — Module A schema
-- Run with: exapump sql -p starter-kit -f db/schema.sql

CREATE SCHEMA IF NOT EXISTS SENTINEL;
OPEN SCHEMA SENTINEL;

-- 1. Pipeline execution history -------------------------------------------
CREATE TABLE IF NOT EXISTS PIPELINE_LOGS (
    LOG_ID              DECIMAL(18,0) IDENTITY PRIMARY KEY,
    PIPELINE_ID         VARCHAR(100)  NOT NULL,
    PIPELINE_NAME       VARCHAR(200),
    SOURCE_TABLE        VARCHAR(200),
    TARGET_TABLE        VARCHAR(200),
    EXECUTION_TS        TIMESTAMP     NOT NULL,
    STATUS              VARCHAR(20)   NOT NULL,  -- SUCCESS | FAILED | RUNNING
    DURATION_SECONDS    DECIMAL(10,2),
    ROWS_PROCESSED      DECIMAL(18,0),
    ERROR_STACK         VARCHAR(10000)
);

-- 2. Query / compute cost metrics ------------------------------------------
CREATE TABLE IF NOT EXISTS QUERY_METRICS (
    QUERY_ID            DECIMAL(18,0) IDENTITY PRIMARY KEY,
    USER_ID             VARCHAR(100),
    TABLE_NAME          VARCHAR(200),
    QUERY_TEXT          VARCHAR(10000),
    EXECUTED_TS         TIMESTAMP     NOT NULL,
    COMPUTE_TIME_MS     DECIMAL(18,2),
    CPU_SECONDS         DECIMAL(18,2),
    ROWS_SCANNED        DECIMAL(18,0),
    ESTIMATED_COST_USD  DECIMAL(10,4),
    IS_DUPLICATE        BOOLEAN DEFAULT FALSE
);

-- 3. Table-to-dashboard lineage ---------------------------------------------
CREATE TABLE IF NOT EXISTS DATA_LINEAGE (
    LINEAGE_ID          DECIMAL(18,0) IDENTITY PRIMARY KEY,
    SOURCE_TABLE        VARCHAR(200) NOT NULL,
    TARGET_TABLE        VARCHAR(200) NOT NULL,
    DOWNSTREAM_DASHBOARD VARCHAR(200),
    OWNER_TEAM          VARCHAR(100),
    CRITICALITY         VARCHAR(20)  -- LOW | MEDIUM | HIGH | CRITICAL
);

-- 4. Incident lifecycle (created by the engine, not seeded) -----------------
CREATE TABLE IF NOT EXISTS INCIDENTS (
    INCIDENT_ID         DECIMAL(18,0) IDENTITY PRIMARY KEY,
    SOURCE_TYPE         VARCHAR(20)   NOT NULL, -- PIPELINE_FAILURE | COST_SPIKE
    SOURCE_REF_ID       DECIMAL(18,0) NOT NULL, -- LOG_ID or QUERY_ID
    CREATED_TS          TIMESTAMP     NOT NULL,
    STATUS              VARCHAR(30)   NOT NULL, -- DETECTED | TRIAGED | AWAITING_APPROVAL | APPROVED | REJECTED | REMEDIATED | FAILED
    BUSINESS_IMPACT      VARCHAR(4000),
    ROOT_CAUSE          VARCHAR(4000),
    SUGGESTED_FIX_SQL    VARCHAR(10000),
    RISK_LEVEL          VARCHAR(20),
    SLACK_MESSAGE_TS    VARCHAR(50),
    RESOLVED_TS         TIMESTAMP
);

-- 5. Audit trail of every write Ops Sentinel ever performs -------------------
CREATE TABLE IF NOT EXISTS REMEDIATION_ACTIONS (
    ACTION_ID           DECIMAL(18,0) IDENTITY PRIMARY KEY,
    INCIDENT_ID         DECIMAL(18,0) NOT NULL,
    EXECUTED_SQL        VARCHAR(10000) NOT NULL,
    EXECUTED_BY         VARCHAR(100),   -- Slack user who clicked Approve
    EXECUTED_TS         TIMESTAMP      NOT NULL,
    OUTCOME             VARCHAR(20),    -- SUCCESS | FAILED
    OUTCOME_DETAIL      VARCHAR(4000)
);
