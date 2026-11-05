const configMessage = document.getElementById('config-message');
const configAudit = document.getElementById('config-audit');
const timeoutInput = document.getElementById('completion-timeout');
const saveConfig = document.getElementById('save-config');
const inferenceMessage = document.getElementById('inference-message');
const inferenceMetricsBody = document.getElementById('inference-metrics-body');
const inferenceMetricsMessage = document.getElementById('inference-metrics-message');
const rosterMessage = document.getElementById('roster-message');
const rosterAudit = document.getElementById('roster-audit');
const rosterSummary = document.getElementById('roster-summary');
const saveRoster = document.getElementById('save-roster');
const rosterForm = document.getElementById('agent-roster-form');
const addProfileRole = document.getElementById('add-profile-role');
const addProfileButton = document.getElementById('add-profile');
const roles = ['context', 'planning', 'risk', 'implementation', 'repair'];
const toolProfiles = {
    context: 'gather_context',
    planning: 'create_plan',
    risk: 'assess_risk',
    implementation: 'prepare_changes',
    repair: 'repair_changes',
};
let rosterRevision = null;
let canEditRoster = false;
let canEditConfig = false;
let savedTimeout = null;
let canEditInference = false;
const savedInferenceModels = {};
const inferenceDownloads = {};
let rosterDraft = null;
let rosterDirty = false;

function setStatus(element, message, state = 'info') {
    element.textContent = message;
    element.dataset.state = state;
}

function updateConfigSaveState() {
    saveConfig.disabled = !canEditConfig || Number(timeoutInput.value) === savedTimeout;
}

async function loadAgentConfig({ quiet = false } = {}) {
    if (!quiet) setStatus(configMessage, 'Loading configuration...', 'loading');
    try {
        const response = await fetch('/api/agent-config', { cache: 'no-store', headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        canEditConfig = record.can_edit === true && record.preview !== true;
        savedTimeout = record.config.completion_timeout_seconds;
        timeoutInput.value = savedTimeout;
        timeoutInput.disabled = !canEditConfig;
        updateConfigSaveState();
        if (!quiet) setStatus(configMessage, record.preview
            ? 'Local preview; live backend data is read-only.'
            : canEditConfig ? 'Organization owner access verified.' : 'Organization owner access is required to save changes.', 'ready');
        configAudit.textContent = record.version
            ? `Version ${record.version} | Updated ${record.updated_at} by ${record.updated_by}`
            : 'Using the runtime default. No saved override.';
    } catch {
        canEditConfig = false;
        timeoutInput.disabled = true;
        saveConfig.disabled = true;
        setStatus(configMessage, 'Agent configuration is unavailable.', 'error');
        configAudit.textContent = '';
    }
}

async function saveAgentConfig(event) {
    event.preventDefault();
    if (!document.getElementById('agent-config-form').reportValidity()) return;
    saveConfig.disabled = true;
    setStatus(configMessage, 'Saving configuration...', 'loading');
    try {
        const response = await fetch('/api/agent-config', {
            method: 'PUT',
            headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({
                completion_timeout_seconds: Number(timeoutInput.value),
            }),
        });
        if (!response.ok) throw new Error('unavailable');
        await loadAgentConfig({ quiet: true });
        setStatus(configMessage, 'Configuration saved for new workflow runs.', 'success');
    } catch {
        setStatus(configMessage, 'Configuration could not be saved.', 'error');
        updateConfigSaveState();
    }
}

function updateInferenceSaveState(form) {
    const instance = form.dataset.instance;
    const input = form.elements.model_filename;
    const active = ['queued', 'downloading', 'verifying'].includes(inferenceDownloads[instance]?.status)
        || ['pending', 'rolling_out'].includes(form.dataset.rolloutStatus);
    form.querySelector('button[type="submit"]').disabled = !canEditInference || (
        active || input.value === savedInferenceModels[instance]
    );
}

function updateModelDownloadButton(form) {
    const instance = form.dataset.instance;
    const url = form.elements.model_url.value;
    const digest = form.elements.model_sha256.value;
    const filename = form.elements.model_filename.value;
    let validUrl = false;
    try {
        const parsed = new URL(url);
        validUrl = parsed.protocol === 'https:' && parsed.hostname === 'huggingface.co'
            && !parsed.username && !parsed.password && !parsed.port && !parsed.hash;
    } catch { /* An empty or malformed URL is not ready to submit. */ }
    const active = ['queued', 'downloading', 'verifying'].includes(inferenceDownloads[instance]?.status)
        || ['pending', 'rolling_out'].includes(form.dataset.rolloutStatus);
    form.querySelector('.download-model').disabled = !canEditInference || active
        || !validUrl || !/^[A-Fa-f0-9]{64}$/.test(digest)
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$/.test(filename);
}

function showDownloadStatus(form, status, error = null) {
    const element = form.querySelector('.download-status');
    element.dataset.state = status === 'failed' ? 'error'
        : status === 'ready' ? 'success'
            : ['queued', 'downloading', 'verifying'].includes(status) ? 'loading' : 'info';
    element.textContent = error || (status ? `Model download ${status}.` : 'No download requested.');
    updateModelDownloadButton(form);
}

function showRolloutStatus(form, status, error = null) {
    const element = form.querySelector('.rollout-status');
    form.dataset.rolloutStatus = status || 'not_requested';
    element.dataset.state = status === 'failed' ? 'error'
        : status === 'ready' ? 'success'
            : ['pending', 'rolling_out', 'waiting_for_download'].includes(status) ? 'loading' : 'info';
    element.textContent = error || (status === 'not_requested'
        ? 'Model rollout not requested.'
        : `Model rollout ${status.replaceAll('_', ' ')}.`);
    updateInferenceSaveState(form);
    updateModelDownloadButton(form);
}

async function loadInferenceInstances({ quiet = false } = {}) {
    if (!quiet) setStatus(inferenceMessage, 'Loading models...', 'loading');
    try {
        const response = await fetch('/api/inference-instances', {
            cache: 'no-store', headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        canEditInference = record.can_edit === true && record.preview !== true;
        for (const instance of record.instances) {
            const form = document.querySelector(`[data-instance="${instance.config.instance_id}"]`);
            const input = form.elements.model_filename;
            savedInferenceModels[instance.config.instance_id] = instance.config.model_filename;
            input.value = instance.config.model_filename;
            form.elements.model_url.value = instance.config.model_url || '';
            form.elements.model_sha256.value = instance.config.model_sha256 || '';
            const rolloutActive = ['pending', 'rolling_out'].includes(instance.config.rollout_status);
            input.disabled = !canEditInference || rolloutActive;
            form.elements.model_url.disabled = !canEditInference || rolloutActive;
            form.elements.model_sha256.disabled = !canEditInference || rolloutActive;
            form.querySelector('.audit-line').textContent = instance.version
                ? `Version ${instance.version} | Updated ${instance.updated_at} by ${instance.updated_by}`
                : 'Configured default; no saved override.';
            const status = instance.config.download_status;
            if (instance.config.download_request_id) {
                inferenceDownloads[instance.config.instance_id] = {
                    requestId: instance.config.download_request_id,
                    status,
                };
            }
            showDownloadStatus(
                form,
                instance.config.download_request_id ? status : '',
                instance.config.download_error,
            );
            showRolloutStatus(
                form,
                instance.config.rollout_status,
                instance.config.rollout_error,
            );
            updateInferenceSaveState(form);
            updateModelDownloadButton(form);
        }
        if (!quiet) setStatus(inferenceMessage, record.preview
            ? 'Local preview; live backend data is read-only.'
            : canEditInference ? 'Organization owner access verified.'
                : 'Organization owner access is required to save changes.', 'ready');
    } catch {
        canEditInference = false;
        document.querySelectorAll('.inference-instance').forEach((form) => {
            form.elements.model_filename.disabled = true;
            form.elements.model_url.disabled = true;
            form.elements.model_sha256.disabled = true;
            updateInferenceSaveState(form);
            updateModelDownloadButton(form);
        });
        setStatus(inferenceMessage, 'Inference configuration is unavailable.', 'error');
    }
}

async function saveInferenceInstance(event) {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    const instance = form.dataset.instance;
    const input = form.elements.model_filename;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    setStatus(inferenceMessage, `Saving ${instance.toUpperCase()} model selection...`, 'loading');
    try {
        const response = await fetch(`/api/inference-instances/${instance}`, {
            method: 'PUT',
            headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_filename: input.value }),
        });
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        savedInferenceModels[instance] = record.config.model_filename;
        form.querySelector('.audit-line').textContent = `Version ${record.version} | Updated ${record.updated_at} by ${record.updated_by}`;
        showRolloutStatus(form, record.config.rollout_status, record.config.rollout_error);
        setStatus(inferenceMessage, `${instance.toUpperCase()} selection saved.`, 'success');
    } catch {
        setStatus(inferenceMessage, `${instance.toUpperCase()} selection could not be saved.`, 'error');
    }
    updateInferenceSaveState(form);
}

async function requestInferenceModelDownload(event) {
    const form = event.currentTarget.closest('.inference-instance');
    const instance = form.dataset.instance;
    if (!form.elements.model_url.reportValidity() || !form.elements.model_sha256.reportValidity()) return;
    const button = form.querySelector('.download-model');
    button.disabled = true;
    inferenceDownloads[instance] = { requestId: null, status: 'queued' };
    updateInferenceSaveState(form);
    showDownloadStatus(form, 'queued');
    try {
        const response = await fetch(`/api/inference-instances/${instance}/model-downloads`, {
            method: 'POST',
            headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model_filename: form.elements.model_filename.value,
                model_url: form.elements.model_url.value,
                model_sha256: form.elements.model_sha256.value,
            }),
        });
        if (response.status === 409) throw new Error('An inference model download or rollout is already active.');
        if (!response.ok) throw new Error('Model download request was rejected.');
        const record = await response.json();
        savedInferenceModels[instance] = record.model_filename;
        inferenceDownloads[instance] = { requestId: record.request_id, status: record.status };
        updateInferenceSaveState(form);
        showDownloadStatus(form, record.status, record.public_error);
        await loadInferenceInstances({ quiet: true });
        await pollInferenceDownloads();
    } catch (error) {
        inferenceDownloads[instance] = { requestId: null, status: 'failed' };
        updateInferenceSaveState(form);
        showDownloadStatus(form, 'failed', error.message || 'Model download request failed.');
    }
}

async function pollInferenceDownloads() {
    let downloadBecameTerminal = false;
    for (const [instance, download] of Object.entries(inferenceDownloads)) {
        if (!download.requestId || !['queued', 'downloading', 'verifying'].includes(download.status)) continue;
        try {
            const response = await fetch(`/api/inference-model-downloads/${download.requestId}`, {
                cache: 'no-store', headers: { Accept: 'application/json' },
            });
            if (!response.ok) throw new Error('unavailable');
            const record = await response.json();
            inferenceDownloads[instance] = { requestId: record.request_id, status: record.status };
            downloadBecameTerminal ||= !['queued', 'downloading', 'verifying'].includes(record.status);
            const form = document.querySelector(`[data-instance="${instance}"]`);
            showDownloadStatus(form, record.status, record.public_error);
        } catch {
            const form = document.querySelector(`[data-instance="${instance}"]`);
            showDownloadStatus(form, 'unavailable', 'Model download status is unavailable.');
        }
    }
    const rolloutActive = [...document.querySelectorAll('.inference-instance')].some((form) =>
        ['pending', 'rolling_out'].includes(form.dataset.rolloutStatus));
    if (downloadBecameTerminal || rolloutActive) await loadInferenceInstances({ quiet: true });
}

function formatMetric(metric, formatter) {
    if (!metric || metric.status === 'unavailable' || metric.value === null) return 'Unavailable';
    const display = formatter(metric.value);
    return metric.status === 'stale' ? `${display} (stale)` : display;
}

function formatBytes(bytes) {
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
    return `${(bytes / 1024 ** 2).toFixed(0)} MiB`;
}

function metricCell(row, value) {
    const cell = document.createElement('td');
    cell.textContent = value;
    row.append(cell);
}

function renderInferenceMetrics(instances) {
    inferenceMetricsBody.replaceChildren();
    for (const instance of instances) {
        const row = document.createElement('tr');
        metricCell(row, instance.instance_id.toUpperCase());
        metricCell(row, formatMetric(instance.cpu_utilization_percent, (value) => `${value.toFixed(1)}%`));
        metricCell(row, formatMetric(instance.ram_used_bytes, formatBytes));
        metricCell(row, formatMetric(instance.gpu_utilization_percent, (value) => `${value.toFixed(1)}%`));
        const used = formatMetric(instance.vram_used_bytes, formatBytes);
        const total = formatMetric(instance.vram_total_bytes, formatBytes);
        metricCell(row, used === 'Unavailable' || total === 'Unavailable' ? 'Unavailable' : `${used} / ${total}`);
        metricCell(row, instance.sampled_at ? new Date(instance.sampled_at).toLocaleString() : 'Unavailable');
        inferenceMetricsBody.append(row);
    }
}

async function loadInferenceMetrics() {
    try {
        const response = await fetch('/api/inference-metrics', {
            cache: 'no-store', headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        renderInferenceMetrics(record.instances);
        setStatus(inferenceMetricsMessage, 'Metrics refreshed.', 'ready');
    } catch {
        renderInferenceMetrics(['gpu', 'cpu'].map((instance_id) => ({
            instance_id,
            cpu_utilization_percent: { value: null, status: 'unavailable' },
            ram_used_bytes: { value: null, status: 'unavailable' },
            gpu_utilization_percent: { value: null, status: 'unavailable' },
            vram_used_bytes: { value: null, status: 'unavailable' },
            vram_total_bytes: { value: null, status: 'unavailable' },
            sampled_at: null,
        })));
        setStatus(inferenceMetricsMessage, 'Inference metrics are unavailable.', 'error');
    }
}

function makeField(labelText, control, className = '') {
    const wrapper = document.createElement('div');
    wrapper.className = `profile-field ${className}`.trim();
    const label = document.createElement('label');
    label.htmlFor = control.id;
    label.textContent = labelText;
    wrapper.append(label, control);
    return wrapper;
}

function updateRosterSaveState() {
    saveRoster.disabled = !canEditRoster || !rosterDirty;
    addProfileButton.disabled = !canEditRoster || rosterDraft.agents.length >= 20;
    addProfileRole.disabled = !canEditRoster || rosterDraft.agents.length >= 20;
    rosterSummary.querySelectorAll('[data-remove-agent-id]').forEach((button) => {
        const profile = rosterDraft.agents.find((agent) => agent.agent_id === button.dataset.removeAgentId);
        const assigned = Object.values(rosterDraft.assignments).includes(profile.agent_id);
        const hasAlternative = rosterDraft.agents.some((agent) => (
            agent.agent_id !== profile.agent_id && agent.tool_profile === profile.tool_profile
        ));
        button.disabled = !canEditRoster || assigned || !hasAlternative;
        button.title = assigned
            ? 'Reassign this profile before removing it.'
            : !hasAlternative ? 'Keep at least one profile for each workflow role.' : 'Remove profile';
    });
}

function updateAssignmentOptions() {
    rosterSummary.querySelectorAll('[data-role-assignment]').forEach((select) => {
        const role = select.dataset.roleAssignment;
        const selected = rosterDraft.assignments[role];
        const compatible = rosterDraft.agents.filter((agent) => agent.tool_profile === toolProfiles[role]);
        select.replaceChildren();
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = compatible.length ? 'Select a profile' : 'Add a compatible profile';
        placeholder.disabled = true;
        select.append(placeholder);
        for (const profile of compatible) {
            const option = document.createElement('option');
            option.value = profile.agent_id;
            option.textContent = `${profile.name} (${profile.agent_id})`;
            select.append(option);
        }
        select.value = compatible.some((profile) => profile.agent_id === selected) ? selected : '';
        if (!select.value) rosterDraft.assignments[role] = '';
        select.disabled = !canEditRoster;
    });
    updateRosterSaveState();
}

function createProfileEditor(profile) {
    const details = document.createElement('details');
    details.className = 'roster-profile';
    const summary = document.createElement('summary');
    const identity = document.createElement('span');
    identity.className = 'profile-identity';
    const title = document.createElement('strong');
    title.textContent = profile.name;
    const id = document.createElement('code');
    id.textContent = profile.agent_id;
    identity.append(title, id);
    const metadata = document.createElement('span');
    metadata.className = 'profile-meta';
    metadata.textContent = `${profile.model_profile} · ${profile.tool_profile}`;
    summary.append(identity, metadata);

    const fields = document.createElement('div');
    fields.className = 'profile-fields';
    const prefix = `profile-${profile.agent_id}`;
    const name = document.createElement('input');
    name.id = `${prefix}-name`;
    name.type = 'text';
    name.maxLength = 100;
    name.required = true;
    name.value = profile.name;
    name.disabled = !canEditRoster;
    name.addEventListener('input', () => {
        profile.name = name.value;
        title.textContent = profile.name || 'Unnamed profile';
        updateAssignmentOptions();
        markRosterChanged();
    });
    fields.append(makeField('Profile name', name));

    const description = document.createElement('input');
    description.id = `${prefix}-description`;
    description.type = 'text';
    description.maxLength = 500;
    description.required = true;
    description.value = profile.description;
    description.disabled = !canEditRoster;
    description.addEventListener('input', () => {
        profile.description = description.value;
        markRosterChanged();
    });
    fields.append(makeField('Description', description));

    const model = document.createElement('input');
    model.id = `${prefix}-model`;
    model.type = 'text';
    model.value = profile.model_profile;
    model.readOnly = true;
    model.disabled = !canEditRoster;
    fields.append(makeField('Model profile', model));

    const tool = document.createElement('select');
    tool.id = `${prefix}-tool`;
    for (const role of roles) {
        const option = document.createElement('option');
        option.value = toolProfiles[role];
        option.textContent = `${role} · ${toolProfiles[role]}`;
        tool.append(option);
    }
    tool.value = profile.tool_profile;
    tool.disabled = !canEditRoster;
    tool.addEventListener('change', () => {
        profile.tool_profile = tool.value;
        metadata.textContent = `${profile.model_profile} · ${profile.tool_profile}`;
        updateAssignmentOptions();
        markRosterChanged();
    });
    fields.append(makeField('Tool profile', tool));

    const skills = document.createElement('input');
    skills.id = `${prefix}-skills`;
    skills.type = 'text';
    skills.value = profile.skill_ids.join(', ');
    skills.disabled = !canEditRoster;
    skills.setAttribute('aria-describedby', `${prefix}-skills-help`);
    skills.addEventListener('input', () => {
        profile.skill_ids = skills.value.split(',').map((skill) => skill.trim()).filter(Boolean);
        markRosterChanged();
    });
    const skillField = makeField('Skill IDs', skills);
    const skillHelp = document.createElement('small');
    skillHelp.id = `${prefix}-skills-help`;
    skillHelp.textContent = 'Comma-separated metadata identifiers';
    skillField.append(skillHelp);
    fields.append(skillField);

    const prompt = document.createElement('textarea');
    prompt.id = `${prefix}-prompt`;
    prompt.rows = 5;
    prompt.maxLength = 4000;
    prompt.value = profile.prompt_content;
    prompt.disabled = !canEditRoster;
    prompt.addEventListener('input', () => {
        profile.prompt_content = prompt.value;
        markRosterChanged();
    });
    fields.append(makeField('Prompt content', prompt, 'profile-prompt-field'));

    const footer = document.createElement('div');
    footer.className = 'profile-footer';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-profile';
    remove.dataset.removeAgentId = profile.agent_id;
    remove.textContent = 'Remove profile';
    remove.addEventListener('click', () => {
        rosterDraft.agents = rosterDraft.agents.filter((agent) => agent.agent_id !== profile.agent_id);
        details.remove();
        updateAssignmentOptions();
        markRosterChanged();
    });
    footer.append(remove);
    details.append(summary, fields, footer);
    return details;
}

function markRosterChanged() {
    rosterDirty = true;
    setStatus(rosterMessage, 'Unsaved changes.', 'dirty');
    updateRosterSaveState();
}

function renderRoster() {
    rosterSummary.replaceChildren();
    const assignmentHeading = document.createElement('h3');
    assignmentHeading.textContent = 'Role assignments';
    const assignmentTableWrap = document.createElement('div');
    assignmentTableWrap.className = 'roster-table-wrap';
    const assignments = document.createElement('table');
    assignments.className = 'roster-table';
    assignments.innerHTML = '<thead><tr><th scope="col">Workflow role</th><th scope="col">Assigned profile</th><th scope="col">Required tool profile</th></tr></thead>';
    const assignmentBody = document.createElement('tbody');
    for (const role of roles) {
        const row = document.createElement('tr');
        const roleCell = document.createElement('th');
        roleCell.scope = 'row';
        roleCell.textContent = role;
        const profileCell = document.createElement('td');
        const select = document.createElement('select');
        select.className = 'role-assignment';
        select.dataset.roleAssignment = role;
        select.setAttribute('aria-label', `Assigned profile for ${role}`);
        select.required = true;
        select.addEventListener('change', () => {
            rosterDraft.assignments[role] = select.value;
            updateRosterSaveState();
            markRosterChanged();
        });
        profileCell.append(select);
        const toolCell = document.createElement('td');
        const toolCode = document.createElement('code');
        toolCode.textContent = toolProfiles[role];
        toolCell.append(toolCode);
        row.append(roleCell, profileCell, toolCell);
        assignmentBody.append(row);
    }
    assignments.append(assignmentBody);
    assignmentTableWrap.append(assignments);

    const profileHeading = document.createElement('h3');
    profileHeading.textContent = 'Profiles';
    const profileIntro = document.createElement('p');
    profileIntro.className = 'profile-intro';
    profileIntro.textContent = 'Select a profile to inspect or edit its configuration.';
    const profiles = document.createElement('div');
    profiles.className = 'roster-profiles';
    for (const profile of rosterDraft.agents) profiles.append(createProfileEditor(profile));
    if (rosterDraft.agents.length === 0) {
        const empty = document.createElement('p');
        empty.className = 'empty-roster';
        empty.textContent = 'No roster configured. Add one profile for each workflow role to begin.';
        profiles.append(empty);
    }
    rosterSummary.append(assignmentHeading, assignmentTableWrap, profileHeading, profileIntro, profiles);
    updateAssignmentOptions();
}

function applyRosterRecord(record) {
    canEditRoster = record.can_edit === true && record.preview !== true;
    rosterRevision = record.revision;
    rosterDraft = record.roster
        ? JSON.parse(JSON.stringify(record.roster))
        : { agents: [], assignments: Object.fromEntries(roles.map((role) => [role, ''])) };
    rosterDirty = false;
    renderRoster();
    updateRosterSaveState();
    rosterAudit.textContent = record.revision
        ? `Revision ${record.revision} | Updated ${record.updated_at} by ${record.updated_by}`
        : 'No saved roster. Revision 0 is ready for initial setup.';
}

async function loadAgentRoster() {
    rosterMessage.textContent = 'Loading roster...';
    try {
        const response = await fetch('/api/agent-roster', { cache: 'no-store', headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        applyRosterRecord(record);
        setStatus(rosterMessage, record.preview
            ? 'Local preview; live roster data is read-only.'
            : canEditRoster ? 'Organization owner access verified.' : 'Organization owner access is required to save changes.', 'ready');
    } catch {
        canEditRoster = false;
        rosterDraft = { agents: [], assignments: {} };
        saveRoster.disabled = true;
        addProfileButton.disabled = true;
        addProfileRole.disabled = true;
        setStatus(rosterMessage, 'Agent roster is unavailable.', 'error');
        rosterAudit.textContent = '';
        rosterSummary.replaceChildren();
    }
}

function addProfile() {
    if (rosterDraft.agents.length >= 20) return;
    const role = addProfileRole.value;
    let suffix = 1;
    let agentId = `${role}-profile`;
    while (rosterDraft.agents.some((profile) => profile.agent_id === agentId)) {
        suffix += 1;
        agentId = `${role}-profile-${suffix}`;
    }
    const profile = {
        agent_id: agentId,
        name: `${role[0].toUpperCase()}${role.slice(1)} profile`,
        description: `Profile for the ${role} workflow role.`,
        prompt_content: '',
        model_profile: 'local_llama_cpp',
        skill_ids: [],
        tool_profile: toolProfiles[role],
    };
    rosterDraft.agents.push(profile);
    if (!rosterDraft.assignments[role]) rosterDraft.assignments[role] = agentId;
    renderRoster();
    markRosterChanged();
    const editor = [...rosterSummary.querySelectorAll('.roster-profile')].at(-1);
    editor.open = true;
    editor.querySelector('input').focus();
}

async function saveAgentRoster(event) {
    event.preventDefault();
    if (!rosterForm.reportValidity()) return;
    if (!Number.isInteger(rosterRevision) || rosterRevision < 0) {
        setStatus(rosterMessage, 'Roster revision is unavailable; reload this page before saving.', 'error');
        return;
    }

    saveRoster.disabled = true;
    setStatus(rosterMessage, 'Saving roster...', 'loading');
    try {
        const response = await fetch('/api/agent-roster', {
            method: 'PUT',
            headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({ roster: rosterDraft, expected_revision: rosterRevision }),
        });
        if (response.status === 409) {
            setStatus(rosterMessage, 'The roster changed since it was loaded. Reload this page before editing.', 'error');
            updateRosterSaveState();
            return;
        }
        if (response.status === 422) {
            setStatus(rosterMessage, 'Roster validation failed. Check profile fields, skill IDs, and role assignments.', 'error');
            updateRosterSaveState();
            return;
        }
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        applyRosterRecord(record);
        setStatus(rosterMessage, `Roster saved as revision ${record.revision}.`, 'success');
    } catch {
        setStatus(rosterMessage, 'Roster could not be saved; the workflow service may be unavailable.', 'error');
        updateRosterSaveState();
    }
}

timeoutInput.addEventListener('input', () => {
    updateConfigSaveState();
    if (Number(timeoutInput.value) !== savedTimeout) setStatus(configMessage, 'Unsaved changes.', 'dirty');
});
document.getElementById('agent-config-form').addEventListener('submit', saveAgentConfig);
document.querySelectorAll('.inference-instance').forEach((form) => {
    form.addEventListener('submit', saveInferenceInstance);
    form.elements.model_filename.addEventListener('input', () => updateInferenceSaveState(form));
    form.elements.model_filename.addEventListener('input', () => updateModelDownloadButton(form));
    form.elements.model_url.addEventListener('input', () => updateModelDownloadButton(form));
    form.elements.model_sha256.addEventListener('input', () => updateModelDownloadButton(form));
    form.querySelector('.download-model').addEventListener('click', requestInferenceModelDownload);
});
rosterForm.addEventListener('submit', saveAgentRoster);
addProfileButton.addEventListener('click', addProfile);
loadAgentConfig();
loadInferenceInstances();
loadInferenceMetrics();
loadAgentRoster();
window.setInterval(pollInferenceDownloads, 3000);
window.setInterval(loadInferenceMetrics, 15000);