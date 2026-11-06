ALTER TABLE langgraph_service.inference_instance_config
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX inference_instance_active_idx
    ON langgraph_service.inference_instance_config (instance_id)
    WHERE is_active;