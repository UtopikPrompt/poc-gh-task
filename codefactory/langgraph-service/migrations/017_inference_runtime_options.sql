ALTER TABLE langgraph_service.inference_instance_config
    ADD COLUMN context_size INTEGER NOT NULL DEFAULT 1024,
    ADD COLUMN threads INTEGER NOT NULL DEFAULT 2,
    ADD CONSTRAINT inference_instance_context_size_check CHECK (
        context_size >= 512
        AND context_size <= CASE WHEN instance_id LIKE 'cpu%' THEN 4096 ELSE 16384 END
    ),
    ADD CONSTRAINT inference_instance_threads_check CHECK (
        threads BETWEEN 1 AND CASE WHEN instance_id LIKE 'cpu%' THEN 4 ELSE 8 END
    );

UPDATE langgraph_service.inference_instance_config
SET context_size = 16384, threads = 8
WHERE instance_id LIKE 'gpu%';