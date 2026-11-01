DO $$
BEGIN
    CREATE ROLE github_service_owner NOLOGIN;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    CREATE ROLE github_service_runtime NOLOGIN;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    CREATE ROLE langgraph_service_owner NOLOGIN;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    CREATE ROLE langgraph_service_runtime NOLOGIN;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

ALTER SCHEMA github_service OWNER TO github_service_owner;
ALTER TABLE github_service.webhook_deliveries OWNER TO github_service_owner;
ALTER TABLE github_service.task_event_inbox OWNER TO github_service_owner;
ALTER TABLE github_service.task_event_outbox OWNER TO github_service_owner;
ALTER TABLE github_service.pull_request_tasks OWNER TO github_service_owner;
ALTER TABLE github_service.task_contexts OWNER TO github_service_owner;

GRANT USAGE ON SCHEMA github_service TO github_service_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA github_service
    TO github_service_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE github_service_owner IN SCHEMA github_service
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO github_service_runtime;

ALTER SCHEMA langgraph_service OWNER TO langgraph_service_owner;
GRANT USAGE, CREATE ON SCHEMA langgraph_service TO langgraph_service_runtime;
ALTER ROLE langgraph_service_runtime SET search_path TO langgraph_service;
ALTER DEFAULT PRIVILEGES FOR ROLE langgraph_service_runtime IN SCHEMA langgraph_service
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO langgraph_service_runtime;
