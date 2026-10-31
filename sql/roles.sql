-- Ops Sentinel database roles. Review, set real passwords, then run as a DBA user (never as the app itself):
--   exapump sql -p starter-kit < sql/roles.sql
--
-- Three identities, three privilege sets, so no single credential can both approve and rewrite history:
--   mcp_readonly      investigation over MCP: SELECT only
--   sentinel_engine   incident bookkeeping and the audit trail: no access to business data
--   sentinel_executor runs approved remediation SQL: DML on business tables only, no access to Sentinel tables
-- The guardrail in engine/sentinel/guardrail.py is the first control; these grants are the second, so a guardrail
-- bug still cannot touch the audit trail.

-- 1. Read-only investigation user (matches EXA_USER) -------------------------
CREATE USER mcp_readonly IDENTIFIED BY "CHANGE_ME_readonly";
GRANT CREATE SESSION TO mcp_readonly;
GRANT SELECT ANY TABLE TO mcp_readonly;

-- 2. Engine user (matches EXASOL_ADMIN_USER) ----------------------------------
CREATE USER sentinel_engine IDENTIFIED BY "CHANGE_ME_engine";
GRANT CREATE SESSION TO sentinel_engine;
GRANT SELECT ON TABLE SENTINEL.PIPELINE_LOGS TO sentinel_engine;
GRANT SELECT ON TABLE SENTINEL.QUERY_METRICS TO sentinel_engine;
GRANT SELECT ON TABLE SENTINEL.DATA_LINEAGE TO sentinel_engine;
GRANT SELECT, INSERT, UPDATE ON TABLE SENTINEL.INCIDENTS TO sentinel_engine;
-- Append-only audit trail: no UPDATE and no DELETE, so a recorded action cannot be rewritten.
GRANT SELECT, INSERT ON TABLE SENTINEL.REMEDIATION_ACTIONS TO sentinel_engine;

-- 3. Executor user (matches EXASOL_EXECUTOR_USER) -------------------------------
CREATE USER sentinel_executor IDENTIFIED BY "CHANGE_ME_executor";
GRANT CREATE SESSION TO sentinel_executor;
-- Grant DML on each business table Sentinel may repair, and only those. The demo's table is shown; add your own.
-- Never grant anything on the five Sentinel tables, or on a whole schema.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE SENTINEL.SALES_CLEAN TO sentinel_executor;
