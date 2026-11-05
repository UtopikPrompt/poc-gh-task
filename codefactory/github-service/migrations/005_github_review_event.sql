ALTER TABLE github_service.task_event_inbox
    DROP CONSTRAINT IF EXISTS task_event_inbox_event_type_check;

ALTER TABLE github_service.task_event_inbox
    ADD CONSTRAINT task_event_inbox_event_type_check
    CHECK (event_type IN ('ready', 'ci', 'merged', 'review_changes_requested'));