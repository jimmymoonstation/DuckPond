const API = '/api';
let currentJobPage = 0;
let pendingJobId = null;
let pendingAppId = null;

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  setupTabs();
  loadStats();
  loadJobs();
  loadConfig();
  loadScraperStatus();
  setInterval(loadStats, 30_000);
  setInterval(() => { if (activeTab() === 'board') loadJobs(false); }, 60_000);
  setInterval(loadScraperStatus, 60_000);
});

function activeTab() {
  return document.querySelector('.tab.active')?.dataset.tab;
}

// ── Tabs ──────────────────────────────────────────────────────────────────────

function setupTabs() {
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
      if (btn.dataset.tab === 'tracker') loadApplications();
      if (btn.dataset.tab === 'resumes') loadResumes();
      if (btn.dataset.tab === 'config') loadConfig();
    });
  });

  document.getElementById('btn-add-job').addEventListener('click', openAddJobModal);
  document.getElementById('aj-cancel').addEventListener('click', closeAddJobModal);
  document.getElementById('aj-confirm').addEventListener('click', confirmAddJob);
  document.getElementById('btn-refresh').addEventListener('click', () => loadJobs());
  document.getElementById('btn-scrape').addEventListener('click', triggerScrape);
  document.getElementById('search-q').addEventListener('input', debounce(() => loadJobs(), 400));
  document.getElementById('search-loc').addEventListener('input', debounce(() => loadJobs(), 400));
  document.getElementById('filter-status').addEventListener('change', loadApplications);
  document.getElementById('btn-save-config').addEventListener('click', saveConfig);
  document.getElementById('btn-add-resume').addEventListener('click', promptAddResume);
  document.getElementById('modal-cancel').addEventListener('click', closeModal);
  document.getElementById('modal-confirm').addEventListener('click', confirmApply);
  document.getElementById('status-modal-cancel').addEventListener('click', closeStatusModal);
  document.getElementById('status-modal-confirm').addEventListener('click', confirmStatusUpdate);
}

// ── Stats ─────────────────────────────────────────────────────────────────────

async function loadStats() {
  const s = await apiFetch('/stats?period=all_time');
  if (!s) return;
  document.getElementById('stat-discovered').textContent = s.jobs_discovered ?? '—';
  document.getElementById('stat-applied').textContent = s.applications?.applied ?? '—';
  document.getElementById('stat-screening').textContent = s.applications?.phone_screen ?? '—';
  document.getElementById('stat-interview').textContent = s.applications?.interview ?? '—';
  document.getElementById('stat-offer').textContent = s.applications?.offer ?? '—';
  document.getElementById('stat-daily').textContent = (s.daily_average_applications ?? 0).toFixed(1);

  const days = s.days_remaining ?? 60;
  document.getElementById('countdown').innerHTML =
    `Day <strong>${s.days_since_start ?? 1}</strong> &nbsp;·&nbsp; <strong>${days}</strong> days remaining`;
}

// ── Job Board ─────────────────────────────────────────────────────────────────

async function loadJobs(showLoading = true) {
  const tbody = document.getElementById('jobs-body');
  if (showLoading) tbody.innerHTML = '<tr><td colspan="7" class="loading">Loading…</td></tr>';

  const q = document.getElementById('search-q').value;
  const loc = document.getElementById('search-loc').value;
  const params = new URLSearchParams({ status: 'new', limit: 50, offset: currentJobPage * 50 });
  if (q) params.set('q', q);
  if (loc) params.set('location', loc);

  const data = await apiFetch(`/jobs?${params}`);
  if (!data) return;

  if (!data.jobs.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="loading">No new openings found. Configure search or run scraper.</td></tr>';
    return;
  }

  tbody.innerHTML = data.jobs.map(j => `
    <tr>
      <td>
        ${j.original_url
          ? `<a href="${j.original_url}" target="_blank" title="Open on company career site">${esc(j.job_title)}</a>
             <a href="${j.url}" target="_blank" title="Open source (${esc(j.source.split(':')[0])})" style="color:var(--text-dim);font-size:10px;margin-left:4px">↗src</a>`
          : `<a href="${j.url}" target="_blank">${esc(j.job_title)}</a>`
        }
      </td>
      <td>${esc(j.company_name)}</td>
      <td>${j.location ? esc(j.location) : '<span style="color:var(--text-dim)">—</span>'}</td>
      <td>${j.level ? `<span class="badge badge-new">${esc(j.level)}</span>` : '—'}</td>
      <td><span class="source-chip">${esc(j.source.split(':')[0])}</span></td>
      <td style="color:var(--text-dim);font-size:12px">${formatPostedDate(j.posted_at, j.discovered_at)}</td>
      <td>
        <button class="btn-primary btn-sm" onclick="openApplyModal(${j.id})">Apply</button>
        <button class="btn-secondary btn-sm" style="margin-left:4px" onclick="saveJob(${j.id})">Save</button>
      </td>
    </tr>
  `).join('');

  renderPagination(data.total, 50, currentJobPage);
}

function renderPagination(total, limit, page) {
  const pages = Math.ceil(total / limit);
  const el = document.getElementById('jobs-pagination');
  if (pages <= 1) { el.innerHTML = ''; return; }
  el.innerHTML = `
    <button class="btn-secondary btn-sm" onclick="changePage(${page - 1})" ${page === 0 ? 'disabled' : ''}>← Prev</button>
    <span style="color:var(--text-dim);font-size:12px">Page ${page + 1} / ${pages} &nbsp; (${total} total)</span>
    <button class="btn-secondary btn-sm" onclick="changePage(${page + 1})" ${page >= pages - 1 ? 'disabled' : ''}>Next →</button>
  `;
}

function changePage(p) { currentJobPage = p; loadJobs(); }

// ── Add Job Modal ─────────────────────────────────────────────────────────────

function openAddJobModal() {
  ['aj-title','aj-company','aj-url','aj-location','aj-level','aj-description'].forEach(id => {
    document.getElementById(id).value = '';
  });
  document.getElementById('aj-error').textContent = '';
  document.getElementById('add-job-overlay').classList.remove('hidden');
  document.getElementById('aj-title').focus();
}

function closeAddJobModal() {
  document.getElementById('add-job-overlay').classList.add('hidden');
}

async function confirmAddJob() {
  const title = document.getElementById('aj-title').value.trim();
  const company = document.getElementById('aj-company').value.trim();
  const url = document.getElementById('aj-url').value.trim();
  const errEl = document.getElementById('aj-error');

  if (!title || !company || !url) {
    errEl.textContent = 'Job title, company, and URL are required.';
    return;
  }

  const body = {
    job_title: title,
    company_name: company,
    url,
    location: document.getElementById('aj-location').value.trim() || null,
    level: document.getElementById('aj-level').value.trim() || null,
    description: document.getElementById('aj-description').value.trim() || null,
  };

  const res = await fetch(API + '/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (res.status === 409) {
    errEl.textContent = 'This job is already in your board.';
    return;
  }
  if (!res.ok) {
    errEl.textContent = 'Something went wrong. Try again.';
    return;
  }

  closeAddJobModal();
  loadJobs();
  loadStats();
}

// ── Apply Modal ───────────────────────────────────────────────────────────────

async function openApplyModal(jobId) {
  pendingJobId = jobId;
  const resumes = await apiFetch('/resumes');
  const sel = document.getElementById('modal-resume');
  sel.innerHTML = '<option value="">No resume selected</option>' +
    (resumes?.resumes || []).map(r => `<option value="${r.id}">${esc(r.name)} ${r.version ? `v${r.version}` : ''}</option>`).join('');
  document.getElementById('modal-notes').value = '';
  document.getElementById('modal-overlay').classList.remove('hidden');
}

function closeModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  pendingJobId = null;
}

async function confirmApply() {
  if (!pendingJobId) return;
  const resumeId = document.getElementById('modal-resume').value;
  const notes = document.getElementById('modal-notes').value;
  await apiFetch('/applications', {
    method: 'POST',
    body: JSON.stringify({ job_id: pendingJobId, resume_id: resumeId ? +resumeId : null, notes, status: 'applied' }),
  });
  closeModal();
  loadJobs();
  loadStats();
}

async function saveJob(jobId) {
  await apiFetch('/applications', {
    method: 'POST',
    body: JSON.stringify({ job_id: jobId, status: 'saved' }),
  });
  loadJobs();
}

// ── Application Tracker ───────────────────────────────────────────────────────

async function loadApplications() {
  const tbody = document.getElementById('apps-body');
  tbody.innerHTML = '<tr><td colspan="7" class="loading">Loading…</td></tr>';
  const status = document.getElementById('filter-status').value;
  const params = new URLSearchParams({ limit: 100 });
  if (status) params.set('status', status);
  const data = await apiFetch(`/applications?${params}`);
  if (!data?.applications.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="loading">No applications yet. Start applying from the Job Board.</td></tr>';
    return;
  }
  tbody.innerHTML = data.applications.map(a => `
    <tr>
      <td><a href="${a.job.url}" target="_blank">${esc(a.job.job_title)}</a></td>
      <td>${esc(a.job.company_name)}</td>
      <td style="font-size:12px;color:var(--text-dim)">${a.applied_at ? formatDate(a.applied_at) : '—'}</td>
      <td><span class="badge badge-${a.status}">${a.status.replace('_', ' ')}</span></td>
      <td style="font-size:12px;color:var(--text-dim)">${a.resume?.name ?? '—'}</td>
      <td class="notes-cell">${esc(a.notes || '')}</td>
      <td>
        <button class="btn-secondary btn-sm" onclick="openStatusModal(${a.id}, '${a.status}')">Update</button>
      </td>
    </tr>
  `).join('');
}

// ── Status Modal ──────────────────────────────────────────────────────────────

function openStatusModal(appId, currentStatus) {
  pendingAppId = appId;
  document.getElementById('status-modal-select').value = currentStatus;
  document.getElementById('status-modal-notes').value = '';
  document.getElementById('status-modal-overlay').classList.remove('hidden');
}

function closeStatusModal() {
  document.getElementById('status-modal-overlay').classList.add('hidden');
  pendingAppId = null;
}

async function confirmStatusUpdate() {
  if (!pendingAppId) return;
  const status = document.getElementById('status-modal-select').value;
  const notes = document.getElementById('status-modal-notes').value;
  await apiFetch(`/applications/${pendingAppId}`, {
    method: 'PATCH',
    body: JSON.stringify({ status, notes }),
  });
  closeStatusModal();
  loadApplications();
  loadStats();
}

// ── Config ────────────────────────────────────────────────────────────────────

async function loadConfig() {
  const cfg = await apiFetch('/config');
  if (!cfg) return;
  document.getElementById('cfg-titles').value = cfg.titles.join(', ');
  document.getElementById('cfg-locations').value = cfg.locations.join(', ');
  document.getElementById('cfg-levels').value = cfg.levels.join(', ');
  document.getElementById('cfg-keywords').value = cfg.keywords.join(', ');
  document.getElementById('cfg-excluded').value = cfg.excluded_companies.join(', ');
}

async function saveConfig() {
  const parse = id => document.getElementById(id).value.split(',').map(s => s.trim()).filter(Boolean);
  const body = {
    titles: parse('cfg-titles'),
    locations: parse('cfg-locations'),
    levels: parse('cfg-levels'),
    keywords: parse('cfg-keywords'),
    excluded_companies: parse('cfg-excluded'),
  };
  const msg = document.getElementById('config-msg');
  msg.textContent = 'Saving…';
  await apiFetch('/config', { method: 'PUT', body: JSON.stringify(body) });
  msg.textContent = 'Saved! Scraper running…';
  setTimeout(() => { msg.textContent = ''; }, 3000);
}

// ── Resumes ───────────────────────────────────────────────────────────────────

async function loadResumes() {
  const tbody = document.getElementById('resumes-body');
  tbody.innerHTML = '<tr><td colspan="5" class="loading">Loading…</td></tr>';
  const data = await apiFetch('/resumes');
  if (!data?.resumes.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="loading">No resumes yet. Add your first one.</td></tr>';
    return;
  }
  tbody.innerHTML = data.resumes.map(r => `
    <tr>
      <td>${esc(r.name)}</td>
      <td>${r.version ?? '—'}</td>
      <td>${(r.tags || []).map(t => `<span class="source-chip">${esc(t)}</span>`).join(' ')}</td>
      <td style="font-size:12px;color:var(--text-dim)">${formatDate(r.created_at)}</td>
      <td>
        <button class="btn-secondary btn-sm" onclick="deleteResume(${r.id})">Delete</button>
      </td>
    </tr>
  `).join('');
}

function promptAddResume() {
  const name = prompt('Resume name (e.g. "SWE Backend v3"):');
  if (!name) return;
  const version = prompt('Version (optional, e.g. "3.0"):') || null;
  const tagsRaw = prompt('Tags (comma-separated, e.g. backend,python,senior):') || '';
  const tags = tagsRaw.split(',').map(s => s.trim()).filter(Boolean);
  apiFetch('/resumes', {
    method: 'POST',
    body: JSON.stringify({ name, version, tags, content_json: {} }),
  }).then(() => loadResumes());
}

async function deleteResume(id) {
  if (!confirm('Delete this resume version?')) return;
  await apiFetch(`/resumes/${id}`, { method: 'DELETE' });
  loadResumes();
}

// ── Scraper ───────────────────────────────────────────────────────────────────

async function loadScraperStatus() {
  const data = await apiFetch('/scraper/status');
  if (!data) return;
  const el = document.getElementById('last-searched');
  if (data.last_run) {
    const ago = timeAgo(data.last_run);
    const found = data.jobs_found_last_run;
    const runs = data.total_runs;
    el.textContent = `Last searched ${ago}  ·  ${found} new jobs  ·  ${runs} runs`;
    el.title = `Full timestamp: ${new Date(data.last_run + 'Z').toLocaleString()}`;
  } else {
    el.textContent = 'Scraper not yet run';
  }
}

async function triggerScrape() {
  const el = document.getElementById('scraper-status');
  el.textContent = 'Running…';
  await apiFetch('/scraper/run', { method: 'POST' });
  el.textContent = 'Scraper triggered. Results in ~30s.';
  setTimeout(() => { el.textContent = ''; loadJobs(); loadStats(); loadScraperStatus(); }, 35_000);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

async function apiFetch(path, opts = {}) {
  try {
    const res = await fetch(API + path, {
      headers: { 'Content-Type': 'application/json', ...opts.headers },
      ...opts,
    });
    if (res.status === 204) return null;
    return res.ok ? res.json() : null;
  } catch (e) {
    console.error('API error:', e);
    return null;
  }
}

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function formatDate(iso) {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function timeAgo(iso) {
  if (!iso) return '—';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function formatPostedDate(posted_at, discovered_at) {
  const dt = posted_at || discovered_at;
  if (!dt) return '—';
  const label = posted_at ? 'posted' : 'found';
  const exact = new Date(dt).toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  });
  return `<span title="${label} ${exact}">${timeAgo(dt)}</span>`;
}

function debounce(fn, delay) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), delay); };
}
