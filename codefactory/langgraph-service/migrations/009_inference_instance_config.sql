CREATE TABLE langgraph_service.inference_instance_config (
    instance_id TEXT PRIMARY KEY CHECK (instance_id IN ('gpu', 'cpu')),
    model_filename TEXT NOT NULL
        CHECK (model_filename ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,199}[.]gguf$'),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at TIMESTAMPTZ,
    updated_by TEXT,
    CHECK ((version = 0 AND updated_at IS NULL AND updated_by IS NULL) OR
           (version > 0 AND updated_at IS NOT NULL AND updated_by IS NOT NULL))
);

INSERT INTO langgraph_service.inference_instance_config (instance_id, model_filename)
VALUES
    ('gpu', 'qwen2.5-32b-instruct-q4_k_m.gguf'),
    ('cpu', 'qwen2.5-0.5b-instruct-q4_k_m.gguf');

ALTER TABLE langgraph_service.agent_config DROP COLUMN model_profile;

ALTER TABLE langgraph_service.inference_instance_config
    OWNER TO langgraph_service_owner;
GRANT SELECT, INSERT, UPDATE ON langgraph_service.inference_instance_config
    TO langgraph_service_runtime;