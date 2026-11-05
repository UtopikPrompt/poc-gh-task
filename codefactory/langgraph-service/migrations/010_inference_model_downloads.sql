ALTER TABLE langgraph_service.inference_instance_config
    ADD COLUMN model_url TEXT,
    ADD COLUMN model_sha256 TEXT
        CHECK (model_sha256 IS NULL OR model_sha256 ~ '^[A-Fa-f0-9]{64}$'),
    ADD COLUMN download_status TEXT NOT NULL DEFAULT 'ready'
        CHECK (download_status IN ('queued', 'downloading', 'verifying', 'ready', 'failed')),
    ADD COLUMN current_request_id UUID,
    ADD COLUMN download_error TEXT;

CREATE TABLE langgraph_service.inference_model_download_request (
    request_id UUID PRIMARY KEY,
    instance_id TEXT NOT NULL
        REFERENCES langgraph_service.inference_instance_config(instance_id),
    model_filename TEXT NOT NULL
        CHECK (model_filename ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,199}[.]gguf$'),
    model_url TEXT NOT NULL,
    model_sha256 TEXT NOT NULL CHECK (model_sha256 ~ '^[A-Fa-f0-9]{64}$'),
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'downloading', 'verifying', 'ready', 'failed')),
    public_error TEXT CHECK (
        public_error IS NULL OR public_error IN (
            'Model artifact SHA-256 did not match.',
            'Model artifact exceeds the 100 GiB size limit.',
            'Model download was rejected by URL or network safety policy.',
            'Model download failed; verify artifact availability and retry.'
        )
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (status IN ('downloading', 'verifying') AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status NOT IN ('downloading', 'verifying') AND lease_token IS NULL AND lease_until IS NULL)
    ),
    CHECK ((status = 'failed' AND public_error IS NOT NULL) OR
           (status <> 'failed' AND public_error IS NULL))
);

ALTER TABLE langgraph_service.inference_instance_config
    ADD CONSTRAINT inference_instance_current_request_fk
    FOREIGN KEY (current_request_id)
    REFERENCES langgraph_service.inference_model_download_request(request_id);

CREATE UNIQUE INDEX inference_model_download_one_active_per_instance
    ON langgraph_service.inference_model_download_request (instance_id)
    WHERE status IN ('queued', 'downloading', 'verifying');

CREATE INDEX inference_model_download_claim_idx
    ON langgraph_service.inference_model_download_request (created_at, request_id)
    WHERE status IN ('queued', 'downloading', 'verifying');

ALTER TABLE langgraph_service.inference_model_download_request
    OWNER TO langgraph_service_owner;
GRANT SELECT, INSERT, UPDATE ON langgraph_service.inference_model_download_request
    TO langgraph_service_runtime;
GRANT SELECT, UPDATE ON langgraph_service.inference_instance_config
    TO langgraph_service_runtime;