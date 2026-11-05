CREATE TABLE IF NOT EXISTS langgraph_service.agent_roster (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    roster_document JSONB,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    updated_at TIMESTAMPTZ,
    updated_by TEXT,
    CHECK (
        (revision = 0 AND roster_document IS NULL AND updated_at IS NULL AND updated_by IS NULL)
        OR
        (revision > 0 AND roster_document IS NOT NULL
            AND jsonb_typeof(roster_document) = 'object'
            AND updated_at IS NOT NULL AND updated_by IS NOT NULL)
    )
);

INSERT INTO langgraph_service.agent_roster (id, revision)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS langgraph_service.task_agent_roster_snapshot (
    task_id TEXT PRIMARY KEY CHECK (length(task_id) BETWEEN 1 AND 200),
    roster_revision INTEGER NOT NULL CHECK (roster_revision > 0),
    profile_snapshot JSONB NOT NULL CHECK (jsonb_typeof(profile_snapshot) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE langgraph_service.agent_roster OWNER TO langgraph_service_owner;
ALTER TABLE langgraph_service.task_agent_roster_snapshot
    OWNER TO langgraph_service_owner;
GRANT SELECT, UPDATE ON langgraph_service.agent_roster
    TO langgraph_service_runtime;
GRANT SELECT, INSERT ON langgraph_service.task_agent_roster_snapshot
    TO langgraph_service_runtime;