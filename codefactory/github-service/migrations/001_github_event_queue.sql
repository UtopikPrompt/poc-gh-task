CREATE SCHEMA IF NOT EXISTS github_service;

CREATE TABLE IF NOT EXISTS github_service.webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('processing', 'accepted', 'failed')),
    response JSONB,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS github_service.task_event_inbox (
    event_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES github_service.webhook_deliveries(delivery_id),
    ordinal INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('ready', 'ci', 'merged')),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (delivery_id, ordinal)
);

CREATE TABLE IF NOT EXISTS github_service.task_event_outbox (
    event_id TEXT PRIMARY KEY REFERENCES github_service.task_event_inbox(event_id),
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'processing', 'processed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_until TIMESTAMPTZ,
    claim_token TEXT,
    last_error TEXT,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS task_event_outbox_ready
    ON github_service.task_event_outbox(state, available_at, lease_until);

CREATE TABLE IF NOT EXISTS github_service.pull_request_tasks (
    repository TEXT NOT NULL,
    pull_request_number INTEGER NOT NULL,
    pull_request_id TEXT,
    task_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (repository, pull_request_number)
);

CREATE TABLE IF NOT EXISTS github_service.task_contexts (
    task_id TEXT PRIMARY KEY,
    project_node_id TEXT NOT NULL,
    project_item_id TEXT NOT NULL,
    target_repository TEXT NOT NULL
);
