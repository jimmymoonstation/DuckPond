const API = '/api';
let currentJobPage = 0;
let pendingJobId = null;
let pendingAppId = null;
let jobSort = { by: 'discovered_at', dir: 'desc' };
let careerSites = {};        // company name (lowercase) → career homepage URL (from scraper const)
let trackedCompanies = {};   // company name (lowercase) → career_url|null  (all active tracked)

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  // Load career sites + tracked company names (parallel)
  const [sites, tracked] = await Promise.all([
    apiFetch('/jobs/career-sites'),
    apiFetch('/companies/names'),
  ]);
  if (sites)   careerSites = sites;
  if (tracked) trackedCompanies = tracked;

  setupTabs();
  markPondViewed(); // opening the app on The Pond counts as viewing it
  loadStats();
  loadJobs();
  loadConfig();
  loadScraperStatus();
  setInterval(loadStats, 30_000);
  setInterval(() => { if (activeTab() === 'board') loadJobs(false); else checkNewJobs(); }, 60_000);
  setInterval(loadScraperStatus, 60_000);
});

function activeTab() {
  return document.querySelector('.tab.active')?.dataset.tab;
}

// ── Tabs ──────────────────────────────────────────────────────────────────────

// ── New-jobs duck badge ───────────────────────────────────────────────────────

function markPondViewed() {
  localStorage.setItem('pondLastViewed', new Date().toISOString());
  document.getElementById('pond-badge').classList.remove('visible');
}

async function checkNewJobs() {
  const badge = document.getElementById('pond-badge');
  if (!badge) return;
  // Don't show badge while user is already on The Pond
  if (activeTab() === 'board') { markPondViewed(); return; }
  const lastViewed = localStorage.getItem('pondLastViewed');
  const data = await apiFetch('/jobs?status=new&limit=1&sort_by=discovered_at&sort_dir=desc');
  if (!data?.jobs?.length) return;
  const newestAt = data.jobs[0].discovered_at;
  if (!newestAt) return;
  if (!lastViewed || newestAt > lastViewed) {
    badge.classList.add('visible');
  }
}

function switchTab(tabName, pushState = true) {
  const btn = document.querySelector(`.tab[data-tab="${tabName}"]`);
  if (!btn) return;
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(`tab-${tabName}`).classList.add('active');
  if (pushState) history.pushState(null, '', `#${tabName}`);
  if (tabName === 'board')     markPondViewed();
  if (tabName === 'tracker')   loadApplications();
  if (tabName === 'resumes')   loadResumes();
  if (tabName === 'config')    loadConfig();
  if (tabName === 'companies') loadCompanies();
  if (tabName === 'mailbox')     loadMailbox();
  if (tabName === 'messages')   loadLinkedInMessages();
  if (tabName === 'analysis')   loadAnalysis();
  if (tabName === 'interviews') loadInterviewPrep();
  if (tabName === 'usage')      loadUsage();
}

function setupTabs() {
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      switchTab(btn.dataset.tab);
    });
  });

  // Restore tab from URL hash on load
  const hash = location.hash.replace('#', '');
  if (hash && document.querySelector(`.tab[data-tab="${hash}"]`)) {
    switchTab(hash, false);
  }

  // Handle browser back/forward
  window.addEventListener('popstate', () => {
    const h = location.hash.replace('#', '') || 'board';
    switchTab(h, false);
  });

  window.switchToTracker = () => switchTab('tracker');

  document.getElementById('btn-add-job').addEventListener('click', openAddJobModal);
  document.getElementById('aj-cancel').addEventListener('click', closeAddJobModal);
  document.getElementById('aj-confirm').addEventListener('click', confirmAddJob);
  document.getElementById('btn-refresh').addEventListener('click', () => loadJobs());
  document.getElementById('btn-scrape').addEventListener('click', triggerScrape);
  document.getElementById('search-q').addEventListener('input', debounce(() => loadJobs(), 400));
  document.getElementById('search-loc').addEventListener('input', debounce(() => loadJobs(), 400));
  document.getElementById('filter-status').addEventListener('change', loadApplications);
  document.getElementById('btn-save-config').addEventListener('click', saveConfig);
  document.getElementById('btn-save-notion').addEventListener('click', saveNotionConfig);
  document.getElementById('btn-test-notion').addEventListener('click', testNotionConnection);
  setupResumeModal();
  document.getElementById('modal-cancel').addEventListener('click', closeModal);
  document.getElementById('modal-confirm').addEventListener('click', confirmApply);
  document.getElementById('status-modal-cancel').addEventListener('click', closeStatusModal);
  document.getElementById('status-modal-confirm').addEventListener('click', confirmStatusUpdate);
  setupCompanyModal();
  setupFeedbackModal();
  setupPortal();
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

function sortBy(col) {
  if (jobSort.by === col) {
    jobSort.dir = jobSort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    jobSort.by = col;
    jobSort.dir = col === 'posted_at' || col === 'discovered_at' ? 'desc' : 'asc';
  }
  currentJobPage = 0;
  loadJobs();
}

function renderSortHeaders() {
  const cols = [
    { key: 'job_title',    label: 'Job Title' },
    { key: 'company_name', label: 'Company' },
    { key: 'location',     label: 'Location' },
    { key: 'level',        label: 'Level' },
    { key: 'source',       label: 'Source' },
    { key: 'posted_at',    label: 'Posted' },
    { key: 'discovered_at', label: 'Found' },
    { key: null,           label: 'Actions' },
  ];
  const thead = document.querySelector('#jobs-table thead tr');
  thead.innerHTML = cols.map(c => {
    if (!c.key) return `<th>${c.label}</th>`;
    const active = jobSort.by === c.key;
    const arrow = active ? (jobSort.dir === 'asc' ? ' ▲' : ' ▼') : ' ⇅';
    return `<th class="sortable${active ? ' sort-active' : ''}" onclick="sortBy('${c.key}')">${c.label}<span class="sort-arrow">${arrow}</span></th>`;
  }).join('');
}

async function loadJobs(showLoading = true) {
  renderSortHeaders();
  const tbody = document.getElementById('jobs-body');
  if (showLoading) tbody.innerHTML = '<tr><td colspan="8" class="loading">Loading…</td></tr>';

  const q = document.getElementById('search-q').value;
  const loc = document.getElementById('search-loc').value;
  const params = new URLSearchParams({
    status: 'new', limit: 50, offset: currentJobPage * 50,
    sort_by: jobSort.by, sort_dir: jobSort.dir,
  });
  if (q) params.set('q', q);
  if (loc) params.set('location', loc);

  const data = await apiFetch(`/jobs?${params}`);
  if (!data) {
    tbody.innerHTML = '<tr><td colspan="8" class="loading">Failed to load — <a href="#" onclick="loadJobs();return false" style="color:#3ddc6b">Retry</a></td></tr>';
    return;
  }

  if (!data.jobs.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="loading">No new openings found. Configure search or run scraper.</td></tr>';
    return;
  }

  try {
  tbody.innerHTML = data.jobs.map(j => `
    <tr>
      <td>
        <a href="${esc(j.url)}" target="_blank" title="Open job posting">${esc(j.job_title)}</a>
        ${j.original_url
          ? `<a href="${esc(j.original_url)}" target="_blank" title="View LinkedIn post" style="color:var(--text-dim);font-size:10px;margin-left:5px;text-decoration:none">↗LI</a>`
          : ''}
      </td>
      <td>${companyLink(j.company_name)}</td>
      <td>${j.location ? esc(j.location) : '<span style="color:var(--text-dim)">—</span>'}</td>
      <td>${j.level ? `<span class="badge badge-new">${esc(j.level)}</span>` : '—'}</td>
      <td>
        <span class="source-chip">${esc(j.source.split(':')[0])}</span>
        ${j.tags && JSON.parse(j.tags).includes('yc')
          ? `<span class="badge badge-yc" title="Y Combinator startup">YC</span>`
          : ''}
      </td>
      <td style="color:var(--text-dim);font-size:12px">${j.posted_at ? `<span title="${fmtExact(j.posted_at)}">${timeAgo(j.posted_at)}</span>` : '<span style="color:var(--border)">—</span>'}</td>
      <td style="color:var(--text-dim);font-size:12px"><span title="${fmtExact(j.discovered_at)}">${timeAgo(j.discovered_at)}</span></td>
      <td>
        ${j.already_applied
          ? `<span class="badge badge-applied" title="You already applied to this role at ${esc(j.company_name)}">✓ Applied</span>`
          : `<button class="btn-primary btn-sm" onclick="openApplyModal(${j.id})">Apply</button>`
        }
        <button class="btn-secondary btn-sm" style="margin-left:4px" onclick="saveJob(${j.id})">Save</button>
        <button class="btn-feedback btn-sm" style="margin-left:4px" onclick="openFeedbackModal(${j.id})" data-label="${esc(j.job_title)} @ ${esc(j.company_name)}">✕</button>
      </td>
    </tr>
  `).join('');
  } catch(err) {
    console.error('Jobs render error:', err);
    tbody.innerHTML = '<tr><td colspan="8" class="loading">Render error — <a href="#" onclick="loadJobs();return false" style="color:#3ddc6b">Retry</a></td></tr>';
    return;
  }

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
  sel.innerHTML = '<option value="">— No resume —</option>' +
    (resumes?.resumes || []).map(r =>
      `<option value="${r.id}">${esc(r.name)}${r.version ? ` v${r.version}` : ''}${r.file_path ? ' 📄' : ''}</option>`
    ).join('');
  const lastResumeId = localStorage.getItem('lastResumeId');
  if (lastResumeId && sel.querySelector(`option[value="${lastResumeId}"]`)) {
    sel.value = lastResumeId;
  }
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
  if (resumeId) localStorage.setItem('lastResumeId', resumeId);
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

let _appsData = [];
let _appsSortState = { by: 'applied', dir: 'desc' };

const _STATUS_ORDER = { applied: 0, assessment: 1, interview: 2, offered: 3, rejected: 4 };

async function loadApplications() {
  const tbody = document.getElementById('apps-body');
  tbody.innerHTML = '<tr><td colspan="7" class="loading">Loading…</td></tr>';
  const status = document.getElementById('filter-status').value;
  const params = new URLSearchParams({ limit: 200 });
  if (status) params.set('status', status);
  const data = await apiFetch(`/applications?${params}`);
  if (!data) { tbody.innerHTML = '<tr><td colspan="7" class="loading">Could not load — click Refresh to retry.</td></tr>'; return; }
  if (!data.applications.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="loading">No applications yet. Start applying from the Job Board.</td></tr>';
    return;
  }
  _appsData = data.applications;
  _renderAppsTable();
}

function appsSort(col) {
  if (_appsSortState.by === col) {
    _appsSortState.dir = _appsSortState.dir === 'asc' ? 'desc' : 'asc';
  } else {
    _appsSortState.by = col;
    _appsSortState.dir = col === 'applied' ? 'desc' : 'asc';
  }
  document.querySelectorAll('#apps-thead .sortable').forEach(th => {
    th.classList.remove('sort-active');
    th.querySelector('.sort-arrow').textContent = ' ⇅';
  });
  const colIdx = { title: 0, company: 1, applied: 2, status: 3 };
  const ths = document.querySelectorAll('#apps-thead th');
  const th = ths[colIdx[col]];
  if (th) {
    th.classList.add('sort-active');
    th.querySelector('.sort-arrow').textContent = _appsSortState.dir === 'asc' ? ' ▲' : ' ▼';
  }
  _renderAppsTable();
}

function _renderAppsTable() {
  const tbody = document.getElementById('apps-body');
  const q = (document.getElementById('apps-search')?.value || '').toLowerCase().trim();

  const filtered = q
    ? _appsData.filter(a =>
        a.job.job_title.toLowerCase().includes(q) ||
        a.job.company_name.toLowerCase().includes(q) ||
        (a.notes || '').toLowerCase().includes(q)
      )
    : _appsData;

  const { by, dir } = _appsSortState;
  const sorted = [...filtered].sort((a, b) => {
    let av, bv;
    if (by === 'title')   { av = a.job.job_title.toLowerCase();   bv = b.job.job_title.toLowerCase(); }
    else if (by === 'company') { av = a.job.company_name.toLowerCase(); bv = b.job.company_name.toLowerCase(); }
    else if (by === 'applied') { av = a.applied_at || ''; bv = b.applied_at || ''; }
    else if (by === 'status')  { av = _STATUS_ORDER[a.status] ?? 99; bv = _STATUS_ORDER[b.status] ?? 99; }
    if (av < bv) return dir === 'asc' ? -1 : 1;
    if (av > bv) return dir === 'asc' ? 1 : -1;
    return 0;
  });
  tbody.innerHTML = sorted.map(a => `
    <tr>
      <td>
        <a href="${esc(a.job.url)}" target="_blank">${esc(a.job.job_title)}</a>
        ${a.job.original_url
          ? `<a href="${esc(a.job.original_url)}" target="_blank" title="View LinkedIn post" style="color:var(--text-dim);font-size:10px;margin-left:5px;text-decoration:none">↗LI</a>`
          : ''}
      </td>
      <td>${appCompanyCell(a)}</td>
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

function isCompanyTracked(name) {
  const nk = normCoKey(name);
  if (name.toLowerCase() in trackedCompanies) return true;
  return Object.keys(trackedCompanies).some(k => normCoKey(k) === nk);
}

function appCompanyCell(app) {
  const name      = app.job.company_name;
  const careerUrl = getCareerUrl(name);
  const tracked   = isCompanyTracked(name);

  if (careerUrl) {
    return `<a href="${esc(careerUrl)}" target="_blank" class="company-tracked" title="Open career site">${esc(name)}</a>`;
  }
  if (tracked) {
    return `<span class="company-tracked" title="Tracked — no career URL">${esc(name)}</span>`;
  }
  return `<span class="company-untracked">${esc(name)}</span>`
    + `<button class="btn-discover" id="disc-${app.id}" onclick="discoverCompany('${esc(name)}',${app.id})" title="Auto-research ${esc(name)} and add to tracker">🔍</button>`;
}

async function discoverCompany(name, appId) {
  const btn = document.getElementById(`disc-${appId}`);
  if (btn) { btn.textContent = '⏳'; btn.disabled = true; }

  const res = await apiFetch('/companies/auto-discover', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ company_name: name }),
  });

  if (res?.status === 'added' || res?.status === 'exists') {
    // Refresh tracked list then re-render
    const tracked = await apiFetch('/companies/names');
    if (tracked) trackedCompanies = tracked;
    _renderAppsTable();
  } else {
    if (btn) {
      btn.textContent = '✕';
      btn.title = res?.message || 'Not found on any ATS';
      btn.style.color = 'var(--red)';
      btn.disabled = false;
    }
  }
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

// ── Feedback ──────────────────────────────────────────────────────────────────

let feedbackJobId = null;
let feedbackSelectedTags = new Set();

function openFeedbackModal(jobId) {
  feedbackJobId = jobId;
  feedbackSelectedTags = new Set();
  const btn = document.querySelector(`button[onclick="openFeedbackModal(${jobId})"]`);
  document.getElementById('feedback-job-title').textContent = btn ? btn.dataset.label : '';
  document.getElementById('feedback-text').value = '';
  document.querySelectorAll('#feedback-chips .chip').forEach(c => c.classList.remove('chip-active'));
  document.getElementById('feedback-overlay').classList.remove('hidden');
}

function closeFeedbackModal() {
  feedbackJobId = null;
  document.getElementById('feedback-overlay').classList.add('hidden');
}

async function submitFeedback() {
  if (!feedbackJobId) return;
  const tags = [...feedbackSelectedTags].join(', ');
  const note = document.getElementById('feedback-text').value.trim();
  const feedback = [tags, note].filter(Boolean).join(' — ');
  if (!feedback) { closeFeedbackModal(); return; }

  await apiFetch(`/jobs/${feedbackJobId}/feedback`, {
    method: 'POST',
    body: JSON.stringify({ feedback }),
  });
  const removedId = feedbackJobId;
  closeFeedbackModal();
  document.querySelector(`button[onclick="openFeedbackModal(${removedId})"]`)?.closest('tr')?.remove();
  loadStats();
}

function setupFeedbackModal() {
  document.getElementById('feedback-cancel').addEventListener('click', closeFeedbackModal);
  document.getElementById('feedback-confirm').addEventListener('click', submitFeedback);
  document.querySelectorAll('#feedback-chips .chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const tag = chip.dataset.tag;
      if (feedbackSelectedTags.has(tag)) {
        feedbackSelectedTags.delete(tag);
        chip.classList.remove('chip-active');
      } else {
        feedbackSelectedTags.add(tag);
        chip.classList.add('chip-active');
      }
    });
  });
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
  loadNotionConfig();
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
  tbody.innerHTML = '<tr><td colspan="6" class="loading">Loading…</td></tr>';
  const data = await apiFetch('/resumes');
  if (!data) { tbody.innerHTML = '<tr><td colspan="6" class="loading">Could not load — click Refresh to retry.</td></tr>'; return; }
  if (!data.resumes.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="loading">No resumes yet. Upload your first one.</td></tr>';
    return;
  }
  tbody.innerHTML = data.resumes.map(r => {
    const hasFile = !!r.file_path;
    const fileCell = hasFile
      ? `<a href="${API}/resumes/${r.id}/file" target="_blank" class="file-download-link" title="Download ${esc(r.name)}">⬇ Download</a>`
      : `<span style="color:var(--text-dim)">—</span>`;
    return `
    <tr>
      <td style="font-weight:500">${esc(r.name)}</td>
      <td>${r.version ? `<span class="source-chip">v${esc(r.version)}</span>` : '—'}</td>
      <td>${(r.tags || []).map(t => `<span class="source-chip">${esc(t)}</span>`).join(' ')}</td>
      <td>${fileCell}</td>
      <td style="font-size:12px;color:var(--text-dim)">${formatDate(r.created_at)}</td>
      <td>
        <button class="btn-secondary btn-sm" onclick="deleteResume(${r.id})">Delete</button>
      </td>
    </tr>`;
  }).join('');
}

// ── Add Resume Modal ──────────────────────────────────────────────────────────

let _rmFile = null;

function openResumeModal() {
  _rmFile = null;
  ['rm-name','rm-version','rm-tags'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('rm-drop-label').textContent = 'Drop file here or click to browse';
  document.getElementById('rm-error').textContent = '';
  document.getElementById('rm-confirm').disabled = false;
  document.getElementById('rm-confirm').textContent = 'Upload & Save';
  document.getElementById('resume-overlay').classList.remove('hidden');
  document.getElementById('rm-name').focus();
}

function closeResumeModal() {
  document.getElementById('resume-overlay').classList.add('hidden');
}

function setupResumeModal() {
  document.getElementById('btn-add-resume').addEventListener('click', openResumeModal);
  document.getElementById('rm-cancel').addEventListener('click', closeResumeModal);
  document.getElementById('rm-confirm').addEventListener('click', submitResume);

  const drop = document.getElementById('rm-drop');
  const fileInput = document.getElementById('rm-file');

  drop.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) setResumeFile(fileInput.files[0]);
  });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop.addEventListener('drop', e => {
    e.preventDefault();
    drop.classList.remove('drag-over');
    if (e.dataTransfer.files[0]) setResumeFile(e.dataTransfer.files[0]);
  });
}

function setResumeFile(file) {
  _rmFile = file;
  document.getElementById('rm-drop-label').textContent = `✓ ${file.name}`;
  document.getElementById('rm-drop').classList.add('file-selected');
  // Auto-fill name from filename if empty
  const nameEl = document.getElementById('rm-name');
  if (!nameEl.value) {
    nameEl.value = file.name.replace(/\.[^.]+$/, '').replace(/[-_]/g, ' ');
  }
}

async function submitResume() {
  const name = document.getElementById('rm-name').value.trim();
  const errEl = document.getElementById('rm-error');
  if (!name) { errEl.textContent = 'Name is required.'; return; }
  if (!_rmFile) { errEl.textContent = 'Please select a file.'; return; }

  const btn = document.getElementById('rm-confirm');
  btn.disabled = true;
  btn.textContent = 'Uploading…';

  const form = new FormData();
  form.append('file', _rmFile);
  form.append('name', name);
  const version = document.getElementById('rm-version').value.trim();
  if (version) form.append('version', version);
  form.append('tags', document.getElementById('rm-tags').value.trim());

  try {
    const resp = await fetch(`${API}/resumes/upload`, { method: 'POST', body: form });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      errEl.textContent = err.detail || `Upload failed (${resp.status})`;
      btn.disabled = false;
      btn.textContent = 'Upload & Save';
      return;
    }
    closeResumeModal();
    loadResumes();
  } catch (e) {
    errEl.textContent = 'Could not reach server.';
    btn.disabled = false;
    btn.textContent = 'Upload & Save';
  }
}

async function deleteResume(id) {
  if (!confirm('Delete this resume version? The file will also be removed.')) return;
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

async function apiFetch(path, opts = {}, _retries = 2) {
  for (let attempt = 0; attempt <= _retries; attempt++) {
    try {
      const res = await fetch(API + path, {
        headers: { 'Content-Type': 'application/json', ...opts.headers },
        ...opts,
      });
      if (res.status === 204) return null;
      if (res.ok) return res.json();
      return null;
    } catch (e) {
      if (attempt < _retries) {
        await new Promise(r => setTimeout(r, 600 * (attempt + 1)));
      } else {
        console.error('API error after retries:', path, e);
      }
    }
  }
  return null;
}

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function normCoKey(name) {
  return name.toLowerCase().replace(/[\s\-_.,&'"]+/g, '');
}

function getCareerUrl(name) {
  const exact = trackedCompanies[name.toLowerCase()];
  if (exact) return exact;
  const norm = normCoKey(name);
  for (const [k, v] of Object.entries(trackedCompanies)) {
    if (normCoKey(k) === norm) return v;
  }
  return careerSites[name.toLowerCase()] || null;
}

function companyLink(name) {
  const url = getCareerUrl(name);
  return url
    ? `<a href="${esc(url)}" target="_blank" title="Open ${esc(name)} careers page">${esc(name)}</a>`
    : esc(name);
}

const _PT = 'America/Los_Angeles';

function formatDate(iso) {
  if (!iso) return '—';
  const ts = iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z';
  return new Date(ts).toLocaleString('en-US', {
    timeZone: _PT,
    month: 'short', day: 'numeric', year: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  });
}

function timeAgo(iso) {
  if (!iso) return '—';
  // Ensure UTC parsing — server returns ISO strings without Z suffix
  const ts = iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return formatDate(iso);
}

function fmtExact(iso) {
  if (!iso) return '';
  const ts = iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z';
  return new Date(ts).toLocaleString('en-US', {
    timeZone: _PT,
    month: 'short', day: 'numeric', year: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  });
}

function debounce(fn, delay) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), delay); };
}

// ── Companies ─────────────────────────────────────────────────────────────────

const ATS_BADGE = {
  greenhouse:      { label: 'Greenhouse',      cls: 'badge-green'  },
  lever:           { label: 'Lever',           cls: 'badge-blue'   },
  ashby:           { label: 'Ashby',           cls: 'badge-purple' },
  workday:         { label: 'Workday',         cls: 'badge-orange' },
  smartrecruiters: { label: 'SmartRecruiters', cls: 'badge-teal'   },
  amazon:          { label: 'Amazon',          cls: 'badge-yellow' },
  custom:          { label: 'Custom',          cls: 'badge-gray'   },
};

// ── Company Portal ────────────────────────────────────────────────────────────

function _portalMsg(text, side, style = '') {
  const log = document.getElementById('portal-log');
  const avatar = side === 'user' ? '🧑' : '🦆';
  const div = document.createElement('div');
  div.className = `portal-msg ${side}${style ? ' ' + style : ''}`;
  div.innerHTML = `
    <div class="portal-avatar">${avatar}</div>
    <div class="portal-bubble">${text}</div>`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function setupPortal() {
  const input = document.getElementById('portal-input');
  const btn   = document.getElementById('portal-submit');

  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    input.focus();

    _portalMsg(esc(text), 'user');
    const spinner = _portalMsg('Looking up…', 'bot', 'spin');

    const result = await apiFetch('/companies/ingest', {
      method: 'POST',
      body: JSON.stringify({ text }),
    });

    spinner.remove();

    if (!result) {
      _portalMsg('⚠️ Server error — could not process the request.', 'bot', 'error');
      return;
    }

    const styleMap = { added: 'ok', exists: 'warn', not_found: 'warn', error: 'error' };
    const icon     = { added: '✅', exists: 'ℹ️', not_found: '🔍', error: '❌' };
    const botStyle = styleMap[result.status] || '';
    _portalMsg(`${icon[result.status] || ''} ${esc(result.message)}`, 'bot', botStyle);

    if (result.status === 'added') {
      // Refresh the company list below
      await loadCompanies();
    }
  }

  btn.addEventListener('click', submit);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
}

let allCompanies = [];

let _coSortState = { by: 'added_at', dir: 'desc' };

async function loadCompanies() {
  const data = await apiFetch('/companies');
  if (!data) return;
  allCompanies = data;
  renderCompanies();

  document.getElementById('co-search').oninput = debounce(renderCompanies, 250);
  document.getElementById('co-filter-ats').onchange = renderCompanies;
  document.getElementById('co-filter-active').onchange = renderCompanies;
}

function companiesSort(col) {
  if (_coSortState.by === col) {
    _coSortState.dir = _coSortState.dir === 'asc' ? 'desc' : 'asc';
  } else {
    _coSortState.by  = col;
    _coSortState.dir = col === 'added_at' ? 'desc' : 'asc';
  }
  // Update header arrows
  document.querySelectorAll('#companies-thead .sortable').forEach(th => {
    th.classList.remove('sort-active');
    th.querySelector('.sort-arrow').textContent = ' ⇅';
  });
  const colIdx = { company_name: 0, ats_type: 1, added_at: 5 };
  const th = document.querySelectorAll('#companies-thead th')[colIdx[col]];
  if (th) {
    th.classList.add('sort-active');
    th.querySelector('.sort-arrow').textContent = _coSortState.dir === 'asc' ? ' ▲' : ' ▼';
  }
  renderCompanies();
}

function renderCompanies() {
  const q           = document.getElementById('co-search').value.toLowerCase();
  const atsFilter   = document.getElementById('co-filter-ats').value;
  const activeFilter = document.getElementById('co-filter-active').value;

  let filtered = allCompanies.filter(c => {
    if (q && !c.company_name.toLowerCase().includes(q) &&
             !c.ats_slug.toLowerCase().includes(q) &&
             !c.ats_type.toLowerCase().includes(q)) return false;
    if (atsFilter && c.ats_type !== atsFilter) return false;
    if (activeFilter === 'true'  && !c.is_active) return false;
    if (activeFilter === 'false' &&  c.is_active) return false;
    return true;
  });

  // Sort
  const { by, dir } = _coSortState;
  filtered = [...filtered].sort((a, b) => {
    const av = (a[by] || '').toLowerCase();
    const bv = (b[by] || '').toLowerCase();
    return dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
  });

  const tbody = document.getElementById('companies-body');
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="loading">No companies match.</td></tr>';
    return;
  }

  const SOURCE_LABEL = { manual: 'Manual', seed: 'Seed', auto: 'Auto', portal: 'Portal' };

  tbody.innerHTML = filtered.map(c => {
    const badge = ATS_BADGE[c.ats_type] || { label: c.ats_type, cls: 'badge-gray' };
    const careerLink = c.career_url
      ? `<a href="${esc(c.career_url)}" target="_blank" title="Open career page">↗</a>`
      : '—';
    const addedDate = c.added_at ? formatDate(c.added_at) : '—';
    const sourceLabel = SOURCE_LABEL[c.discovered_from] || c.discovered_from;
    const activeToggle = c.is_active
      ? `<button class="btn-tiny btn-active"   onclick="toggleCompany(${c.id}, false)">✓ Active</button>`
      : `<button class="btn-tiny btn-inactive" onclick="toggleCompany(${c.id}, true)">✗ Inactive</button>`;
    return `<tr class="${c.is_active ? '' : 'row-inactive'}">
      <td>${esc(c.company_name)}</td>
      <td><span class="badge ${badge.cls}">${badge.label}</span></td>
      <td><code>${esc(c.ats_slug)}</code></td>
      <td>${careerLink}</td>
      <td style="font-size:12px;color:var(--text-dim)">${sourceLabel}</td>
      <td style="font-size:12px;color:var(--text-dim)">${addedDate}</td>
      <td>${activeToggle}</td>
      <td><button class="btn-tiny btn-danger" onclick="deleteCompany(${c.id}, '${esc(c.company_name)}')">Delete</button></td>
    </tr>`;
  }).join('');
}

async function toggleCompany(id, active) {
  await apiFetch(`/companies/${id}`, { method: 'PATCH', body: JSON.stringify({ is_active: active }) });
  const c = allCompanies.find(x => x.id === id);
  if (c) c.is_active = active;
  renderCompanies();
}

async function deleteCompany(id, name) {
  if (!confirm(`Delete ${name}? This cannot be undone.`)) return;
  const res = await fetch(`${API}/companies/${id}`, { method: 'DELETE' });
  if (res.ok) {
    allCompanies = allCompanies.filter(c => c.id !== id);
    renderCompanies();
  }
}

function setupCompanyModal() {
  document.getElementById('btn-discover').addEventListener('click', async () => {
    const btn = document.getElementById('btn-discover');
    btn.disabled = true;
    btn.textContent = '🔍 Discovering…';
    const result = await apiFetch('/scraper/discover', { method: 'POST' });
    btn.disabled = false;
    btn.textContent = '🔍 Auto-Discover';
    if (result) alert('Discovery started! New companies will appear in the list within a few minutes.');
  });

  document.getElementById('btn-add-company').addEventListener('click', () => {
    document.getElementById('co-name').value = '';
    document.getElementById('co-slug').value = '';
    document.getElementById('co-board').value = '';
    document.getElementById('co-wd-ver').value = 'wd5';
    document.getElementById('co-url').value = '';
    document.getElementById('co-error').textContent = '';
    document.getElementById('co-overlay').classList.remove('hidden');
  });
  document.getElementById('co-cancel').addEventListener('click', () => {
    document.getElementById('co-overlay').classList.add('hidden');
  });
  document.getElementById('co-confirm').addEventListener('click', async () => {
    const body = {
      company_name: document.getElementById('co-name').value.trim(),
      ats_type: document.getElementById('co-ats-type').value,
      ats_slug: document.getElementById('co-slug').value.trim(),
      workday_board: document.getElementById('co-board').value.trim() || null,
      workday_wd_ver: document.getElementById('co-wd-ver').value.trim() || 'wd5',
      career_url: document.getElementById('co-url').value.trim() || null,
    };
    if (!body.company_name || !body.ats_slug) {
      document.getElementById('co-error').textContent = 'Name and slug are required.';
      return;
    }
    const result = await apiFetch('/companies', { method: 'POST', body: JSON.stringify(body) });
    if (result) {
      allCompanies.push(result);
      allCompanies.sort((a, b) => a.company_name.localeCompare(b.company_name));
      renderCompanies();
      document.getElementById('co-overlay').classList.add('hidden');
    } else {
      document.getElementById('co-error').textContent = 'Failed to add company. Check for duplicates.';
    }
  });
}

// ── Mailbox Tab ───────────────────────────────────────────────────────────────

let mailboxEvents = [];
let mailboxSortState = { by: 'received_at', dir: 'desc' };

const CATEGORY_LABEL = {
  interview:           'Interview',
  offer:               'Offer',
  assessment:          'Assessment',
  rejection:           'Rejection',
  application_confirm: 'Confirmation',
  linkedin_message:    'LinkedIn DM',
  recruiter:           'Recruiter',
  other:               'Other',
};
const CATEGORY_CLASS = {
  interview:           'badge-green',
  offer:               'badge-purple',
  assessment:          'badge-blue',
  rejection:           'badge-orange',
  application_confirm: 'badge-teal',
  linkedin_message:    'badge-blue',
  recruiter:           'badge-yellow',
  other:               'badge-gray',
};

async function loadMailbox() {
  const data = await apiFetch('/mailbox/summary');
  if (!data) return;

  const cat = data.by_category || {};
  const week = data.this_week || {};

  document.getElementById('mc-total').textContent      = data.total_emails ?? 0;
  document.getElementById('mc-interview').textContent  = cat.interview ?? 0;
  document.getElementById('mc-assessment').textContent = cat.assessment ?? 0;
  document.getElementById('mc-rejection').textContent  = cat.rejection ?? 0;
  document.getElementById('mc-offer').textContent      = cat.offer ?? 0;
  document.getElementById('mc-confirm').textContent    = cat.application_confirm ?? 0;

  if (data.last_sync) {
    const d = new Date(data.last_sync + 'Z');
    document.getElementById('mailbox-last-sync').textContent =
      'Last sync: ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  }

  const weekEl = document.getElementById('mailbox-week-cards');
  const weekOrder = ['interview','offer','assessment','rejection','application_confirm'];
  weekEl.innerHTML = weekOrder.map(k => `
    <div class="mcard-sm">
      <span class="mcard-sm-num">${week[k] ?? 0}</span>
      <span class="mcard-sm-label">${CATEGORY_LABEL[k]}</span>
    </div>
  `).join('');

  document.getElementById('mc-linkedin').textContent = data.linkedin_messages ?? 0;

  mailboxEvents = data.recent_events || [];
  renderMailboxTable();
}

function mailboxSort(col) {
  if (mailboxSortState.by === col) {
    mailboxSortState.dir = mailboxSortState.dir === 'asc' ? 'desc' : 'asc';
  } else {
    mailboxSortState.by = col;
    mailboxSortState.dir = col === 'received_at' ? 'desc' : 'asc';
  }
  // Update header arrows
  document.querySelectorAll('#mailbox-thead .sortable').forEach(th => {
    th.classList.remove('sort-active');
    th.querySelector('.sort-arrow').textContent = ' ⇅';
  });
  const colMap = { company: 1, category: 3, received_at: 4 };
  const idx = colMap[col];
  const ths = document.querySelectorAll('#mailbox-thead th');
  if (ths[idx]) {
    ths[idx].classList.add('sort-active');
    ths[idx].querySelector('.sort-arrow').textContent = mailboxSortState.dir === 'asc' ? ' ▲' : ' ▼';
  }
  renderMailboxTable();
}

function renderMailboxTable() {
  const tbody = document.getElementById('mailbox-events-body');
  if (!mailboxEvents.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="loading">No email events yet — click Sync Now.</td></tr>';
    return;
  }

  const q   = (document.getElementById('mailbox-search')?.value || '').toLowerCase().trim();
  const cat = document.getElementById('mailbox-filter-cat')?.value || '';

  const filtered = mailboxEvents.filter(e => {
    if (cat && e.category !== cat) return false;
    if (q) {
      const hay = ((e.company_name || '') + ' ' + (e.from_name || '') + ' ' + (e.subject || '')).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const { by, dir } = mailboxSortState;
  const sorted = [...filtered].sort((a, b) => {
    let av = by === 'company' ? (a.company_name || a.from_name || '') :
             by === 'category' ? (a.category || '') :
             (a.received_at || '');
    let bv = by === 'company' ? (b.company_name || b.from_name || '') :
             by === 'category' ? (b.category || '') :
             (b.received_at || '');
    av = av.toLowerCase(); bv = bv.toLowerCase();
    return dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
  });

  if (!sorted.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="loading">No emails match your search.</td></tr>';
    return;
  }

  tbody.innerHTML = sorted.map(e => {
    const recv = e.received_at ? formatDate(e.received_at) : '—';
    const appLink = e.linked_application_id
      ? `<a href="#" onclick="switchToTracker(${e.linked_application_id})" style="color:var(--accent)">View</a>`
      : '<span style="color:var(--text-dim)">—</span>';
    return `<tr data-event-id="${e.id}">
      <td style="font-size:18px;text-align:center">${e.icon}</td>
      <td class="mailbox-company-cell" onclick="startEditCompany(${e.id},this)" title="Click to edit">
        <strong>${esc(e.company_name || e.from_name || '?')}</strong>
        <span class="edit-hint">✎</span>
      </td>
      <td style="font-size:12px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.subject)}">${esc(e.subject)}</td>
      <td class="mailbox-cat-cell" onclick="startEditCategory(${e.id},this,'${e.category}')" title="Click to change category">
        <span class="ats-badge ${CATEGORY_CLASS[e.category]||'badge-gray'}">${CATEGORY_LABEL[e.category]||e.category}</span>
        <span class="edit-hint">✎</span>
      </td>
      <td style="font-size:12px;color:var(--text-dim)">${recv}</td>
      <td>${appLink}</td>
    </tr>`;
  }).join('');
}

const _ALL_CATS = ['offer','interview','assessment','rejection','application_confirm','recruiter','linkedin_message','other'];

function startEditCategory(eventId, cell, current) {
  // Already in edit mode — don't re-enter (prevents click-bubble re-trigger)
  if (cell.querySelector('select')) return;

  const opts = _ALL_CATS
    .map(c => `<option value="${c}"${c===current?' selected':''}>${CATEGORY_LABEL[c]||c}</option>`)
    .join('');
  cell.innerHTML = `<select style="background:var(--surface2);border:1px solid var(--accent);color:var(--text);padding:3px 6px;border-radius:4px;font-size:12px;outline:none">${opts}</select>`;
  const sel = cell.querySelector('select');

  // Stop clicks on the select bubbling up to the <td onclick> which would re-run this function
  sel.addEventListener('click', e => e.stopPropagation());

  sel.addEventListener('change', () => commitEditCategory(eventId, sel));
  sel.addEventListener('keydown', e => { if (e.key === 'Escape') renderMailboxTable(); });
  sel.focus();
}

async function commitEditCategory(eventId, sel) {
  const val = sel.value;
  await patchMailboxEvent(eventId, { category: val }, sel.parentElement);
}

function startEditCompany(eventId, cell) {
  if (cell.querySelector('input')) return; // already editing

  const current = cell.querySelector('strong').textContent;
  cell.innerHTML = `<input class="mailbox-company-input" value="${esc(current)}" style="width:120px;background:var(--surface2);border:1px solid var(--accent);color:var(--text);padding:3px 6px;border-radius:4px;font-size:12px" />`
    + `<button class="_confirm-btn" style="margin-left:4px;padding:2px 7px;font-size:11px;background:var(--accent);color:#050f07;border:none;border-radius:4px;cursor:pointer">✓</button>`
    + `<button class="_cancel-btn" style="margin-left:2px;padding:2px 7px;font-size:11px;background:var(--surface2);color:var(--text-dim);border:1px solid var(--border);border-radius:4px;cursor:pointer">✕</button>`;
  const inp = cell.querySelector('input');

  // Stop all child clicks from bubbling to <td onclick>
  cell.querySelectorAll('input,button').forEach(el =>
    el.addEventListener('click', e => e.stopPropagation())
  );
  cell.querySelector('._confirm-btn').addEventListener('mousedown', e => {
    e.preventDefault();
    commitEditCompany(eventId, inp);
  });
  cell.querySelector('._cancel-btn').addEventListener('mousedown', e => {
    e.preventDefault();
    renderMailboxTable();
  });
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter')  commitEditCompany(eventId, inp);
    if (e.key === 'Escape') renderMailboxTable();
  });
  inp.focus();
}

async function commitEditCompany(eventId, inp) {
  const val = inp.value.trim();
  if (!val) return;
  await patchMailboxEvent(eventId, { company_name: val }, inp.parentElement);
}

async function patchMailboxEvent(eventId, patch, feedbackEl) {
  const res = await apiFetch(`/mailbox/events/${eventId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (res) {
    // Update local data so re-render is instant
    const ev = mailboxEvents.find(e => e.id === eventId);
    if (ev) {
      if (patch.category) { ev.category = patch.category; ev.icon = _CATEGORY_ICONS[patch.category] || '📧'; }
      if (patch.company_name) ev.company_name = patch.company_name;
    }
    renderMailboxTable();
  }
}

const _CATEGORY_ICONS = {
  offer:'🎉', interview:'📅', assessment:'📝',
  rejection:'❌', application_confirm:'✅', recruiter:'📨', linkedin_message:'💬', other:'📧',
};

async function loadLinkedInMessages() {
  const data = await apiFetch('/mailbox/linkedin-messages?limit=30');
  if (!data) return;

  const badge = document.getElementById('linkedin-unread-badge');
  if (data.unread_3d > 0) {
    badge.textContent = data.unread_3d + ' new';
    badge.style.display = 'inline';
  } else {
    badge.style.display = 'none';
  }

  const list = document.getElementById('linkedin-messages-list');
  if (!data.messages || data.messages.length === 0) {
    list.innerHTML = '<div style="color:var(--text-dim);font-size:13px">No LinkedIn messages yet — they\'ll appear here once Gmail syncs them.</div>';
    return;
  }

  list.innerHTML = data.messages.map(m => {
    const sender = esc(m.sender_name || m.from_name || 'LinkedIn User');
    const preview = esc(m.preview || m.subject || '');
    const date = m.received_at
      ? formatDate(m.received_at)
      : '';
    return `<div style="background:var(--card-bg);border:1px solid var(--border);border-left:3px solid #0077b5;border-radius:6px;padding:12px 14px;display:flex;align-items:flex-start;gap:12px">
      <div style="width:36px;height:36px;border-radius:50%;background:#0077b5;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:14px;flex-shrink:0">${esc(sender[0] || '?')}</div>
      <div style="flex:1;min-width:0">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
          <strong style="font-size:14px">${sender}</strong>
          <span style="font-size:11px;color:var(--text-dim);white-space:nowrap">${date}</span>
        </div>
        <div style="font-size:13px;color:var(--text-dim);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${preview}">${preview}</div>
      </div>
      <a href="https://www.linkedin.com/messaging/" target="_blank" rel="noopener" style="color:#0077b5;font-size:12px;white-space:nowrap;align-self:center">Open →</a>
    </div>`;
  }).join('');
}

async function triggerEmailSync() {
  document.getElementById('mailbox-last-sync').textContent = 'Syncing…';
  const result = await apiFetch('/mailbox/sync', { method: 'POST' });
  if (result) {
    await loadMailbox();
    if (result.new_events > 0 || result.status_updates > 0) {
      alert(`Sync complete: ${result.new_events} new emails, ${result.status_updates} status updates.`);
    }
  }
}

function switchToTracker(appId) {
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
  document.querySelector('[data-tab="tracker"]').classList.add('active');
  document.getElementById('tab-tracker').classList.add('active');
  loadApplications();
}

// ── Analysis Tab ──────────────────────────────────────────────────────────────

const _anCharts = {};   // registry so we can destroy before redraw

function _destroyChart(id) {
  if (_anCharts[id]) { _anCharts[id].destroy(); delete _anCharts[id]; }
}

const _CHART_DEFAULTS = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend: { display: false } },
  scales: {},
};

const _GRID = {
  color: 'rgba(40,56,32,0.6)',
  borderColor: 'rgba(40,56,32,0.6)',
};

const _TICK = { color: '#7a9a6a', font: { size: 11 } };

async function loadAnalysis() {
  const d = await apiFetch('/stats/analysis');
  if (!d) return;

  _renderKPIs(d.kpi);
  _renderDaily(d);
  _renderCumulative(d);
  _renderFunnel(d.funnel);
  _renderSource(d.by_source);
  _renderCompanies(d.top_companies);
}

function _renderKPIs(k) {
  document.getElementById('an-kpis').innerHTML = [
    { val: k.total_applied,            label: 'Total Applied',      cls: 'accent'  },
    { val: k.avg_per_day + '/day',     label: 'Avg per Day',        cls: ''        },
    { val: k.response_rate + '%',      label: 'Response Rate',      cls: 'blue'    },
    { val: k.interview_rate + '%',     label: 'Interview Rate',     cls: 'yellow'  },
    { val: k.interviews,               label: 'Interviews',         cls: 'blue'    },
    { val: k.offers,                   label: 'Offers',             cls: 'purple'  },
    { val: k.rejections,               label: 'Rejections',         cls: 'red'     },
    { val: k.days_active + ' days',    label: 'Days Active',        cls: ''        },
  ].map(c => `
    <div class="an-kpi ${c.cls}">
      <div class="an-kpi-val">${c.val}</div>
      <div class="an-kpi-label">${c.label}</div>
    </div>`).join('');
}

function _renderDaily(d) {
  _destroyChart('daily');
  const ctx = document.getElementById('chart-daily').getContext('2d');
  _anCharts['daily'] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.dates,
      datasets: [
        {
          label: 'Applications',
          data: d.applied_series,
          borderColor: '#6dba5e',
          backgroundColor: 'rgba(109,186,94,0.12)',
          borderWidth: 2,
          pointRadius: 3,
          pointHoverRadius: 5,
          pointBackgroundColor: '#6dba5e',
          fill: true,
          tension: 0.35,
        },
        {
          label: '7-day avg',
          data: d.rolling_7d,
          borderColor: '#e8b84b',
          backgroundColor: 'transparent',
          borderWidth: 2,
          pointRadius: 0,
          borderDash: [4, 3],
          tension: 0.4,
        },
      ],
    },
    options: {
      ..._CHART_DEFAULTS,
      plugins: {
        legend: { display: false },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: { grid: _GRID, ticks: { ..._TICK, maxTicksLimit: 14 } },
        y: { grid: _GRID, ticks: _TICK, beginAtZero: true },
      },
    },
  });
}

function _renderCumulative(d) {
  _destroyChart('cumulative');
  const ctx = document.getElementById('chart-cumulative').getContext('2d');
  _anCharts['cumulative'] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.dates,
      datasets: [{
        label: 'Total Applications',
        data: d.cumulative,
        borderColor: '#6dba5e',
        backgroundColor: 'rgba(109,186,94,0.08)',
        borderWidth: 2,
        pointRadius: 0,
        fill: true,
        tension: 0.3,
      }],
    },
    options: {
      ..._CHART_DEFAULTS,
      scales: {
        x: { grid: _GRID, ticks: { ..._TICK, maxTicksLimit: 10 } },
        y: { grid: _GRID, ticks: _TICK, beginAtZero: true },
      },
    },
  });
}

function _renderFunnel(funnel) {
  _destroyChart('funnel');
  const ctx = document.getElementById('chart-funnel').getContext('2d');
  const labels = funnel.map(f => f.stage.replace('_', ' ').replace(/\b\w/g, l => l.toUpperCase()));
  const counts  = funnel.map(f => f.count);
  _anCharts['funnel'] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Count',
        data: counts,
        backgroundColor: ['rgba(109,186,94,0.7)','rgba(116,192,252,0.7)','rgba(232,184,75,0.7)','rgba(192,132,252,0.7)'],
        borderRadius: 4,
        borderWidth: 0,
      }],
    },
    options: {
      ..._CHART_DEFAULTS,
      indexAxis: 'y',
      scales: {
        x: { grid: _GRID, ticks: _TICK, beginAtZero: true },
        y: { grid: { display: false }, ticks: { color: '#deebd4', font: { size: 12 } } },
      },
    },
  });
}

function _renderSource(sources) {
  _destroyChart('source');
  const ctx = document.getElementById('chart-source').getContext('2d');
  const colors = ['#6dba5e','#74c0fc','#e8b84b','#c084fc','#e85a6a','#2dd4bf','#f97316','#94a3b8'];
  _anCharts['source'] = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: sources.map(s => s.source),
      datasets: [{
        data: sources.map(s => s.count),
        backgroundColor: colors,
        borderColor: '#121a0e',
        borderWidth: 2,
        hoverOffset: 6,
      }],
    },
    options: {
      ..._CHART_DEFAULTS,
      cutout: '60%',
      plugins: {
        legend: {
          display: true,
          position: 'right',
          labels: { color: '#7a9a6a', font: { size: 11 }, boxWidth: 10, padding: 8 },
        },
      },
    },
  });
}

function _renderCompanies(companies) {
  _destroyChart('companies');
  const ctx = document.getElementById('chart-companies').getContext('2d');
  const labels  = companies.map(c => c.company);
  const applied  = companies.map(c => c.total - c.interview - c.offer - c.rejected);
  const interviews = companies.map(c => c.interview);
  const offers   = companies.map(c => c.offer);
  const rejected = companies.map(c => c.rejected);
  _anCharts['companies'] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Applied',    data: applied,    backgroundColor: 'rgba(109,186,94,0.65)', borderRadius: 3 },
        { label: 'Interview',  data: interviews, backgroundColor: 'rgba(116,192,252,0.75)', borderRadius: 3 },
        { label: 'Offer',      data: offers,     backgroundColor: 'rgba(192,132,252,0.75)', borderRadius: 3 },
        { label: 'Rejected',   data: rejected,   backgroundColor: 'rgba(232,90,106,0.55)', borderRadius: 3 },
      ],
    },
    options: {
      ..._CHART_DEFAULTS,
      indexAxis: 'y',
      plugins: {
        legend: {
          display: true,
          labels: { color: '#7a9a6a', font: { size: 11 }, boxWidth: 10 },
        },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: { stacked: true, grid: _GRID, ticks: _TICK, beginAtZero: true },
        y: { stacked: true, grid: { display: false }, ticks: { color: '#deebd4', font: { size: 11 } } },
      },
    },
  });
}

// ── Interviews ─────────────────────────────────────────────────────────────────

let _interviewApps = [];

async function loadInterviewPrep() {
  const container = document.getElementById('interviews-list');
  if (!container) return;
  container.innerHTML = '<div style="padding:60px;text-align:center;color:#628a6a">Loading…</div>';
  const apps = await apiFetch('/interview-prep');
  _interviewApps = apps || [];
  _renderInterviewCards();
}

function _renderInterviewCards() {
  const container = document.getElementById('interviews-list');
  if (!container) return;
  if (!_interviewApps.length) {
    container.innerHTML = `
      <div style="padding:60px;text-align:center;color:#628a6a">
        <div style="font-size:36px;margin-bottom:12px">🎯</div>
        <div style="font-size:14px;font-weight:600;color:#dff0d4">No active interviews yet</div>
        <div style="font-size:12px;margin-top:6px">Applications in phone screen, interview, or offer stage will appear here.</div>
      </div>`;
    return;
  }
  container.innerHTML = _interviewApps.map(_renderInterviewCard).join('');
}

function _toggleInterviewCard(id) {
  const card = document.getElementById(`icard-${id}`);
  const body = document.getElementById(`ibody-${id}`);
  const chevron = document.getElementById(`ichevron-${id}`);
  if (!card || !body) return;
  const isOpen = !body.classList.contains('hidden');
  body.classList.toggle('hidden', isOpen);
  card.classList.toggle('icard-collapsed', isOpen);
  if (chevron) chevron.style.transform = isOpen ? 'rotate(0deg)' : 'rotate(90deg)';
}

function _renderInterviewCard(a) {
  const statusColors = { phone_screen: '#74c0fc', interview: '#69db7c', offer: '#c084fc' };
  const statusLabels = { phone_screen: 'Phone Screen', interview: 'Interview', offer: 'Offer 🎉' };
  const color = statusColors[a.status] || '#adb5bd';
  const label = statusLabels[a.status] || a.status;

  let prep = null;
  try { prep = a.prep_notes ? JSON.parse(a.prep_notes) : null; } catch (e) {}
  const prepHtml = prep ? _renderPrepContent(prep) : '';

  const descHtml = a.job.description
    ? `<div class="icard-desc">
        <div class="icard-desc-label">Job Description</div>
        <div class="icard-desc-text" id="idesc-${a.id}">
          ${esc(a.job.description.slice(0, 600))}${a.job.description.length > 600
            ? `<span id="idesc-more-${a.id}" style="display:none">${esc(a.job.description.slice(600, 3000))}</span>
               <button class="icard-desc-toggle" onclick="event.stopPropagation();_toggleDesc(${a.id})">Show more</button>`
            : ''}
        </div>
      </div>`
    : `<div class="icard-desc" id="idesc-wrap-${a.id}">
        <button class="btn-secondary btn-sm" onclick="event.stopPropagation();fetchJobDescription(${a.id},${a.job.id})" id="ifetch-${a.id}" style="font-size:11px">⬇ Fetch Job Description</button>
      </div>`;

  const notionHtml = `<div id="inotion-${a.id}">
    ${a.notion_page_id
      ? `<a href="https://notion.so/${a.notion_page_id.replace(/-/g,'')}" target="_blank" class="inotion-link" onclick="event.stopPropagation()">↗ Notion</a>`
      : `<button class="btn-secondary btn-sm inotion-create" onclick="event.stopPropagation();createNotionPage(${a.id})">+ Notion Page</button>`
    }
  </div>`;

  return `
    <div class="interview-card icard-collapsed" id="icard-${a.id}">
      <div class="icard-header" onclick="_toggleInterviewCard(${a.id})" style="cursor:pointer">
        <svg id="ichevron-${a.id}" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0;color:#628a6a;transition:transform 0.2s;transform:rotate(0deg)"><polyline points="6,4 10,8 6,12"/></svg>
        <div class="icard-info">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span class="icard-status" style="background:${color}22;color:${color};border:1px solid ${color}44">${label}</span>
            <span style="font-weight:700;font-size:14px;color:#dff0d4">${esc(a.job.job_title)}</span>
            <span style="color:#628a6a">at</span>
            <span style="font-weight:600;color:#adc9a0">${esc(a.job.company_name)}</span>
            ${a.job.level ? `<span class="badge">${esc(a.job.level)}</span>` : ''}
          </div>
          <div style="display:flex;align-items:center;gap:12px;margin-top:4px">
            ${a.applied_at ? `<span style="font-size:11px;color:#628a6a">Applied ${formatDate(a.applied_at)}</span>` : ''}
            ${notionHtml}
          </div>
        </div>
        <button class="btn-secondary" id="ibtn-${a.id}" onclick="event.stopPropagation();generatePrep(${a.id})" style="white-space:nowrap;flex-shrink:0;margin-left:auto">
          ${prep ? '↺ Regenerate' : '✨ Generate Prep'}
        </button>
      </div>
      <div class="icard-body hidden" id="ibody-${a.id}">
        ${descHtml}
        <div class="icard-prep${prepHtml ? '' : ' hidden'}" id="iprep-${a.id}">${prepHtml}</div>
        ${!prepHtml ? `<div style="padding:16px 0 4px;font-size:12px;color:#628a6a">Click <em>Generate Prep</em> to build AI-powered interview preparation for this role.</div>` : ''}
      </div>
    </div>`;
}

async function fetchJobDescription(appId, jobId) {
  const btn = document.getElementById(`ifetch-${appId}`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Fetching…'; }

  const res = await apiFetch(`/jobs/${jobId}/fetch-description`, { method: 'POST' });

  const wrap = document.getElementById(`idesc-wrap-${appId}`);
  if (res?.description && wrap) {
    const desc = res.description;
    const app = _interviewApps.find(a => a.id === appId);
    if (app) app.job.description = desc;

    const preview = esc(desc.slice(0, 600));
    const more = desc.length > 600
      ? `<span id="idesc-more-${appId}" style="display:none">${esc(desc.slice(600, 3000))}</span>
         <button class="icard-desc-toggle" onclick="event.stopPropagation();_toggleDesc(${appId})">Show more</button>`
      : '';
    wrap.innerHTML = `
      <div class="icard-desc-label">Job Description</div>
      <div class="icard-desc-text" id="idesc-${appId}">${preview}${more}</div>`;
  } else {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '⬇ Fetch Job Description';
    }
    // Show paste fallback
    if (wrap) {
      wrap.innerHTML += `
        <div style="margin-top:8px">
          <div style="font-size:11px;color:#628a6a;margin-bottom:4px">Auto-fetch failed (job may require login) — paste the description here:</div>
          <textarea id="idesc-paste-${appId}" rows="5" style="width:100%;background:#0a1a0d;border:1px solid #1c3020;border-radius:6px;color:#c5ddc0;font-size:12px;padding:8px;resize:vertical" placeholder="Paste job description…" onclick="event.stopPropagation()"></textarea>
          <button class="btn-secondary btn-sm" style="margin-top:6px;font-size:11px" onclick="event.stopPropagation();saveDescriptionPaste(${appId},${jobId})">Save Description</button>
        </div>`;
    }
  }
}

async function saveDescriptionPaste(appId, jobId) {
  const ta = document.getElementById(`idesc-paste-${appId}`);
  const desc = ta?.value.trim();
  if (!desc) return;

  const res = await apiFetch(`/jobs/${jobId}/description`, { method: 'PUT', body: JSON.stringify({ description: desc }) });
  if (res) {
    const app = _interviewApps.find(a => a.id === appId);
    if (app) app.job.description = desc;
    const wrap = document.getElementById(`idesc-wrap-${appId}`);
    if (wrap) {
      const preview = esc(desc.slice(0, 600));
      const more = desc.length > 600
        ? `<span id="idesc-more-${appId}" style="display:none">${esc(desc.slice(600, 3000))}</span>
           <button class="icard-desc-toggle" onclick="event.stopPropagation();_toggleDesc(${appId})">Show more</button>`
        : '';
      wrap.innerHTML = `
        <div class="icard-desc-label">Job Description</div>
        <div class="icard-desc-text" id="idesc-${appId}">${preview}${more}</div>`;
    }
  }
}

function _toggleDesc(id) {
  const more = document.getElementById(`idesc-more-${id}`);
  const btn = document.querySelector(`#idesc-${id} .icard-desc-toggle`);
  if (!more) return;
  const showing = more.style.display !== 'none';
  more.style.display = showing ? 'none' : 'inline';
  if (btn) btn.textContent = showing ? 'Show more' : 'Show less';
}

function _renderPrepContent(prep) {
  const sections = [
    { key: 'likely_questions',    title: 'Likely Interview Questions' },
    { key: 'topics_to_study',     title: 'Topics to Study' },
    { key: 'behavioral_questions',title: 'Behavioral Questions' },
    { key: 'company_research',    title: 'Company Research' },
    { key: 'tips',                title: 'Tips' },
  ];
  return sections
    .filter(s => prep[s.key]?.length)
    .map(s => `
      <div class="prep-section">
        <div class="prep-section-title">${s.title}</div>
        <ul class="prep-list">
          ${prep[s.key].map(item => `<li>${esc(item)}</li>`).join('')}
        </ul>
      </div>`)
    .join('');
}

async function generatePrep(appId) {
  const btn = document.getElementById(`ibtn-${appId}`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Generating…'; }

  const prep = await apiFetch(`/interview-prep/${appId}/generate`, { method: 'POST' });

  if (!prep) {
    if (btn) { btn.disabled = false; btn.textContent = '✨ Generate Prep'; }
    alert('Failed to generate prep. Make sure the job has a description saved.');
    return;
  }

  const app = _interviewApps.find(a => a.id === appId);
  if (app) app.prep_notes = JSON.stringify(prep);

  const prepContainer = document.getElementById(`iprep-${appId}`);
  if (prepContainer) {
    prepContainer.innerHTML = _renderPrepContent(prep);
    prepContainer.classList.remove('hidden');
  }
  if (btn) { btn.disabled = false; btn.textContent = '↺ Regenerate'; }
}

// ── Notion ─────────────────────────────────────────────────────────────────────

function _notionMsg(text, color = '#adc9a0') {
  const el = document.getElementById('notion-msg');
  if (el) { el.textContent = text; el.style.color = color; }
}

async function loadNotionConfig() {
  const cfg = await apiFetch('/notion/config');
  if (!cfg) return;
  if (cfg.api_token_set) document.getElementById('notion-token').placeholder = '••••••••••••••••••••••••••••••• (saved)';
  document.getElementById('notion-parent-page').value = cfg.interviews_parent_page_id || '';
  document.getElementById('notion-pages').value = (cfg.context_page_ids || []).join('\n');
  document.getElementById('notion-enabled').checked = cfg.is_enabled;
}

async function saveNotionConfig() {
  _notionMsg('Saving…');
  const token = document.getElementById('notion-token').value.trim();
  const parentPage = document.getElementById('notion-parent-page').value.trim();
  const pagesRaw = document.getElementById('notion-pages').value;
  const enabled = document.getElementById('notion-enabled').checked;

  const contextIds = pagesRaw.split('\n').map(s => s.trim()).filter(Boolean).map(_extractNotionId);
  const body = {
    interviews_parent_page_id: parentPage || null,
    context_page_ids: contextIds,
    is_enabled: enabled,
  };
  if (token) body.api_token = token;

  const res = await apiFetch('/notion/config', { method: 'PUT', body: JSON.stringify(body) });
  if (res) {
    _notionMsg('Saved!', '#3ddc6b');
    if (token) document.getElementById('notion-token').value = '';
    setTimeout(() => _notionMsg(''), 3000);
  } else {
    _notionMsg('Save failed.', '#e85a5a');
  }
}

async function testNotionConnection() {
  _notionMsg('Testing…');
  const token = document.getElementById('notion-token').value.trim();
  if (token) await saveNotionConfig();
  const res = await apiFetch('/notion/test');
  if (res?.ok) {
    _notionMsg(`✓ Connected as "${res.name}"`, '#3ddc6b');
  } else {
    _notionMsg('✗ Connection failed — check your token and that the page is shared with your integration.', '#e85a5a');
  }
}

async function createNotionPage(appId) {
  const btn = document.querySelector(`#inotion-${appId} button`);
  if (btn) { btn.disabled = true; btn.textContent = 'Creating…'; }

  const res = await apiFetch(`/notion/create-page/${appId}`, { method: 'POST' });

  if (res?.notion_page_id) {
    const app = _interviewApps.find(a => a.id === appId);
    if (app) app.notion_page_id = res.notion_page_id;
    const container = document.getElementById(`inotion-${appId}`);
    if (container) {
      const cleanId = res.notion_page_id.replace(/-/g, '');
      container.innerHTML = `<a href="https://notion.so/${cleanId}" target="_blank" style="font-size:11px;color:#3ddc6b;text-decoration:none">↗ Open in Notion</a>`;
    }
  } else {
    if (btn) { btn.disabled = false; btn.textContent = '+ Create Notion Page'; }
    alert('Could not create Notion page. Make sure your token is saved, the integration is enabled, and the Interview Pages Parent is set in Settings → Notion.');
  }
}

function _extractNotionId(input) {
  if (!input) return '';
  // Extract ID from Notion URLs: .../Page-Title-<32hexchars> or ?v=...
  const m = input.match(/([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})/i)
    || input.match(/([0-9a-f]{32})/i);
  if (m) return m[1];
  return input.trim();
}

// ── API Usage ──────────────────────────────────────────────────────────────────

function _liveBadge(isLive) {
  return isLive
    ? '<span style="font-size:9px;font-weight:700;background:#1c3020;color:#3ddc6b;border:1px solid #2a5030;border-radius:4px;padding:1px 5px;vertical-align:middle;margin-left:5px">LIVE</span>'
    : '<span style="font-size:9px;font-weight:700;background:#1c2228;color:#628a6a;border:1px solid #2a3a44;border-radius:4px;padding:1px 5px;vertical-align:middle;margin-left:5px">EST</span>';
}

async function refreshLiveUsage() {
  const btn = document.getElementById('btn-refresh-live');
  if (btn) { btn.disabled = true; btn.textContent = 'Fetching…'; }
  await apiFetch('/usage/refresh', { method: 'POST' });
  if (btn) { btn.disabled = false; btn.textContent = 'Refresh Live'; }
  await loadUsage();
}

async function loadUsage() {
  const body = document.getElementById('usage-body');
  const cards = document.getElementById('usage-cards');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="7" class="loading" style="padding:40px;text-align:center">Loading…</td></tr>';

  const data = await apiFetch('/usage');
  if (!data) { body.innerHTML = '<tr><td colspan="7" class="loading">Failed to load</td></tr>'; return; }

  // ── Brave card (quota from response headers — auto-updated on each scrape run)
  const braveUsed = data.brave_used_live ?? data.monthly.brave;
  const braveLimit = data.brave_limit;
  const bravePct = data.brave_pct;
  const braveColor = bravePct > 80 ? '#e85a5a' : bravePct > 50 ? '#f5c842' : '#3ddc6b';
  const braveIsLive = !!data.brave_quota_updated_at;
  const braveUpdated = data.brave_quota_updated_at
    ? `<div style="font-size:10px;color:#3a5a3a;margin-top:6px">Updated ${new Date(data.brave_quota_updated_at).toLocaleString()}</div>` : '';

  // ── Webshare card
  const ws = data.webshare_live;
  const wsIsLive = !!ws;
  const wsCalls = wsIsLive ? ws.quota_used : data.monthly.webshare;
  const wsBytes = wsIsLive ? ws.bandwidth_bytes : (data.monthly.webshare * 10240);
  const wsMB = (wsBytes / 1_048_576).toFixed(1);
  const wsUpdated = wsIsLive && ws.updated_at
    ? `<div style="font-size:10px;color:#3a5a3a;margin-top:6px">Updated ${new Date(ws.updated_at).toLocaleString()}</div>` : '';
  const wsNoKey = !data.has_webshare_key
    ? `<div style="font-size:10px;color:#628a6a;margin-top:6px">Add WEBSHARE_API_KEY for live data</div>` : '';

  // ── Claude card
  const cl = data.claude_live;
  const clIsLive = !!cl;
  const clCalls = clIsLive ? cl.quota_used : data.monthly.claude_calls;
  const clTotalTokens = clIsLive
    ? (cl.tokens_in || 0) + (cl.tokens_out || 0)
    : (data.monthly.claude_tokens_in + data.monthly.claude_tokens_out);
  const claudeCost = clIsLive ? cl.cost_usd : data.monthly.claude_cost_usd;
  const clNoKey = !data.has_anthropic_admin_key
    ? `<div style="font-size:10px;color:#628a6a;margin-top:6px">Add ANTHROPIC_ADMIN_KEY for live data</div>` : '';
  const clUpdated = clIsLive && cl.updated_at
    ? `<div style="font-size:10px;color:#3a5a3a;margin-top:6px">Updated ${new Date(cl.updated_at).toLocaleString()}</div>` : '';

  cards.innerHTML = `
    <div style="background:#0e1f12;border:1px solid #1c3020;border-radius:10px;padding:16px 20px;min-width:200px">
      <div style="font-size:11px;color:#628a6a;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Brave API — ${data.month} ${_liveBadge(braveIsLive)}</div>
      <div style="font-size:24px;font-weight:700;color:${braveColor}">${braveUsed.toLocaleString()}</div>
      <div style="font-size:12px;color:#628a6a;margin-top:4px">of ${braveLimit.toLocaleString()} / month</div>
      <div style="margin-top:10px;height:4px;background:#1c3020;border-radius:2px">
        <div style="height:4px;background:${braveColor};border-radius:2px;width:${Math.min(bravePct,100)}%"></div>
      </div>
      <div style="font-size:11px;color:#628a6a;margin-top:4px">${data.brave_remaining.toLocaleString()} remaining</div>
      ${braveUpdated}
    </div>
    <div style="background:#0e1f12;border:1px solid #1c3020;border-radius:10px;padding:16px 20px;min-width:200px">
      <div style="font-size:11px;color:#628a6a;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Webshare Proxy — ${data.month} ${_liveBadge(wsIsLive)}</div>
      <div style="font-size:24px;font-weight:700;color:#74c0fc">${wsCalls.toLocaleString()}</div>
      <div style="font-size:12px;color:#628a6a;margin-top:4px">requests this month</div>
      <div style="font-size:20px;font-weight:600;color:#74c0fc;margin-top:10px">${wsMB} MB</div>
      <div style="font-size:12px;color:#628a6a;margin-top:2px">${wsIsLive ? 'actual' : 'estimated'} data used</div>
      ${wsUpdated}${wsNoKey}
    </div>
    <div style="background:#0e1f12;border:1px solid #1c3020;border-radius:10px;padding:16px 20px;min-width:200px">
      <div style="font-size:11px;color:#628a6a;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Claude API — ${data.month} ${_liveBadge(clIsLive)}</div>
      <div style="font-size:24px;font-weight:700;color:#c084fc">${clCalls.toLocaleString()}</div>
      <div style="font-size:12px;color:#628a6a;margin-top:4px">messages this month</div>
      <div style="font-size:20px;font-weight:600;color:#c084fc;margin-top:10px">${clTotalTokens.toLocaleString()}</div>
      <div style="font-size:12px;color:#628a6a;margin-top:2px">tokens (in + out)</div>
      <div style="font-size:16px;font-weight:600;color:${claudeCost > 1 ? '#f5c842' : '#c084fc'};margin-top:8px">$${claudeCost.toFixed(4)}</div>
      <div style="font-size:12px;color:#628a6a;margin-top:2px">${clIsLive ? 'actual' : 'estimated'} cost (Haiku)</div>
      ${clUpdated}${clNoKey}
    </div>`;

  // Daily table
  if (!data.daily.length) {
    body.innerHTML = '<tr><td colspan="7" class="loading" style="padding:20px;text-align:center">No usage recorded yet — data appears after the next scrape run.</td></tr>';
    return;
  }

  body.innerHTML = data.daily.map(r => {
    const claudeTokens = (r.claude_tokens_in || 0) + (r.claude_tokens_out || 0);
    const claudeCalls = r.claude_calls || 0;
    const claudeCost = r.claude_cost_usd || 0;
    return `
    <tr style="border-bottom:1px solid #1c3020">
      <td style="padding:9px 12px;font-size:13px;color:#dff0d4">${r.date}</td>
      <td style="padding:9px 12px;text-align:right;font-size:13px;color:${r.brave > 0 ? '#3ddc6b' : '#628a6a'}">${r.brave > 0 ? r.brave.toLocaleString() : '—'}</td>
      <td style="padding:9px 12px;text-align:right;font-size:13px;color:${r.webshare > 0 ? '#74c0fc' : '#628a6a'}">${r.webshare > 0 ? r.webshare.toLocaleString() : '—'}</td>
      <td style="padding:9px 12px;text-align:right;font-size:13px;color:#628a6a">${r.webshare_mb > 0 ? r.webshare_mb.toFixed(2) + ' MB' : '—'}</td>
      <td style="padding:9px 12px;text-align:right;font-size:13px;color:${claudeCalls > 0 ? '#c084fc' : '#628a6a'}">${claudeCalls > 0 ? claudeCalls.toLocaleString() : '—'}</td>
      <td style="padding:9px 12px;text-align:right;font-size:13px;color:${claudeTokens > 0 ? '#c084fc' : '#628a6a'}">${claudeTokens > 0 ? claudeTokens.toLocaleString() : '—'}</td>
      <td style="padding:9px 12px;text-align:right;font-size:13px;color:#628a6a">${claudeCost > 0 ? '$' + claudeCost.toFixed(4) : '—'}</td>
    </tr>`;
  }).join('');
}
