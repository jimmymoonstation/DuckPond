const DEFAULT_SERVER = 'http://143.198.134.85';
let currentUrl = '';
let currentTabId = null;
let lastAnalysis = null;

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  const { serverUrl } = await chrome.storage.local.get('serverUrl');
  document.getElementById('server-url').value = serverUrl || DEFAULT_SERVER;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentUrl = tab?.url || '';
  currentTabId = tab?.id;
  document.getElementById('current-url').textContent = currentUrl || 'No URL';

  document.getElementById('btn-analyze').addEventListener('click', analyze);
  document.getElementById('btn-quick-add').addEventListener('click', openQuickAdd);
  document.getElementById('qa-confirm').addEventListener('click', confirmQuickAdd);
  document.getElementById('qa-cancel').addEventListener('click', closeQuickAdd);
  document.getElementById('btn-settings').addEventListener('click', toggleSettings);
  document.getElementById('btn-save-settings').addEventListener('click', saveSettings);
  document.getElementById('btn-save-board').addEventListener('click', saveToBoard);
  document.getElementById('btn-apply-now').addEventListener('click', () => chrome.tabs.create({ url: currentUrl }));
});

// ── Extract page content from the live browser tab ───────────────────────────

async function extractPageContent() {
  // Runs inside the page — has access to the full rendered DOM including login-gated content
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: currentTabId },
    func: () => {
      // Grab meta tags for quick title/company hints
      const metaTitle = document.querySelector('meta[property="og:title"]')?.content
        || document.querySelector('meta[name="title"]')?.content
        || document.title || '';

      const metaDesc = document.querySelector('meta[property="og:description"]')?.content
        || document.querySelector('meta[name="description"]')?.content || '';

      // Remove noise elements
      const clone = document.body.cloneNode(true);
      for (const el of clone.querySelectorAll('script,style,nav,footer,header,aside,[role="banner"],[role="navigation"]')) {
        el.remove();
      }

      // Try to find the main job content block first
      const selectors = [
        '#job-description', '.job-description', '[data-testid="job-description"]',
        '.description__text', '.jobs-description',  // LinkedIn
        '.posting-description',                      // Lever
        '[class*="jobDescription"]', '[class*="job-details"]',
        'main', 'article',
      ];
      let mainText = '';
      for (const sel of selectors) {
        const el = clone.querySelector(sel);
        if (el) {
          const t = el.innerText || el.textContent || '';
          if (t.trim().length > 200) { mainText = t.trim(); break; }
        }
      }

      const fullText = mainText || clone.innerText || clone.textContent || '';

      // Try to find the company's original ATS URL — links on the page that point to known ATS platforms
      const atsPatterns = [
        'greenhouse.io', 'lever.co', 'ashbyhq.com', 'myworkdayjobs.com',
        'careers.google.com', 'metacareers.com', 'jobs.apple.com', 'amazon.jobs',
        'taleo.net', 'jobvite.com', 'icims.com', 'smartrecruiters.com', 'bamboohr.com',
      ];
      const allLinks = [...document.querySelectorAll('a[href]')]
        .map(a => a.href)
        .filter(h => h && h.startsWith('http'));
      const originalUrl = allLinks.find(h => atsPatterns.some(p => h.includes(p))) || null;

      return {
        text: fullText.slice(0, 8000),
        meta_title: metaTitle,
        meta_desc: metaDesc,
        original_url: originalUrl,
      };
    },
  });
  return result;
}

// ── Analyze ───────────────────────────────────────────────────────────────────

async function analyze() {
  if (!currentUrl || currentUrl.startsWith('chrome://')) {
    showError('Navigate to a job posting page first.');
    return;
  }

  setLoading(true, 'Reading page…');
  hideResults();
  hideError();
  closeQuickAdd();

  let pageData;
  try {
    pageData = await extractPageContent();
  } catch (e) {
    setLoading(false);
    showError('Could not read page. Try reloading the job page and opening the extension again.');
    return;
  }

  if (!pageData?.text || pageData.text.trim().length < 100) {
    setLoading(false);
    showError('Page content looks empty. Make sure the job posting is fully loaded.');
    return;
  }

  setLoading(true, 'Analyzing with Claude…');
  const server = await getServer();

  try {
    const resp = await fetch(`${server}/api/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: currentUrl, page_content: pageData.text, original_url: pageData.original_url || null }),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `Server error ${resp.status}`);
    }

    lastAnalysis = await resp.json();
    renderResults(lastAnalysis);
  } catch (e) {
    showError(e.message.includes('Failed to fetch')
      ? `Can't reach server at ${server}. Check your server URL in settings.`
      : e.message);
  } finally {
    setLoading(false);
  }
}

// ── Quick Add ─────────────────────────────────────────────────────────────────

async function openQuickAdd() {
  hideResults();
  hideError();

  // Pre-fill from page meta tags
  let metaTitle = '';
  let metaCompany = '';
  try {
    const pageData = await extractPageContent();
    metaTitle = pageData?.meta_title || '';
    // Try to split "Job Title at Company" or "Job Title - Company"
    const match = metaTitle.match(/^(.+?)\s+(?:at|@|-–—)\s+(.+)$/i);
    if (match) {
      metaTitle = match[1].trim();
      metaCompany = match[2].trim();
    }
  } catch (_) {}

  document.getElementById('qa-title').value = metaTitle;
  document.getElementById('qa-company').value = metaCompany;
  document.getElementById('qa-location').value = '';
  document.getElementById('qa-level').value = '';
  document.getElementById('qa-msg').textContent = '';
  document.getElementById('quick-add-form').classList.remove('hidden');
  document.getElementById('qa-title').focus();
}

function closeQuickAdd() {
  document.getElementById('quick-add-form').classList.add('hidden');
}

async function confirmQuickAdd() {
  const title = document.getElementById('qa-title').value.trim();
  const company = document.getElementById('qa-company').value.trim();
  const msg = document.getElementById('qa-msg');

  if (!title || !company) {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Title and company are required.';
    return;
  }

  const server = await getServer();
  const btn = document.getElementById('qa-confirm');
  btn.disabled = true;

  try {
    const resp = await fetch(`${server}/api/jobs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_title: title,
        company_name: company,
        url: currentUrl,
        location: document.getElementById('qa-location').value.trim() || null,
        level: document.getElementById('qa-level').value.trim() || null,
      }),
    });

    msg.style.color = 'var(--green)';
    if (resp.status === 409) {
      msg.textContent = 'Already on your board.';
    } else if (resp.ok) {
      msg.textContent = '✓ Saved to board!';
      btn.textContent = 'Saved ✓';
    } else {
      msg.style.color = 'var(--red)';
      msg.textContent = 'Save failed. Try again.';
      btn.disabled = false;
    }
  } catch {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Could not reach server.';
    btn.disabled = false;
  }
}

// ── Render Analysis Results ───────────────────────────────────────────────────

function renderResults(data) {
  document.getElementById('res-title').textContent = data.job_title || 'Unknown Role';
  document.getElementById('res-company').textContent = data.company_name || '';
  document.getElementById('res-summary').textContent = data.summary || '';

  const score = data.fit_score ?? 0;
  const badge = document.getElementById('fit-score');
  badge.textContent = `${score}%`;
  badge.className = 'fit-badge ' + (score >= 70 ? 'fit-high' : score >= 45 ? 'fit-mid' : 'fit-low');

  document.getElementById('res-strengths').innerHTML =
    (data.strengths || []).map(s => `<li>${esc(s)}</li>`).join('');

  const gaps = data.gaps || [];
  document.getElementById('res-gaps').innerHTML = gaps.map(g => `<li>${esc(g)}</li>`).join('');
  document.getElementById('gaps-section').style.display = gaps.length ? '' : 'none';

  document.getElementById('res-bullets').innerHTML = (data.cover_letter_bullets || []).map(b => `
    <li onclick="copyText(this, ${JSON.stringify(b)})">
      ${esc(b)}<span class="copy-hint">click to copy</span>
    </li>
  `).join('');

  document.getElementById('btn-save-board').disabled = false;
  document.getElementById('btn-save-board').textContent = 'Save to Board';
  document.getElementById('save-msg').textContent = '';
  document.getElementById('results').classList.remove('hidden');
}

function copyText(el, text) {
  navigator.clipboard.writeText(text).then(() => {
    const hint = el.querySelector('.copy-hint');
    hint.textContent = '✓ copied!';
    setTimeout(() => { hint.textContent = 'click to copy'; }, 1500);
  });
}

// ── Save to Board (from analysis) ─────────────────────────────────────────────

async function saveToBoard() {
  if (!lastAnalysis) return;
  const server = await getServer();
  const btn = document.getElementById('btn-save-board');
  const msg = document.getElementById('save-msg');
  btn.disabled = true;

  try {
    const resp = await fetch(`${server}/api/jobs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_title: lastAnalysis.job_title,
        company_name: lastAnalysis.company_name,
        url: currentUrl,
        original_url: lastAnalysis.original_url || null,
        location: lastAnalysis.location || null,
        level: lastAnalysis.level || null,
        description: lastAnalysis.description || null,
      }),
    });

    msg.style.color = 'var(--green)';
    if (resp.status === 409) {
      msg.textContent = 'Already on your board';
    } else if (resp.ok) {
      msg.textContent = '✓ Saved!';
      btn.textContent = 'Saved ✓';
    } else {
      msg.style.color = 'var(--red)';
      msg.textContent = 'Save failed';
      btn.disabled = false;
    }
  } catch {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Could not reach server';
    btn.disabled = false;
  }
}

// ── Settings ──────────────────────────────────────────────────────────────────

function toggleSettings() {
  document.getElementById('settings-panel').classList.toggle('hidden');
}

async function saveSettings() {
  const url = document.getElementById('server-url').value.trim().replace(/\/$/, '');
  await chrome.storage.local.set({ serverUrl: url });
  document.getElementById('settings-panel').classList.add('hidden');
}

async function getServer() {
  const { serverUrl } = await chrome.storage.local.get('serverUrl');
  return (serverUrl || DEFAULT_SERVER).replace(/\/$/, '');
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function setLoading(on, msg = '') {
  document.getElementById('loading').classList.toggle('hidden', !on);
  document.getElementById('loading-msg').textContent = msg;
  document.getElementById('btn-analyze').disabled = on;
  document.getElementById('btn-quick-add').disabled = on;
}

function hideResults() { document.getElementById('results').classList.add('hidden'); }
function hideError()   { document.getElementById('error-box').classList.add('hidden'); }

function showError(msg) {
  document.getElementById('error-msg').textContent = msg;
  document.getElementById('error-box').classList.remove('hidden');
}

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
