ALTER TABLE github_service.task_contexts
    ADD COLUMN IF NOT EXISTS installation_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'task_context_installation_positive'
          AND conrelid = 'github_service.task_contexts'::regclass
    ) THEN
        ALTER TABLE github_service.task_contexts
            ADD CONSTRAINT task_context_installation_positive
            CHECK (installation_id IS NULL OR installation_id > 0);
    END IF;
END
$$;