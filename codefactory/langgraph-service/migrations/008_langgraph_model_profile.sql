ALTER TABLE langgraph_service.agent_config
    ADD COLUMN model_profile TEXT NOT NULL DEFAULT 'qwen2_5_32b_gpu'
    CHECK (model_profile IN ('qwen2_5_32b_gpu', 'qwen2_5_0_5b_cpu'));