ALTER TABLE langgraph_service.inference_instance_config
    ADD COLUMN workload_status TEXT NOT NULL DEFAULT 'ready'
        CHECK (workload_status IN ('provisioning', 'ready', 'deleting', 'failed', 'deleted')),
    ADD COLUMN workload_error TEXT CHECK (
        workload_error IS NULL OR workload_error =
            'Inference workload reconciliation failed; check the inference controller and retry.'
    );

CREATE TABLE langgraph_service.inference_workload_outbox (
    event_id UUID PRIMARY KEY,
    instance_id TEXT NOT NULL
        REFERENCES langgraph_service.inference_instance_config(instance_id),
    operation TEXT NOT NULL CHECK (operation IN ('ensure', 'delete')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'succeeded')),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    completed_lease_token UUID,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (status = 'leased' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
        OR (status = 'pending' AND lease_token IS NULL AND lease_until IS NULL)
        OR (status = 'succeeded' AND lease_token IS NULL AND lease_until IS NULL
            AND completed_lease_token IS NOT NULL)
    )
);

CREATE INDEX inference_workload_claim_idx
    ON langgraph_service.inference_workload_outbox (created_at, event_id)
    WHERE status IN ('pending', 'leased');

INSERT INTO langgraph_service.inference_workload_outbox (event_id, instance_id, operation)
SELECT gen_random_uuid(), instance_id, 'ensure'
FROM langgraph_service.inference_instance_config
WHERE is_active;

UPDATE langgraph_service.inference_instance_config
SET workload_status = 'provisioning'
WHERE is_active;

ALTER TABLE langgraph_service.inference_workload_outbox OWNER TO langgraph_service_owner;
GRANT SELECT, INSERT, UPDATE ON langgraph_service.inference_workload_outbox
    TO langgraph_service_runtime;
GRANT SELECT, UPDATE ON langgraph_service.inference_instance_config
    TO langgraph_service_runtime;