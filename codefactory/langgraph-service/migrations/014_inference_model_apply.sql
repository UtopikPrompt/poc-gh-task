ALTER TABLE langgraph_service.inference_model_rollout_outbox
    DROP CONSTRAINT inference_model_rollout_outbox_download_request_id_fkey;

ALTER TABLE langgraph_service.inference_model_rollout_outbox
    RENAME COLUMN download_request_id TO rollout_request_id;

ALTER TABLE langgraph_service.inference_model_rollout_outbox
    RENAME CONSTRAINT inference_model_rollout_outbox_download_request_id_key
        TO inference_model_rollout_outbox_rollout_request_id_key;

ALTER TABLE langgraph_service.inference_instance_config
    DROP CONSTRAINT inference_instance_rollout_request_fk,
    ADD CONSTRAINT inference_instance_rollout_request_fk
        FOREIGN KEY (rollout_request_id)
        REFERENCES langgraph_service.inference_model_rollout_outbox(rollout_request_id);