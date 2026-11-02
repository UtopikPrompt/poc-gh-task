const message = document.getElementById('message');
const body = document.getElementById('workflows');
const configMessage = document.getElementById('config-message');
const configAudit = document.getElementById('config-audit');
const timeoutInput = document.getElementById('completion-timeout');
const saveConfig = document.getElementById('save-config');
const executionMessage = document.getElementById('execution-message');
const executionsBody = document.getElementById('executions');
const executionDetail = document.getElementById('execution-detail');

const cell = (row, value) => {
    const element = row.insertCell();
    element.textContent = value == null ? 'Unavailable' : String(value);
    return element;
};

async function refresh() {
    message.textContent = 'Loading workflow state...';
    body.replaceChildren();
    try {
        const response = await fetch('/api/workflows', { headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const data = await response.json();
        for (const workflow of data.workflows) {
            const row = body.insertRow();
            cell(row, workflow.task_id);
            cell(row, workflow.repository);
            cell(row, workflow.issue_number == null ? null : `#${workflow.issue_number}`);
            cell(row, workflow.pull_request_number == null ? null : `#${workflow.pull_request_number}`);
            cell(row, workflow.latest_event);
            const status = cell(row, workflow.has_failure ? `${workflow.status} (failure recorded)` : workflow.status);
            status.className = `status${workflow.status === 'failed' ? ' failed' : ''}`;
            cell(row, workflow.latest_event_at);
        }
        message.textContent = data.workflows.length ? `${data.workflows.length} workflow(s)` : 'No workflow records are available yet.';
    } catch {
        message.textContent = 'Workflow state is unavailable.';
    }
}

async function loadAgentConfig() {
    configMessage.textContent = 'Loading configuration...';
    try {
        const response = await fetch('/api/agent-config', { headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        timeoutInput.value = record.config.completion_timeout_seconds;
        timeoutInput.disabled = !record.can_edit;
        saveConfig.disabled = !record.can_edit;
        configMessage.textContent = record.can_edit ? 'Organization owner access verified.' : 'Organization owner access is required to save changes.';
        configAudit.textContent = record.version
            ? `Version ${record.version} | Updated ${record.updated_at} by ${record.updated_by}`
            : 'Using the runtime default. No saved override.';
    } catch {
        timeoutInput.disabled = true;
        saveConfig.disabled = true;
        configMessage.textContent = 'Agent configuration is unavailable.';
        configAudit.textContent = '';
    }
}

async function saveAgentConfig(event) {
    event.preventDefault();
    configMessage.textContent = 'Saving configuration...';
    try {
        const response = await fetch('/api/agent-config', {
            method: 'PUT',
            headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({ completion_timeout_seconds: Number(timeoutInput.value) }),
        });
        if (!response.ok) throw new Error('unavailable');
        configMessage.textContent = 'Configuration saved for new workflow runs.';
        await loadAgentConfig();
    } catch {
        configMessage.textContent = 'Configuration could not be saved.';
    }
}

async function showExecution(threadId) {
    executionDetail.replaceChildren();
    try {
        const response = await fetch(`/api/executions/${encodeURIComponent(threadId)}`, { headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const detail = await response.json();
        const heading = document.createElement('h3');
        heading.textContent = `Checkpoint history | ${detail.thread_id}`;
        const list = document.createElement('ol');
        for (const checkpoint of detail.checkpoints) {
            const item = document.createElement('li');
            const state = [checkpoint.workflow_status, checkpoint.waiting_for, checkpoint.created_at, checkpoint.step == null ? null : `step ${checkpoint.step}`]
                .filter(Boolean)
                .join(' | ');
            item.textContent = state || 'Checkpoint state unavailable';
            list.append(item);
        }
        executionDetail.append(heading, list);
    } catch {
        executionDetail.textContent = 'Checkpoint details are unavailable.';
    }
}

async function refreshExecutions() {
    executionMessage.textContent = 'Loading executions...';
    executionsBody.replaceChildren();
    try {
        const response = await fetch('/api/executions', { headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const data = await response.json();
        for (const execution of data.executions) {
            const row = executionsBody.insertRow();
            const taskCell = row.insertCell();
            const taskButton = document.createElement('button');
            taskButton.type = 'button';
            taskButton.className = 'task-link';
            taskButton.textContent = execution.thread_id;
            taskButton.addEventListener('click', () => showExecution(execution.thread_id));
            taskCell.append(taskButton);
            cell(row, execution.workflow_status);
            cell(row, execution.waiting_for);
            cell(row, execution.created_at);
            cell(row, execution.step);
        }
        executionMessage.textContent = data.executions.length
            ? `${data.executions.length} LangGraph execution(s)`
            : 'No LangGraph executions are available yet.';
    } catch {
        executionMessage.textContent = 'LangGraph execution state is unavailable.';
    }
}

document.getElementById('refresh').addEventListener('click', () => {
    refresh();
    refreshExecutions();
    loadAgentConfig();
});
document.getElementById('agent-config-form').addEventListener('submit', saveAgentConfig);
refresh();
refreshExecutions();
loadAgentConfig();