-- Core platform tables: tenancy, config history, signals, incidents, their timeline, the job queue and the audit log.
-- Applied by `sentinel-migrate` into the platform schema (OPS_SENTINEL by default), never into the v1 SENTINEL schema.
-- Ids are application-generated ULIDs (VARCHAR(26)). JSON documents are stored as VARCHAR and read with Exasol's
-- JSON functions. Exasol has no UNIQUE constraint, so uniqueness beyond the primary key is enforced by single-statement
-- INSERT ... WHERE NOT EXISTS under Exasol's serializable transactions.

CREATE TABLE IF NOT EXISTS WORKSPACES (
    WORKSPACE_ID   VARCHAR(100)  NOT NULL PRIMARY KEY,
    DISPLAY_NAME   VARCHAR(200),
    CREATED_AT     TIMESTAMP     NOT NULL
);

CREATE TABLE IF NOT EXISTS CONFIG_VERSIONS (
    CONFIG_ID      VARCHAR(26)   NOT NULL PRIMARY KEY,
    WORKSPACE_ID   VARCHAR(100)  NOT NULL,
    CHECKSUM       VARCHAR(64)   NOT NULL,
    SPEC_YAML      VARCHAR(2000000) NOT NULL, -- as written, secret references unresolved
    APPLIED_BY     VARCHAR(200)  NOT NULL,
    APPLIED_AT     TIMESTAMP     NOT NULL
);

CREATE TABLE IF NOT EXISTS SIGNALS (
    SIGNAL_ID      VARCHAR(26)   NOT NULL PRIMARY KEY,
    WORKSPACE_ID   VARCHAR(100)  NOT NULL,
    INCIDENT_ID    VARCHAR(26),
    SOURCE_NAME    VARCHAR(100)  NOT NULL,
    FINGERPRINT    VARCHAR(64)   NOT NULL,
    SEVERITY       VARCHAR(10)   NOT NULL,
    SUMMARY        VARCHAR(2000) NOT NULL,
    LABELS         VARCHAR(20000),
    PAYLOAD        VARCHAR(2000000),
    IS_RESOLVED    BOOLEAN       NOT NULL,
    OBSERVED_AT    TIMESTAMP     NOT NULL,
    RECEIVED_AT    TIMESTAMP     NOT NULL
);

CREATE TABLE IF NOT EXISTS INCIDENTS (
    INCIDENT_ID    VARCHAR(26)   NOT NULL PRIMARY KEY,
    WORKSPACE_ID   VARCHAR(100)  NOT NULL,
    ENVIRONMENT    VARCHAR(100)  NOT NULL,
    FINGERPRINT    VARCHAR(64)   NOT NULL,
    TITLE          VARCHAR(500)  NOT NULL,
    SEVERITY       VARCHAR(10)   NOT NULL,
    STATUS         VARCHAR(20)   NOT NULL,
    SERVICE        VARCHAR(200),
    SOURCE_NAME    VARCHAR(100),
    LABELS         VARCHAR(20000),
    SIGNAL_COUNT   DECIMAL(18,0) NOT NULL,
    SUMMARY        VARCHAR(20000), -- the latest triage / investigation summary
    OPENED_AT      TIMESTAMP     NOT NULL,
    UPDATED_AT     TIMESTAMP     NOT NULL,
    RESOLVED_AT    TIMESTAMP,
    ROW_VERSION    DECIMAL(18,0) NOT NULL -- optimistic concurrency: every update checks and bumps it
);

-- Everything that happened to an incident, in order: status changes, agent steps, notifications, decisions.
CREATE TABLE IF NOT EXISTS INCIDENT_EVENTS (
    EVENT_ID       VARCHAR(26)   NOT NULL PRIMARY KEY,
    INCIDENT_ID    VARCHAR(26)   NOT NULL,
    WORKSPACE_ID   VARCHAR(100)  NOT NULL,
    KIND           VARCHAR(50)   NOT NULL,
    ACTOR          VARCHAR(200)  NOT NULL,
    DETAIL         VARCHAR(2000000),
    CREATED_AT     TIMESTAMP     NOT NULL
);

CREATE TABLE IF NOT EXISTS JOBS (
    JOB_ID          VARCHAR(26)   NOT NULL PRIMARY KEY,
    QUEUE_NAME      VARCHAR(50)   NOT NULL,
    KIND            VARCHAR(100)  NOT NULL,
    WORKSPACE_ID    VARCHAR(100),
    PAYLOAD         VARCHAR(2000000) NOT NULL,
    IDEMPOTENCY_KEY VARCHAR(200),
    STATUS          VARCHAR(20)   NOT NULL, -- queued | running | done | dead
    ATTEMPTS        DECIMAL(5,0)  NOT NULL,
    MAX_ATTEMPTS    DECIMAL(5,0)  NOT NULL,
    RUN_AFTER       TIMESTAMP     NOT NULL,
    LEASE_OWNER     VARCHAR(200),
    LEASE_UNTIL     TIMESTAMP,
    LAST_ERROR      VARCHAR(4000),
    CREATED_AT      TIMESTAMP     NOT NULL,
    FINISHED_AT     TIMESTAMP
);

-- Append-only. The runtime user is granted SELECT and INSERT only (see sql/platform_roles.sql).
CREATE TABLE IF NOT EXISTS AUDIT_EVENTS (
    AUDIT_ID       VARCHAR(26)   NOT NULL PRIMARY KEY,
    WORKSPACE_ID   VARCHAR(100),
    ACTOR          VARCHAR(200)  NOT NULL,
    ACTION_NAME    VARCHAR(100)  NOT NULL,
    SUBJECT_TYPE   VARCHAR(50),
    SUBJECT_ID     VARCHAR(100),
    DETAIL         VARCHAR(2000000),
    CREATED_AT     TIMESTAMP     NOT NULL
);
