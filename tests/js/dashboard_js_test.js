'use strict';
// AUD12-06: dashboard JS has never had tests. Harness = node --test + a
// minimal DOM stub (OWNER-DECISIONS: no Playwright). The script under test
// (dashboard.js) is extracted from awf/templates/dashboard.html.j2 by
// tests/agent_workflow_ui/test_dashboard_js.py — the template is the source
// of truth, there is no JS copy to drift.

const test = require('node:test');
const assert = require('node:assert');

// ─── Minimal DOM stub ─────────────────────────────────────────────────
function _escape(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

class El {
  constructor(id) {
    this.id = id || '';
    this._html = '';
    this._text = null;
    this.className = '';
    this.style = {};
    this.dataset = {};
    this.title = '';
    this.scrollTop = 0;
  }
  set innerHTML(v) { this._html = String(v); this._text = null; }
  // textContent assignment must read back as escaped markup — that is how
  // the dashboard's escapeHtml() works (and what the XSS tests rely on).
  get innerHTML() { return this._text === null ? this._html : _escape(this._text); }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text === null ? this._html : this._text; }
}

const registry = new Map();
global.document = {
  getElementById(id) {
    if (!registry.has(id)) registry.set(id, new El(id));
    return registry.get(id);
  },
  createElement(tag) { return new El(tag); },
  querySelectorAll() { return []; },
  addEventListener() {},
  removeEventListener() {},
};
global.window = global;
global.setInterval = () => 0;
global.clearInterval = () => {};
global.setTimeout = () => 0;
global.fetch = async () => ({ ok: true, json: async () => ({}) });

// The script self-executes on load (updateAll(INITIAL_STATE), poll(),
// setInterval) — the stubs above absorb all of that.
const dash = require('./dashboard.js');

function state(overrides) {
  return Object.assign({
    project_name: 'P',
    todo_id: 'TODO-0001',
    status: 'running',
    status_text: 'Pipeline running',
    stages: [],
    stages_done: 0,
    stages_total: 0,
    next_stage: null,
    worker: null,
    handoffs: [],
    todo_timeline: [],
    todo_content_html: '',
    todo_summary: '',
    todo_diff_stat: '',
    events: [],
    elapsed_epoch: 0,
    elapsed_frozen: false,
    elapsed_str: '',
    total_elapsed_sec: 0,
    total_running: false,
    total_run_started_epoch: 0,
    salvage_stage: null,
    run: null,
  }, overrides);
}

function fresh() { registry.clear(); }

// ─── AUD10-10: XSS — labels from pipeline.yaml must not execute ───────
test('AUD10-10: stage label payload is escaped in stage-list', () => {
  fresh();
  dash.updateAll(state({
    stages: [{
      name: 'evil', role: 'evil', status: 'current', icon: 'X', color: '#ffffff',
      label: '"><img src=x onerror=alert(1)>',
    }],
    stages_total: 1,
  }));
  const html = registry.get('stage-list').innerHTML;
  assert.ok(!html.includes('<img'), 'raw <img> must not survive into innerHTML');
  assert.ok(html.includes('&lt;img'), 'escaped payload expected');
});

test('AUD10-10: chat + next-stage label payloads are escaped', () => {
  fresh();
  dash.updateAll(state({
    next_stage: { icon: 'I', label: '<img src=x onerror=alert(1)>', color: '#ffffff' },
    handoffs: [{
      role: 'r', icon: 'I', color: '#ffffff', label: '<img src=x onerror=alert(1)>',
      content_html: '<p>ok</p>', duration: '1s', rev: 'r|1',
      started_at: '10:00:00', ended_at: '10:01:00', started_epoch: 0,
      active: false, awaiting: false, is_verify: false, line: '',
    }],
  }));
  const chat = registry.get('pane-chat').innerHTML;
  const next = registry.get('next-preview').innerHTML;
  assert.ok(!chat.includes('<img'), 'raw payload must not survive in chat');
  assert.ok(chat.includes('&lt;img'), 'escaped payload expected in chat');
  assert.ok(!next.includes('<img'), 'raw payload must not survive in next-stage');
});

// ─── AUD10-03: the active entry's worker line must reach the chat ─────
test('AUD10-03: active line updates when the re-render key carries it', () => {
  fresh();
  const mk = (line) => state({
    handoffs: [{
      role: 'dev', icon: 'D', color: '#ffffff', label: 'Dev',
      content_html: '', duration: '', rev: `active|dev|1000|${line}`,
      started_at: '10:00:00', ended_at: '', started_epoch: 1000,
      active: true, awaiting: false, is_verify: false, line: line,
    }],
  });
  dash.updateAll(mk('touching foo.py'));
  assert.ok(registry.get('pane-chat').innerHTML.includes('touching foo.py'));

  dash.updateAll(mk('touching bar.py'));
  const html = registry.get('pane-chat').innerHTML;
  assert.ok(html.includes('touching bar.py'), 'new line must appear in the next poll');
  assert.ok(!html.includes('touching foo.py'), 'old line must be replaced, not frozen');
});

// ─── AUD10-08: checkpoint must be visible (dot + badge classes) ───────
test('AUD10-08: checkpoint status sets dot+badge classes', () => {
  fresh();
  dash.updateAll(state({ status: 'checkpoint', status_text: 'Checkpoint pending' }));
  assert.strictEqual(registry.get('status-dot').className, 'status-dot checkpoint');
  assert.strictEqual(registry.get('status-badge').className, 'status-badge checkpoint');
});

// ─── Basic invariants: order, rendering, update ───────────────────────
test('chat renders handoffs in the order the server sent them', () => {
  fresh();
  const h = (role, rev) => ({
    role, icon: 'I', color: '#ffffff', label: role,
    content_html: `<p>${role} body</p>`,
    duration: '1s', rev: role + '|' + rev,
    started_at: '10:00:00', ended_at: '10:01:00', started_epoch: 0,
    active: false, awaiting: false, is_verify: false, line: '',
  });
  dash.updateAll(state({ handoffs: [h('newer', '2'), h('older', '1')] }));
  const html = registry.get('pane-chat').innerHTML;
  assert.ok(html.indexOf('newer body') < html.indexOf('older body'),
    'newest entry must render first');
});

test('unchanged rev does not re-render the chat', () => {
  fresh();
  const h = {
    role: 'dev', icon: 'D', color: '#ffffff', label: 'Dev',
    content_html: '<p>body</p>', duration: '', rev: 'dev|7',
    started_at: '10:00:00', ended_at: '10:01:00', started_epoch: 0,
    active: false, awaiting: false, is_verify: false, line: '',
  };
  dash.updateAll(state({ handoffs: [h] }));
  const first = registry.get('pane-chat').innerHTML;
  dash.updateAll(state({ handoffs: [h] }));
  assert.strictEqual(registry.get('pane-chat').innerHTML, first,
    'identical keys must not churn the pane (Day-4 stability)');
});

test('empty handoffs show the empty state', () => {
  fresh();
  dash.updateAll(state({ handoffs: [] }));
  assert.ok(registry.get('pane-chat').innerHTML.includes('Agent handoffs'));
});
