const configMessage = document.getElementById('config-message');
const configAudit = document.getElementById('config-audit');
const timeoutInput = document.getElementById('completion-timeout');
const saveConfig = document.getElementById('save-config');
const inferenceMessage = document.getElementById('inference-message');
const inferenceMetricsBody = document.getElementById('inference-metrics-body');
const inferenceMetricsMessage = document.getElementById('inference-metrics-message');
const modelDownloadForm = document.getElementById('model-download-form');
const modelDownloadFilename = document.getElementById('model-download-filename');
const modelDownloadUrl = document.getElementById('model-download-url');
const modelCatalogStatus = document.getElementById('model-catalog-status');
const availableModels = document.getElementById('available-models');
const downloadStatus = document.getElementById('download-status');
const toastRegion = document.getElementById('toast-region');
const inferenceInstanceList = document.getElementById('inference-instance-list');
const inferenceInstanceTemplate = document.getElementById('inference-instance-template');
const inferenceInstanceCreateForm = document.getElementById('inference-instance-create-form');
const newInferenceInstanceId = document.getElementById('new-inference-instance-id');
const newInferenceModel = document.getElementById('new-inference-model');
const createInferenceInstanceButton = document.getElementById('create-inference-instance');
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
let availableModelFilenames = [];
let modelInventoryLoaded = false;
let activeInferenceInstanceCount = 0;
let toastTimeout = null;
let modelDownloadFilenameManuallyEdited = false;
const savedInferenceModels = {};
let sharedDownload = { requestId: null, status: '', publicError: null };
let rosterDraft = null;
let rosterDirty = false;

function setStatus(element, message, state = 'info') {
    element.textContent = message;
    element.dataset.state = state;
}

function showToast(message, state = 'info') {
    toastRegion.textContent = message;
    toastRegion.dataset.state = state;
    toastRegion.hidden = false;
    window.clearTimeout(toastTimeout);
    toastTimeout = window.setTimeout(() => {
        toastRegion.hidden = true;
    }, 5000);
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
        showToast('Configuration saved for new workflow runs.', 'success');
    } catch {
        setStatus(configMessage, 'Configuration could not be saved.', 'error');
        showToast('Configuration could not be saved.', 'error');
        updateConfigSaveState();
    }
}

function updateInferenceSaveState(form) {
    const instance = form.dataset.instance;
    const input = form.elements.model_filename;
    const status = form.dataset.rolloutStatus || 'not_requested';
    const active = ['pending', 'rolling_out'].includes(status);
    const unchanged = input.value === savedInferenceModels[instance];
    const button = form.querySelector('button[type="submit"]');
    const help = form.querySelector('.apply-model-help');
    button.disabled = !canEditInference || (
        active || !modelInventoryLoaded || !availableModelFilenames.includes(input.value)
        || (unchanged && status === 'ready')
    );
    help.textContent = active
        ? 'A model rollout is already in progress.'
        : !canEditInference
            ? 'Organization owner access is required to apply models.'
            : !modelInventoryLoaded
                ? 'Model availability is unavailable; reload the catalog to enable Apply model.'
                : !availableModelFilenames.includes(input.value)
                    ? 'Select an available model to enable Apply model.'
                    : unchanged && status === 'ready'
                        ? 'This filename is already selected. Choose a different available model to enable Apply model.'
                        : unchanged && status === 'failed'
                            ? 'The previous rollout failed. Apply model to retry.'
                            : unchanged && status === 'not_requested'
                                ? 'No rollout has been requested for this selection. Apply model to request one.'
                                : '';
}

function updateInferenceDeleteState(form) {
    const active = ['pending', 'rolling_out'].includes(form.dataset.rolloutStatus);
    const button = form.querySelector('.delete-inference-instance');
    button.disabled = !canEditInference || active || activeInferenceInstanceCount <= 1;
    button.title = active
        ? 'Wait for the active model rollout to finish.'
        : activeInferenceInstanceCount <= 1
            ? 'Keep at least one inference instance configuration.'
            : 'Delete configuration';
}

function updateInferenceCreateState() {
    const instanceId = newInferenceInstanceId.value;
    const validId = /^(?:gpu|cpu)(?:-[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?)?$/.test(instanceId);
    const availableModel = availableModelFilenames.includes(newInferenceModel.value);
    createInferenceInstanceButton.disabled = !canEditInference || !modelInventoryLoaded
        || !validId || Object.hasOwn(savedInferenceModels, instanceId) || !availableModel;
}

function updateModelDownloadButton() {
    const url = modelDownloadUrl.value;
    const filename = modelDownloadFilename.value;
    let validUrl = false;
    try {
        const parsed = new URL(url);
        validUrl = parsed.protocol === 'https:' && parsed.hostname === 'huggingface.co'
            && !parsed.username && !parsed.password && !parsed.port && !parsed.hash;
    } catch { /* An empty or malformed URL is not ready to submit. */ }
    const active = ['resolving', 'queued', 'downloading', 'verifying'].includes(sharedDownload.status);
    modelDownloadForm.querySelector('.download-model').disabled = !canEditInference || active
        || !validUrl
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$/.test(filename);
}

function suggestModelFilenameFromUrl() {
    if (!modelDownloadFilenameManuallyEdited) {
        let filename = '';
        try {
            const parsed = new URL(modelDownloadUrl.value);
            if (parsed.protocol === 'https:' && parsed.hostname === 'huggingface.co'
                && !parsed.username && !parsed.password && !parsed.port) {
                const encodedBasename = parsed.pathname.split('/').pop();
                const basename = decodeURIComponent(encodedBasename || '');
                if (/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$/.test(basename)) {
                    filename = basename;
                }
            }
        } catch { /* Invalid URLs and invalid percent-encoding have no suggestion. */ }
        modelDownloadFilename.value = filename;
    }
    updateModelDownloadButton();
}

function showDownloadStatus(status, error = null) {
    downloadStatus.dataset.state = ['failed', 'resolution_failed'].includes(status) ? 'error'
        : status === 'ready' ? 'success'
            : ['resolving', 'queued', 'downloading', 'verifying'].includes(status) ? 'loading' : 'info';
    const message = error || (status === 'resolving'
        ? 'Checking Hugging Face file metadata...'
        : status === 'queued'
            ? 'Download queued; checksum metadata verified.'
            : status ? `Model download ${status}.` : 'No download requested.');
    downloadStatus.textContent = message;
    updateModelDownloadButton();
}

function showRolloutStatus(form, status, error = null) {
    const element = form.querySelector('.rollout-status');
    form.dataset.rolloutStatus = status || 'not_requested';
    element.dataset.state = status === 'failed' ? 'error'
        : status === 'ready' ? 'success'
            : ['pending', 'rolling_out'].includes(status) ? 'loading' : 'info';
    element.textContent = error || (status === 'not_requested'
        ? 'Model rollout not requested.'
        : `Model rollout ${status.replaceAll('_', ' ')}.`);
    updateInferenceSaveState(form);
    updateInferenceDeleteState(form);
    updateModelDownloadButton();
}

function createInferenceInstanceForm(instanceId) {
    const form = inferenceInstanceTemplate.content.firstElementChild.cloneNode(true);
    const kind = instanceId.startsWith('gpu') ? 'GPU' : 'CPU';
    const prefix = `inference-${instanceId}`;
    form.dataset.instance = instanceId;
    form.querySelector('h3').textContent = `${kind} instance · ${instanceId}`;
    for (const [name, suffix] of Object.entries({ model_filename: 'model-filename' })) {
        const input = form.elements[name];
        input.id = `${prefix}-${suffix}`;
        form.querySelector(`label[for="${suffix}"]`).htmlFor = input.id;
    }
    const selectionStatus = form.querySelector('.model-selection-status');
    selectionStatus.id = `${prefix}-model-selection-status`;
    form.elements.model_filename.setAttribute('aria-describedby', selectionStatus.id);
    const applyHelp = form.querySelector('.apply-model-help');
    applyHelp.id = `${prefix}-apply-model-help`;
    const applyButton = form.querySelector('button[type="submit"]');
    applyButton.textContent = 'Apply model';
    applyButton.setAttribute('aria-describedby', applyHelp.id);
    form.addEventListener('submit', saveInferenceInstance);
    form.elements.model_filename.addEventListener('change', () => updateInferenceSaveState(form));
    form.querySelector('.delete-inference-instance').addEventListener('click', () => {
        deleteInferenceInstance(instanceId);
    });
    return form;
}

function renderModelSelection(form, selectedFilename) {
    const select = form.elements.model_filename;
    select.replaceChildren();
    const available = modelInventoryLoaded && availableModelFilenames.includes(selectedFilename);
    for (const filename of availableModelFilenames) {
        const option = document.createElement('option');
        option.value = filename;
        option.textContent = filename;
        select.append(option);
    }
    if (!available) {
        const option = document.createElement('option');
        option.value = selectedFilename;
        option.disabled = true;
        option.textContent = modelInventoryLoaded
            ? `${selectedFilename} (missing from shared model storage)`
            : `${selectedFilename} (model availability unavailable)`;
        select.append(option);
    }
    select.value = selectedFilename;
    const status = form.querySelector('.model-selection-status');
    status.dataset.state = !modelInventoryLoaded ? 'error' : available ? 'ready' : 'error';
    status.textContent = !modelInventoryLoaded
        ? 'Model availability is unavailable; the configured filename is retained.'
        : available
            ? `${availableModelFilenames.length} GGUF models available.`
            : 'Configured file is missing from shared model storage. Choose an available model or request its download.';
}

function syncInferenceInstanceForms(instances) {
    const existing = new Map([...inferenceInstanceList.querySelectorAll('.inference-instance')]
        .map((form) => [form.dataset.instance, form]));
    const instanceIds = new Set();
    for (const instance of instances) {
        const instanceId = instance.config.instance_id;
        instanceIds.add(instanceId);
        const form = existing.get(instanceId) || createInferenceInstanceForm(instanceId);
        inferenceInstanceList.append(form);
    }
    for (const [instanceId, form] of existing) {
        if (!instanceIds.has(instanceId)) form.remove();
    }
}

function renderModelCatalog() {
    availableModels.replaceChildren();
    for (const filename of availableModelFilenames) {
        const option = document.createElement('option');
        option.value = filename;
        availableModels.append(option);
    }
    setStatus(modelCatalogStatus, modelInventoryLoaded
        ? `${availableModelFilenames.length} GGUF models available across inference instances.`
        : 'Shared model catalog is unavailable.', modelInventoryLoaded ? 'ready' : 'error');
    modelDownloadFilename.disabled = !canEditInference;
    modelDownloadUrl.disabled = !canEditInference;
    newInferenceInstanceId.disabled = !canEditInference;
    newInferenceModel.replaceChildren();
    for (const filename of availableModelFilenames) {
        const option = document.createElement('option');
        option.value = filename;
        option.textContent = filename;
        newInferenceModel.append(option);
    }
    newInferenceModel.disabled = !canEditInference || !modelInventoryLoaded;
    updateInferenceCreateState();
    updateModelDownloadButton();
}

async function loadInferenceInstances({ quiet = false } = {}) {
    if (!quiet) setStatus(inferenceMessage, 'Loading models...', 'loading');
    try {
        const modelsResponse = await fetch('/api/inference-models', {
            cache: 'no-store', headers: { Accept: 'application/json' },
        });
        if (!modelsResponse.ok) throw new Error('unavailable');
        const modelsRecord = await modelsResponse.json();
        if (!Array.isArray(modelsRecord.models) || modelsRecord.models.some((name) => typeof name !== 'string')) {
            throw new Error('unavailable');
        }
        availableModelFilenames = modelsRecord.models;
        modelInventoryLoaded = true;
    } catch {
        availableModelFilenames = [];
        modelInventoryLoaded = false;
    }
    renderModelCatalog();
    try {
        const response = await fetch('/api/inference-instances', {
            cache: 'no-store', headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        canEditInference = record.can_edit === true && record.preview !== true;
        activeInferenceInstanceCount = record.instances.length;
        renderModelCatalog();
        syncInferenceInstanceForms(record.instances);
        for (const instance of record.instances) {
            const form = document.querySelector(`[data-instance="${instance.config.instance_id}"]`);
            const input = form.elements.model_filename;
            savedInferenceModels[instance.config.instance_id] = instance.config.model_filename;
            renderModelSelection(form, instance.config.model_filename);
            const rolloutActive = ['pending', 'rolling_out'].includes(instance.config.rollout_status);
            input.disabled = !canEditInference || rolloutActive || !modelInventoryLoaded;
            form.querySelector('.audit-line').textContent = instance.version
                ? `Version ${instance.version} | Updated ${instance.updated_at} by ${instance.updated_by}`
                : 'Configured default; no saved override.';
            showRolloutStatus(
                form,
                instance.config.rollout_status,
                instance.config.rollout_error,
            );
            updateInferenceSaveState(form);
            updateInferenceDeleteState(form);
            updateModelDownloadButton();
        }
        if (!quiet) setStatus(inferenceMessage, !modelInventoryLoaded
            ? 'Model availability is unavailable; selections are read-only.'
            : record.preview ? 'Local preview; live backend data is read-only.'
                : canEditInference ? 'Organization owner access verified.'
                    : 'Organization owner access is required to save changes.',
            modelInventoryLoaded ? 'ready' : 'error');
    } catch {
        canEditInference = false;
        document.querySelectorAll('.inference-instance').forEach((form) => {
            form.elements.model_filename.disabled = true;
            updateInferenceSaveState(form);
            updateInferenceDeleteState(form);
        });
        modelDownloadFilename.disabled = true;
        modelDownloadUrl.disabled = true;
        newInferenceInstanceId.disabled = true;
        newInferenceModel.disabled = true;
        createInferenceInstanceButton.disabled = true;
        updateModelDownloadButton();
        setStatus(inferenceMessage, 'Inference configuration is unavailable.', 'error');
    }
}

async function createInferenceInstance(event) {
    event.preventDefault();
    if (!inferenceInstanceCreateForm.reportValidity()) return;
    createInferenceInstanceButton.disabled = true;
    setStatus(inferenceMessage, 'Creating inference configuration...', 'loading');
    try {
        const response = await fetch('/api/inference-instances', {
            method: 'POST',
            headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({
                instance_id: newInferenceInstanceId.value,
                model_filename: newInferenceModel.value,
            }),
        });
        if (response.status === 409) throw new Error('conflict');
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        setStatus(inferenceMessage, `${record.config.instance_id} configuration created.`, 'success');
        showToast(`${record.config.instance_id} configuration created.`, 'success');
        newInferenceInstanceId.value = '';
        await loadInferenceInstances({ quiet: true });
        await loadInferenceMetrics();
    } catch (error) {
        const duplicate = error.message === 'conflict';
        setStatus(inferenceMessage, duplicate
            ? 'That inference instance ID already exists.'
            : 'Inference configuration could not be created.', 'error');
        showToast(duplicate
            ? 'That inference instance ID already exists.'
            : 'Inference configuration could not be created.', 'error');
    }
    updateInferenceCreateState();
}

async function deleteInferenceInstance(instanceId) {
    if (!window.confirm(`Delete the saved configuration for ${instanceId}? Historical references will remain.`)) return;
    setStatus(inferenceMessage, `Deleting ${instanceId} configuration...`, 'loading');
    try {
        const response = await fetch(`/api/inference-instances/${instanceId}`, {
            method: 'DELETE',
            headers: { Accept: 'application/json' },
        });
        if (response.status === 409) throw new Error('conflict');
        if (!response.ok) throw new Error('unavailable');
        delete savedInferenceModels[instanceId];
        setStatus(inferenceMessage, `${instanceId} configuration deleted.`, 'success');
        showToast(`${instanceId} configuration deleted.`, 'success');
        await loadInferenceInstances({ quiet: true });
        await loadInferenceMetrics();
    } catch (error) {
        const conflict = error.message === 'conflict';
        setStatus(inferenceMessage, conflict
            ? 'This configuration cannot be deleted while active or when it is the last one.'
            : 'Inference configuration could not be deleted.', 'error');
        showToast(conflict
            ? 'This configuration cannot be deleted while active or when it is the last one.'
            : 'Inference configuration could not be deleted.', 'error');
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
    setStatus(inferenceMessage, `Applying ${instance.toUpperCase()} model...`, 'loading');
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
        setStatus(inferenceMessage, `${instance.toUpperCase()} model rollout requested.`, 'success');
        showToast(`${instance.toUpperCase()} model rollout requested.`, 'success');
    } catch {
        setStatus(inferenceMessage, `${instance.toUpperCase()} model could not be applied.`, 'error');
        showToast(`${instance.toUpperCase()} model could not be applied.`, 'error');
    }
    updateInferenceSaveState(form);
}

async function requestInferenceModelDownload(event) {
    event.preventDefault();
    if (!modelDownloadForm.reportValidity()) return;
    const button = modelDownloadForm.querySelector('.download-model');
    button.disabled = true;
    sharedDownload = { requestId: null, status: 'resolving', publicError: null };
    showDownloadStatus('resolving');
    try {
        const response = await fetch('/api/inference-model-downloads', {
            method: 'POST',
            headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model_filename: modelDownloadFilename.value,
                model_url: modelDownloadUrl.value,
            }),
        });
        if (response.status === 409) throw new Error('A shared model download is already active.');
        if (response.status === 422) throw new Error('Hugging Face file could not be verified. Check the URL and try again.');
        if (!response.ok) throw new Error('Model download request was rejected.');
        const record = await response.json();
        sharedDownload = {
            requestId: record.request_id,
            status: record.status,
            publicError: record.public_error,
        };
        showDownloadStatus(record.status, record.public_error);
        showToast('Model download added to the shared queue.', 'success');
        await loadInferenceInstances({ quiet: true });
        await pollInferenceDownloads();
    } catch (error) {
        sharedDownload = {
            requestId: null,
            status: 'resolution_failed',
            publicError: error.message || 'Model download request failed.',
        };
        showDownloadStatus('failed', sharedDownload.publicError);
        showToast(sharedDownload.publicError, 'error');
    }
}

async function pollInferenceDownloads() {
    let downloadBecameTerminal = false;
    const download = sharedDownload;
    if (download.requestId && ['queued', 'downloading', 'verifying'].includes(download.status)) {
        try {
            const response = await fetch(`/api/inference-model-downloads/${download.requestId}`, {
                cache: 'no-store', headers: { Accept: 'application/json' },
            });
            if (!response.ok) throw new Error('unavailable');
            const record = await response.json();
            sharedDownload = {
                requestId: record.request_id,
                status: record.status,
                publicError: record.public_error,
            };
            const terminal = !['queued', 'downloading', 'verifying'].includes(record.status);
            downloadBecameTerminal ||= terminal;
            if (terminal) {
                showToast(record.status === 'ready'
                    ? 'Model download completed and is available in shared storage.'
                    : record.public_error || `Model download ${record.status}.`,
                    record.status === 'ready' ? 'success' : 'error');
            }
            showDownloadStatus(record.status, record.public_error);
        } catch {
            showDownloadStatus(download.status, 'Model download status is unavailable.');
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
    if (!instances.length) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 4;
        cell.textContent = 'Metrics unavailable.';
        row.append(cell);
        inferenceMetricsBody.append(row);
        return;
    }
    for (const instance of instances) {
        const row = document.createElement('tr');
        metricCell(row, instance.instance_id.toUpperCase());
        const cpu = formatMetric(instance.cpu_utilization_percent, (value) => `${value.toFixed(1)}%`);
        const gpu = formatMetric(instance.gpu_utilization_percent, (value) => `${value.toFixed(1)}%`);
        metricCell(row, `CPU: ${cpu} | GPU: ${gpu}`);
        const ram = formatMetric(instance.ram_used_bytes, formatBytes);
        const used = formatMetric(instance.vram_used_bytes, formatBytes);
        const total = formatMetric(instance.vram_total_bytes, formatBytes);
        metricCell(row, `RAM: ${ram} | VRAM used: ${used}; total: ${total}`);
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
        renderInferenceMetrics([]);
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
            showToast('The roster changed. Reload this page before editing.', 'error');
            updateRosterSaveState();
            return;
        }
        if (response.status === 422) {
            setStatus(rosterMessage, 'Roster validation failed. Check profile fields, skill IDs, and role assignments.', 'error');
            showToast('Roster validation failed. Check profile fields and assignments.', 'error');
            updateRosterSaveState();
            return;
        }
        if (!response.ok) throw new Error('unavailable');
        const record = await response.json();
        applyRosterRecord(record);
        setStatus(rosterMessage, `Roster saved as revision ${record.revision}.`, 'success');
        showToast(`Roster saved as revision ${record.revision}.`, 'success');
    } catch {
        setStatus(rosterMessage, 'Roster could not be saved; the workflow service may be unavailable.', 'error');
        showToast('Agent roster could not be saved.', 'error');
        updateRosterSaveState();
    }
}

timeoutInput.addEventListener('input', () => {
    updateConfigSaveState();
    if (Number(timeoutInput.value) !== savedTimeout) setStatus(configMessage, 'Unsaved changes.', 'dirty');
});
document.getElementById('agent-config-form').addEventListener('submit', saveAgentConfig);
inferenceInstanceCreateForm.addEventListener('submit', createInferenceInstance);
newInferenceInstanceId.addEventListener('input', updateInferenceCreateState);
newInferenceModel.addEventListener('change', updateInferenceCreateState);
modelDownloadForm.addEventListener('submit', requestInferenceModelDownload);
modelDownloadFilename.addEventListener('input', () => {
    modelDownloadFilenameManuallyEdited = true;
    updateModelDownloadButton();
});
modelDownloadUrl.addEventListener('input', suggestModelFilenameFromUrl);
rosterForm.addEventListener('submit', saveAgentRoster);
addProfileButton.addEventListener('click', addProfile);
loadAgentConfig();
loadInferenceInstances();
loadInferenceMetrics();
loadAgentRoster();
window.setInterval(pollInferenceDownloads, 3000);
window.setInterval(loadInferenceMetrics, 15000);