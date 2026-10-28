'use strict';
// PA2 (closer IMP7): the pipeline-compose form used to drop the stage keys
// `skip_if`, `write_check`, `write_allow` and `tools` on re-save. A grep of
// the rendered HTML cannot prove the round-trip — this driver executes the
// template's own collectStages()/buildYamlPreview() (extracted by
// tests/agent_workflow_ui/test_pipeline_compose_js.py) against a DOM stub.
// There is no JS copy of the form logic to drift.

const test = require('node:test');
const assert = require('node:assert');

// ─── Minimal DOM stub ─────────────────────────────────────────────────
class El {
  constructor(id) {
    this.id = id || '';
    this.value = '';
    this.textContent = '';
    this.innerHTML = '';
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.className = '';
    this.classList = { contains: () => false, add: () => {}, remove: () => {} };
  }
  addEventListener() {}
  appendChild(child) { return child; }
  insertBefore() {}
  remove() {}
  setAttribute() {}
  querySelector() { return new El(); }
  querySelectorAll() { return []; }
}

const byId = new Map();
let rows = [];

global.document = {
  getElementById(id) {
    if (!byId.has(id)) byId.set(id, new El(id));
    return byId.get(id);
  },
  createElement(tag) { return new El(tag); },
  querySelectorAll() { return rows; },
  addEventListener() {},
  removeEventListener() {},
};
global.window = global;

// The bundle self-executes on load (prefill loop, updateEmpty, validate) —
// the stubs above absorb it. `initialStages` is [] in the wrapper's render.
const compose = require('./pipeline_compose.js');

// ─── Helpers ──────────────────────────────────────────────────────────
function input(value) { const e = new El(); e.value = value; return e; }

function makeRow(values) {
  return {
    querySelector(sel) {
      if (Object.prototype.hasOwnProperty.call(values, sel)) {
        return input(values[sel]);
      }
      if (sel.indexOf('.st-') === 0) return null;
      return new El();
    },
    querySelectorAll() { return []; },
    classList: { contains: () => false },
  };
}

function setRows(list) { rows = list.map(makeRow); }

// ─── Tests ────────────────────────────────────────────────────────────
test('collectStages keeps the closer gate and its write fields', () => {
  setRows([{
    '.st-role': 'agent-implementer',
    '.st-id': 'implementer-close',
    '.st-task': 'close the probes',
    '.st-skip_if': 'checklist_green',
    '.st-write_check': 'off',
    '.st-write_allow': 'reports/a.md, reports/b.md',
    '.st-tools': '{"deny": ["write"]}',
    '.st-timeout': '600',
  }]);
  assert.deepStrictEqual(compose.collectStages(), [{
    role: 'agent-implementer',
    id: 'implementer-close',
    task: 'close the probes',
    skip_if: 'checklist_green',
    write_check: 'off',
    write_allow: ['reports/a.md', 'reports/b.md'],
    tools: { deny: ['write'] },
    timeout: 600,
  }]);
});

test('a plain stage gains none of the gate keys', () => {
  setRows([{ '.st-role': 'agent-qa-review' }]);
  assert.deepStrictEqual(compose.collectStages(), [{ role: 'agent-qa-review' }]);
});

test('an empty tools object stays omitted (preview matches the saved file)', () => {
  setRows([{ '.st-role': 'agent-implementer', '.st-tools': '{}' }]);
  const stages = compose.collectStages();
  assert.deepStrictEqual(stages, [{ role: 'agent-implementer' }]);
  const preview = compose.buildYamlPreview('default', stages);
  assert.ok(!preview.includes('tools:'), preview);
});

test('buildYamlPreview emits the same keys collectStages collects', () => {
  setRows([{
    '.st-role': 'agent-implementer',
    '.st-id': 'implementer-close',
    '.st-skip_if': 'checklist_green',
    '.st-write_check': 'off',
    '.st-write_allow': 'a.md',
    '.st-timeout': '900',
  }]);
  const preview = compose.buildYamlPreview('default', compose.collectStages());
  for (const key of ['write_allow:', 'write_check:', 'skip_if:', 'timeout:']) {
    assert.ok(preview.includes(key), `${key} missing from preview:\n${preview}`);
  }
  assert.ok(preview.includes('"off"'), 'write_check must stay a quoted string');
});

test('a stage timeout of 0 is omitted (the backend refuses it)', () => {
  setRows([{ '.st-role': 'agent-implementer', '.st-timeout': '0' }]);
  assert.deepStrictEqual(compose.collectStages(), [{ role: 'agent-implementer' }]);
});

test('invalid tools JSON disables submit instead of silently dropping it', () => {
  setRows([{ '.st-role': 'agent-implementer', '.st-tools': '{broken' }]);
  compose.validate();
  assert.strictEqual(byId.get('tools-error').hidden, false);
  assert.strictEqual(byId.get('submit-btn').disabled, true);
});
