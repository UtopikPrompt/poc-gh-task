CREATE TABLE IF NOT EXISTS langgraph_service.agent_config (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    completion_timeout_seconds SMALLINT NOT NULL
        CHECK (completion_timeout_seconds BETWEEN 1 AND 90),
    version INTEGER NOT NULL CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by TEXT NOT NULL
);

ALTER TABLE langgraph_service.agent_config OWNER TO langgraph_service_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON langgraph_service.agent_config
    TO langgraph_service_runtime;