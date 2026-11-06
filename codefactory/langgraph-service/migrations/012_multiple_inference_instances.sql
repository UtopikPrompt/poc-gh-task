ALTER TABLE langgraph_service.inference_instance_config
    DROP CONSTRAINT inference_instance_config_instance_id_check,
    ADD CONSTRAINT inference_instance_config_instance_id_check
        CHECK (instance_id ~ '^(gpu|cpu)(-[a-z0-9]([a-z0-9-]{0,57}[a-z0-9])?)?$');

ALTER TABLE langgraph_service.inference_model_rollout_outbox
    DROP CONSTRAINT inference_model_rollout_outbox_instance_id_check,
    ADD CONSTRAINT inference_model_rollout_outbox_instance_id_check
        CHECK (instance_id ~ '^(gpu|cpu)(-[a-z0-9]([a-z0-9-]{0,57}[a-z0-9])?)?$');