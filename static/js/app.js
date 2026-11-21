'use strict';

const $ = id => document.getElementById(id);
let previewId = null;
let odooReady = false;
let createdOdooId = null;
let busy = false;

/** Enable or disable actions while a job is running. */
function setBusy(value) {
    busy = value;
    $('find').disabled = value;
    $('download').disabled = value || !previewId;
    $('createOdoo').disabled = value || !previewId || !odooReady || !!createdOdooId;
}

/** Display progress messages from the Flask background job. */
function appendLog(lines) {
    $('log').textContent = lines.length ? lines.join('\n') : 'Working...';
    $('log').scrollTop = $('log').scrollHeight;
}

/** Create an element containing text without interpreting it as HTML. */
function addText(parent, cls, value) {
    const element = document.createElement('div');
    element.className = cls;
    element.textContent = String(value);
    parent.appendChild(element);
    return element;
}

/** Show a safe link to the official HigherGov opportunity page. */
function showBidLink(opportunity) {
    const container = $('bidLink');
    container.replaceChildren();
    const bidUrl = opportunity?.path;
    if (typeof bidUrl !== 'string') return;

    let parsedUrl;
    try {
        parsedUrl = new URL(bidUrl);
    } catch {
        return;
    }
    if (parsedUrl.protocol !== 'https:' || parsedUrl.hostname !== 'www.highergov.com') return;

    const link = document.createElement('a');
    link.href = parsedUrl.href;
    link.textContent = 'View Bid on HigherGov ↗';
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    container.appendChild(link);
}

/** Render the opportunity fields and document records returned by preview. */
function showPreview(data) {
    const details = $('details');
    details.replaceChildren();
    const opportunity = data.opportunity || {};
    showBidLink(opportunity);

    Object.entries(opportunity).forEach(([key, value]) => {
        const line = document.createElement('div');
        line.className = 'kv';
        addText(line, 'key', key);
        addText(line, '', typeof value === 'object' ? JSON.stringify(value) : value);
        details.appendChild(line);
    });

    const docs = $('documents');
    docs.replaceChildren();
    if (!data.documents_checked) {
        addText(docs, 'notice', 'Documents were not checked. Download disabled.');
        return;
    }
    const documents = data.documents || [];
    addText(docs, 'status', `${documents.length} document record(s) returned`);
    documents.forEach((document, index) => {
        const filename = document.file_name || document.filename || 'Unnamed document';
        const element = addText(docs, 'doc', `${index + 1}. ${filename}`);
        addText(element, '', document.download_url ? 'Download URL available' : 'No download_url field');
    });
}

/** Submit a JSON POST request to the local Flask backend. */
async function request(path, body) {
    const response = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Request failed');
    return data;
}

/** Poll a background job until it succeeds or fails. */
async function poll(jobId) {
    while (true) {
        await new Promise(resolve => setTimeout(resolve, 650));
        const response = await fetch('/api/jobs/' + jobId);
        const data = await response.json();
        if (!response.ok) throw Error(data.error || 'Status error');
        appendLog(data.messages || []);
        if (data.state === 'error') throw Error(data.error || 'Unknown error');
        if (data.state === 'done') return data.result;
    }
}

$('find').onclick = async () => {
    const bid = $('bid_id').value.trim();
    if (!bid) {
        $('status').textContent = 'Enter a bid ID.';
        return;
    }
    previewId = null;
    createdOdooId = null;
    $('odooLink').replaceChildren();
    setBusy(true);
    $('status').textContent = 'Searching HigherGov...';
    $('details').textContent = 'Loading...';
    $('documents').textContent = 'Loading...';
    $('bidLink').replaceChildren();
    try {
        const job = await request('/api/preview', {bid_id: bid});
        const result = await poll(job.job_id);
        previewId = job.job_id;
        showPreview(result);
        $('status').textContent = 'Preview ready. Review available fields and documents.';
    } catch (error) {
        $('status').textContent = 'Error: ' + error.message;
        $('details').textContent = 'No preview available.';
        $('documents').textContent = 'No documents available.';
    } finally {
        setBusy(false);
    }
};

$('download').onclick = async () => {
    if (!previewId) return;
    setBusy(true);
    $('status').textContent = 'Downloading...';
    try {
        const job = await request('/api/download', {preview_id: previewId});
        const result = await poll(job.job_id);
        $('status').textContent = `Finished: ${result.manifest.status} · ${result.folder}`;
        if (result.manifest.failed?.length) {
            $('status').textContent += ` · ${result.manifest.failed.length} failed`;
        }
    } catch (error) {
        $('status').textContent = 'Error: ' + error.message;
    } finally {
        setBusy(false);
    }
};


/** Only enabled when an existing HigherGov bid preview is ready. */
async function checkOdoo() {
    try {
        const response = await fetch('/api/odoo/status');
        const data = await response.json();
        odooReady = Boolean(data.ready);
        $('odooStatus').textContent = odooReady
            ? 'Odoo configured. Review the bid, then click Create Opportunity.'
            : 'Odoo setup required: ' + data.message;
    } catch (error) {
        odooReady = false;
        $('odooStatus').textContent = 'Could not check Odoo configuration.';
    }
    setBusy(busy);
}

$('createOdoo').onclick = async () => {
    if (!previewId || !odooReady || createdOdooId) return;
    if (!window.confirm('Create an Odoo CRM opportunity from this HigherGov bid?')) return;
    setBusy(true);
    $('odooStatus').textContent = 'Creating opportunity in Odoo...';
    $('odooLink').replaceChildren();
    try {
        const result = await request('/api/odoo/create', {preview_id: previewId});
        createdOdooId = result.id;
        $('odooStatus').textContent = result.already_exists
            ? `Already in Odoo (opportunity #${result.id}); no duplicate created.`
            : `Created Odoo opportunity #${result.id}.`;
        const link = document.createElement('a');
        link.textContent = 'Open Opportunity in Odoo ↗';
        link.href = result.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        $('odooLink').appendChild(link);
    } catch (error) {
        $('odooStatus').textContent = 'Odoo error: ' + error.message;
    } finally {
        setBusy(false);
    }
};

checkOdoo();
