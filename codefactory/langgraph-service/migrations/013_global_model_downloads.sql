ALTER TABLE langgraph_service.inference_model_download_request
    ALTER COLUMN instance_id DROP NOT NULL,
    ALTER COLUMN config_version SET DEFAULT 0;

CREATE UNIQUE INDEX inference_model_download_one_active_shared
    ON langgraph_service.inference_model_download_request ((true))
    WHERE instance_id IS NULL
        AND status IN ('queued', 'downloading', 'verifying');

UPDATE langgraph_service.inference_model_rollout_outbox
SET status = 'superseded', lease_token = NULL, lease_until = NULL, updated_at = now()
WHERE status IN ('pending', 'leased');

UPDATE langgraph_service.inference_instance_config
SET download_status = 'ready', current_request_id = NULL, download_error = NULL
WHERE download_status IN ('queued', 'downloading', 'verifying');

UPDATE langgraph_service.inference_instance_config
SET rollout_status = 'not_requested', rollout_request_id = NULL, rollout_error = NULL
WHERE rollout_status IN ('waiting_for_download', 'pending', 'rolling_out');