ALTER TABLE langgraph_service.inference_model_download_request
    ADD COLUMN config_version INTEGER NOT NULL DEFAULT 0 CHECK (config_version >= 0);

UPDATE langgraph_service.inference_model_download_request AS request
SET config_version = config.version
FROM langgraph_service.inference_instance_config AS config
WHERE config.current_request_id = request.request_id;

ALTER TABLE langgraph_service.inference_instance_config
    ADD COLUMN rollout_status TEXT NOT NULL DEFAULT 'not_requested'
        CHECK (rollout_status IN (
            'not_requested', 'waiting_for_download', 'pending', 'rolling_out', 'ready', 'failed'
        )),
    ADD COLUMN rollout_request_id UUID,
    ADD COLUMN rollout_error TEXT CHECK (
        rollout_error IS NULL OR rollout_error =
            'Model rollout failed; verify the inference deployment and retry.'
    ),
    ADD CONSTRAINT inference_instance_rollout_request_fk
        FOREIGN KEY (rollout_request_id)
        REFERENCES langgraph_service.inference_model_download_request(request_id);

CREATE TABLE langgraph_service.inference_model_rollout_outbox (
    event_id UUID PRIMARY KEY,
    download_request_id UUID NOT NULL UNIQUE
        REFERENCES langgraph_service.inference_model_download_request(request_id),
    instance_id TEXT NOT NULL CHECK (instance_id IN ('gpu', 'cpu')),
    model_filename TEXT NOT NULL
        CHECK (model_filename ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,199}[.]gguf$'),
    config_version INTEGER NOT NULL CHECK (config_version >= 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'succeeded', 'failed', 'superseded')),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (status = 'leased' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status IN ('succeeded', 'failed') AND lease_token IS NOT NULL AND lease_until IS NULL)
        OR
        (status IN ('pending', 'superseded') AND lease_token IS NULL AND lease_until IS NULL)
    )
);

INSERT INTO langgraph_service.inference_model_rollout_outbox (
        event_id, download_request_id, instance_id, model_filename, config_version,
        idempotency_key
)
SELECT request.request_id, request.request_id, request.instance_id,
             request.model_filename, request.config_version,
    concat(request.instance_id, ':', request.config_version, ':', request.request_id)
FROM langgraph_service.inference_model_download_request AS request
JOIN langgraph_service.inference_instance_config AS config
    ON config.instance_id = request.instance_id
 AND config.current_request_id = request.request_id
 AND config.model_filename = request.model_filename
 AND config.version = request.config_version
WHERE request.status = 'ready' AND config.download_status = 'ready'
ON CONFLICT DO NOTHING;

UPDATE langgraph_service.inference_instance_config AS config
SET rollout_status = 'pending', rollout_request_id = config.current_request_id
WHERE config.download_status = 'ready'
    AND EXISTS (
            SELECT 1 FROM langgraph_service.inference_model_rollout_outbox AS event
            WHERE event.download_request_id = config.current_request_id
                AND event.instance_id = config.instance_id
                AND event.config_version = config.version
                AND event.model_filename = config.model_filename
    );

CREATE INDEX inference_model_rollout_claim_idx
    ON langgraph_service.inference_model_rollout_outbox (created_at, event_id)
    WHERE status IN ('pending', 'leased');

ALTER TABLE langgraph_service.inference_model_rollout_outbox
    OWNER TO langgraph_service_owner;
GRANT SELECT, INSERT, UPDATE ON langgraph_service.inference_model_rollout_outbox
    TO langgraph_service_runtime;
GRANT SELECT, UPDATE ON langgraph_service.inference_model_download_request
    TO langgraph_service_runtime;
GRANT SELECT, UPDATE ON langgraph_service.inference_instance_config
    TO langgraph_service_runtime;