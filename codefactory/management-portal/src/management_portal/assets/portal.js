const message = document.getElementById('message');
const body = document.getElementById('workflows');
const executionMessage = document.getElementById('execution-message');
const executionsBody = document.getElementById('executions');
const executionDetail = document.getElementById('execution-detail');
const liveStatus = document.getElementById('live-status');
const refreshButton = document.getElementById('refresh');
const refreshIntervalMs = 5000;
let canRetryWorkflows = false;
let canResolveNoCI = false;
let refreshPromise = null;
let selectedExecutionThread = null;
let workflowsByTaskId = new Map();

const workflowFlowNodes = [
    ['dispatch', 'Dispatch', 'Route the incoming event to its lifecycle path.'],
    ['validate_task', 'Validate task', 'Check readiness and target repository.'],
    ['context', 'Gather context', 'Collect repository and issue context.'],
    ['plan', 'Plan', 'Prepare a bounded implementation plan.'],
    ['risk', 'Assess risk', 'Review the proposed change risk.'],
    ['change', 'Prepare changes', 'Create or repair the proposed change.'],
    ['publish_pr', 'Publish pull request', 'Create or identify the pull request.'],
    ['await_ci', 'Await CI result', 'Continue when GitHub reports the required checks.'],
    ['record_ci', 'Record CI result', 'Route a successful or failed check result.'],
    ['repair', 'Repair changes', 'Handle a failed CI result when configured.'],
    ['human_merge_boundary', 'Human merge', 'Wait for a verified human merge.'],
    ['record_merge', 'Record merge', 'Request completion after merge.'],
    ['record_completion', 'Confirm completion', 'Mark the task done after confirmation.'],
    ['record_no_ci_resolution', 'Resolve no CI', 'Record an owner decision to proceed without checks.'],
];

const workflowFlowLanes = [
    ['Ready event', 'Dispatch routes ready work through pull request creation and CI.', ['dispatch', 'validate_task', 'context', 'plan', 'risk', 'change', 'publish_pr', 'await_ci']],
    ['CI failure', 'Failed checks can be repaired and published for another CI run.', ['record_ci', 'repair', 'publish_pr']],
    ['CI success', 'Successful checks stop at the human-merge boundary.', ['record_ci', 'human_merge_boundary']],
    ['No CI checks', 'An empty completed check snapshot needs an owner decision.', ['record_no_ci_resolution', 'human_merge_boundary']],
    ['Merge event', 'A verified merge is recorded as a separate event.', ['dispatch', 'record_merge']],
    ['Completion event', 'Completion is recorded after confirmation.', ['dispatch', 'record_completion']],
    ['Invalid event', 'Invalid events end at dispatch.', ['dispatch']],
];

const waitingNodeByStage = {
    context: 'context',
    plan: 'plan',
    risk: 'risk',
    change_preparation: 'change',
    publish_pr: 'publish_pr',
    pull_request_id: 'publish_pr',
    github_ci: 'await_ci',
    operator_decision: 'record_no_ci_resolution',
    repair_changes: 'repair',
    human_feedback: 'repair',
    human_merge: 'human_merge_boundary',
};

const stoppedNodeByStatus = {
    invalid_event: 'dispatch',
    task_not_ready: 'validate_task',
    invalid_repository_count: 'validate_task',
};

const workflowStatusLabels = {
    pending_external_operation: 'Waiting for an external operation',
    running: 'In progress',
    invalid_event: 'Stopped: invalid event',
    task_not_ready: 'Stopped: task is not ready',
    invalid_repository_count: 'Stopped: invalid repository selection',
    awaiting_ci: 'No CI result received yet',
    no_ci_checks: 'No CI checks found: operator decision required',
    repair_needed: 'CI failed: repair needed',
    repair_unresolved: 'Needs reviewer feedback',
    ci_succeeded: 'CI passed',
    awaiting_human_merge: 'Waiting for a human merge',
    done: 'Completed',
};

const latestEventLabels = {
    ready: 'Task ready',
    ci: 'CI result',
    merged: 'Pull request merged',
    review_changes_requested: 'Review changes requested',
};

const waitingForLabels = {
    context: 'repository context',
    plan: 'implementation plan',
    risk: 'risk assessment',
    change_preparation: 'change preparation',
    publish_pr: 'pull request publication',
    pull_request_id: 'pull request confirmation',
    github_ci: 'a GitHub CI result',
    operator_decision: 'an owner decision about proceeding without CI',
    repair_changes: 'CI repair',
    human_feedback: 'human feedback',
    human_merge: 'a human merge',
};

function readableState(value, labels) {
    if (!value) return 'Unavailable';
    return labels[value] || value.replaceAll('_', ' ');
}

function pullRequestUrl(repository, pullRequestNumber) {
    if (typeof repository !== 'string' || !Number.isSafeInteger(pullRequestNumber) || pullRequestNumber < 1) return null;
    const repositoryParts = repository.split('/');
    if (repositoryParts.length !== 2 || repositoryParts.some((part) => !part)) return null;
    return `https://github.com/${repositoryParts.map(encodeURIComponent).join('/')}/pull/${pullRequestNumber}`;
}

function appendReviewLink(target, workflow) {
    if (!workflow) return;
    const reviewUrl = pullRequestUrl(workflow.repository, workflow.pull_request_number);
    if (!reviewUrl) return;
    const reviewLink = document.createElement('a');
    reviewLink.className = 'task-link';
    reviewLink.href = reviewUrl;
    reviewLink.target = '_blank';
    reviewLink.rel = 'noopener noreferrer';
    reviewLink.textContent = 'Review feedback';
    target.append(document.createTextNode(' '), reviewLink);
}

const cell = (row, value) => {
    const element = row.insertCell();
    element.textContent = value == null ? 'Unavailable' : String(value);
    return element;
};

async function refresh() {
    workflowsByTaskId = new Map();
    try {
        const response = await fetch('/api/workflows', { cache: 'no-store', headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const data = await response.json();
        workflowsByTaskId = new Map(data.workflows.map((workflow) => [workflow.task_id, workflow]));
        body.replaceChildren();
        for (const workflow of data.workflows) {
            const row = body.insertRow();
            cell(row, workflow.task_id);
            const lifecycleStatus = cell(row, readableState(workflow.workflow_status, workflowStatusLabels));
            lifecycleStatus.className = `status${workflow.workflow_status === 'repair_unresolved' ? ' failed' : ''}`;
            if (workflow.workflow_status === 'repair_unresolved') appendReviewLink(lifecycleStatus, workflow);
            cell(row, readableState(workflow.waiting_for, waitingForLabels));
            cell(row, workflow.repository);
            cell(row, workflow.issue_number == null ? null : `#${workflow.issue_number}`);
            cell(row, workflow.pull_request_number == null ? null : `#${workflow.pull_request_number}`);
            cell(row, readableState(workflow.latest_event, latestEventLabels));
            const queueStatus = cell(row, workflow.has_failure ? `${workflow.status} (failure recorded)` : workflow.status);
            queueStatus.className = `status${workflow.status === 'failed' ? ' failed' : ''}`;
            cell(row, workflow.latest_event_at);
            const action = row.insertCell();
            if (canRetryWorkflows && workflow.status === 'failed') {
                const retry = document.createElement('button');
                retry.type = 'button';
                retry.textContent = 'Retry';
                retry.addEventListener('click', () => retryWorkflow(workflow.task_id, retry));
                action.append(retry);
            }
        }
        message.textContent = data.workflows.length ? `${data.workflows.length} workflow(s)` : 'No workflow records are available yet.';
    } catch {
        message.textContent = 'Workflow state is unavailable.';
    }
}

async function loadPortalPermissions() {
    try {
        const response = await fetch('/api/agent-config', { cache: 'no-store', headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        canRetryWorkflows = record.can_edit === true && record.preview !== true;
        canResolveNoCI = canRetryWorkflows;
    } catch {
        canRetryWorkflows = false;
        canResolveNoCI = false;
    }
}

async function retryWorkflow(taskId, button) {
    button.disabled = true;
    button.textContent = 'Retrying...';
    message.textContent = 'Requeueing failed workflow event...';
    try {
        const response = await fetch(`/api/workflows/${encodeURIComponent(taskId)}/retry`, {
            method: 'POST',
            headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error('unavailable');
        await refreshDashboard();
    } catch {
        message.textContent = 'Retry failed or this workflow is no longer eligible.';
        button.disabled = false;
        button.textContent = 'Retry';
    }
}

async function resolveNoCI(threadId, button) {
    if (!window.confirm(`Proceed with task ${threadId} without CI checks? This records your GitHub owner identity.`)) return;
    button.disabled = true;
    button.textContent = 'Recording decision...';
    try {
        const response = await fetch(`/api/executions/${encodeURIComponent(threadId)}/resolve-no-ci`, {
            method: 'POST',
            headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error('unavailable');
        await refreshDashboard();
    } catch {
        executionMessage.textContent = 'No-CI decision was not recorded; refresh and try again.';
        button.disabled = false;
        button.textContent = 'Proceed without CI';
    }
}

async function showExecution(threadId) {
    selectedExecutionThread = threadId;
    try {
        const response = await fetch(`/api/executions/${encodeURIComponent(threadId)}`, { cache: 'no-store', headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const detail = await response.json();
        if (selectedExecutionThread !== threadId) return;
        renderExecutionFlow(detail);
    } catch {
        if (selectedExecutionThread === threadId) executionDetail.textContent = 'Checkpoint details are unavailable.';
    }
}

function renderExecutionFlow(detail) {
    const checkpoints = detail.checkpoints || [];
    const latest = checkpoints[0] || {};
    const completedNodes = new Set();
    for (let index = 1; index < checkpoints.length; index += 1) {
        const checkpoint = checkpoints[index];
        const newerCheckpoint = checkpoints[index - 1];
        if (
            typeof checkpoint.event_type === 'string'
            && checkpoint.event_type === newerCheckpoint.event_type
            && Number.isInteger(checkpoint.step)
            && Number.isInteger(newerCheckpoint.step)
            && newerCheckpoint.step > checkpoint.step
        ) {
            for (const nodeId of checkpoint.next_nodes || []) completedNodes.add(nodeId);
        }
    }
    const waitingNode = waitingNodeByStage[latest.waiting_for];
    const stoppedNode = stoppedNodeByStatus[latest.workflow_status];
    const unresolvedRepair = latest.workflow_status === 'repair_unresolved'
        && latest.waiting_for === 'human_feedback';

    const nodeById = new Map(workflowFlowNodes.map((node) => [node[0], node]));
    const heading = document.createElement('h3');
    heading.textContent = `LangGraph topology | ${detail.thread_id}`;
    const summary = document.createElement('p');
    summary.className = 'flow-summary';
    summary.textContent = [
        `Status: ${readableState(latest.workflow_status, workflowStatusLabels)}`,
        latest.waiting_for ? `Waiting for: ${readableState(latest.waiting_for, waitingForLabels)}` : null,
        latest.created_at ? `Checkpoint: ${latest.created_at}` : null,
    ].filter(Boolean).join(' | ');
    const repairNote = unresolvedRepair ? document.createElement('p') : null;
    if (repairNote) {
        repairNote.className = 'flow-summary';
        repairNote.textContent = 'The workflow could not resolve the latest review into a repair. Clarify the requested changes in a new review to continue.';
    }
    const audit = latest.no_ci_resolution;
    const auditNote = audit ? document.createElement('p') : null;
    if (auditNote) {
        auditNote.className = 'flow-summary';
        auditNote.textContent = `Operator decision: ${audit.decision} by ${audit.actor} at ${audit.decided_at} for pull request ${audit.pull_request_id}.`;
    }
    const graph = document.createElement('div');
    graph.className = 'workflow-flow';
    const visibleNodeIds = new Set();

    for (const [laneLabel, laneDescription, nodeIds] of workflowFlowLanes) {
        const lane = document.createElement('section');
        lane.className = 'flow-lane';
        const laneHeading = document.createElement('h4');
        laneHeading.textContent = laneLabel;
        const laneDetail = document.createElement('p');
        laneDetail.className = 'flow-lane-description';
        laneDetail.textContent = laneDescription;
        const list = document.createElement('ol');
        list.className = 'flow-lane-nodes';

        for (const nodeId of nodeIds) {
            const [, label, description] = nodeById.get(nodeId);
            let status = '';
            let statusClass = 'pending';
            if (unresolvedRepair && nodeId === 'repair') {
                status = 'Needs reviewer feedback';
                statusClass = 'waiting';
            } else if (stoppedNode === nodeId) {
                status = 'Stopped';
                statusClass = 'stopped';
            } else if (waitingNode === nodeId) {
                status = latest.workflow_status === 'awaiting_ci'
                    ? 'No CI result received yet'
                    : latest.workflow_status === 'no_ci_checks'
                        ? 'No checks found; owner decision required'
                        : `Waiting for ${readableState(latest.waiting_for, waitingForLabels)}`;
                statusClass = 'waiting';
            } else if (completedNodes.has(nodeId)) {
                status = 'Complete';
                statusClass = 'complete';
            }
            if (!['complete', 'waiting', 'stopped'].includes(statusClass)) continue;
            if (visibleNodeIds.has(nodeId)) continue;
            visibleNodeIds.add(nodeId);

            const item = document.createElement('li');
            item.className = `flow-node flow-node-${statusClass}`;
            item.dataset.nodeId = nodeId;
            const content = document.createElement('div');
            const title = document.createElement('strong');
            title.textContent = label;
            const detailText = document.createElement('p');
            detailText.textContent = description;
            const nodeStatus = document.createElement('span');
            nodeStatus.className = 'flow-node-status';
            nodeStatus.textContent = status;
            if (unresolvedRepair && nodeId === 'repair') {
                appendReviewLink(nodeStatus, workflowsByTaskId.get(detail.thread_id));
            }
            if (statusClass === 'complete') content.append(title);
            else content.append(title, detailText);
            item.append(content, nodeStatus);
            list.append(item);
        }

        if (!list.children.length) continue;
        lane.append(laneHeading, laneDetail, list);
        graph.append(lane);
    }

    executionDetail.replaceChildren(heading, summary, ...(repairNote ? [repairNote] : []), ...(auditNote ? [auditNote] : []), graph);
}

async function refreshExecutions() {
    try {
        const response = await fetch('/api/executions', { cache: 'no-store', headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const data = await response.json();
        executionsBody.replaceChildren();
        for (const execution of data.executions) {
            const row = executionsBody.insertRow();
            const taskCell = row.insertCell();
            const taskButton = document.createElement('button');
            taskButton.type = 'button';
            taskButton.className = 'task-link';
            taskButton.textContent = execution.thread_id;
            taskButton.addEventListener('click', () => showExecution(execution.thread_id));
            taskCell.append(taskButton);
            const executionStatus = cell(row, readableState(execution.workflow_status, workflowStatusLabels));
            if (execution.workflow_status === 'repair_unresolved') {
                appendReviewLink(executionStatus, workflowsByTaskId.get(execution.thread_id));
            }
            cell(row, readableState(execution.waiting_for, waitingForLabels));
            cell(row, execution.created_at);
            cell(row, execution.step);
            const action = row.insertCell();
            if (canResolveNoCI && execution.workflow_status === 'no_ci_checks') {
                const resolve = document.createElement('button');
                resolve.type = 'button';
                resolve.textContent = 'Proceed without CI';
                resolve.addEventListener('click', () => resolveNoCI(execution.thread_id, resolve));
                action.append(resolve);
            }
        }
        executionMessage.textContent = data.executions.length
            ? `${data.executions.length} LangGraph execution(s)`
            : 'No LangGraph executions are available yet.';
    } catch {
        executionMessage.textContent = 'LangGraph execution state is unavailable.';
    }
}

async function refreshDashboard() {
    if (document.hidden) return;
    if (refreshPromise) return refreshPromise;

    refreshButton.disabled = true;
    liveStatus.textContent = 'Updating live data...';
    refreshPromise = (async () => {
        try {
            await loadPortalPermissions();
            await refresh();
            await refreshExecutions();
            if (selectedExecutionThread) await showExecution(selectedExecutionThread);
            liveStatus.textContent = `Updated at ${new Date().toLocaleTimeString()} · refreshes every ${refreshIntervalMs / 1000} seconds`;
        } catch {
            liveStatus.textContent = 'Live monitoring is unavailable; retrying automatically.';
        } finally {
            refreshButton.disabled = false;
            refreshPromise = null;
        }
    })();
    return refreshPromise;
}

refreshButton.addEventListener('click', () => refreshDashboard());
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshDashboard();
});
window.setInterval(() => {
    if (!document.hidden) refreshDashboard();
}, refreshIntervalMs);
refreshDashboard();