ALTER TABLE github_service.task_contexts
    ADD COLUMN IF NOT EXISTS project_owner TEXT,
    ADD COLUMN IF NOT EXISTS project_owner_type TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'task_context_project_owner_type'
          AND conrelid = 'github_service.task_contexts'::regclass
    ) THEN
        ALTER TABLE github_service.task_contexts
            ADD CONSTRAINT task_context_project_owner_type
            CHECK (
                project_owner_type IS NULL
                OR project_owner_type IN ('organization', 'user')
            );
    END IF;
END
$$;