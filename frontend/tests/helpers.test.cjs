const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ui = require('../ui_helpers.js');

test('escape text and attribute delimiters', () => {
  assert.equal(ui.escapeHTML('<img src="x" onerror=\'alert(1)\'>&'), '&lt;img src=&quot;x&quot; onerror=&#39;alert(1)&#39;&gt;&amp;');
  assert.equal(ui.imageURL('javascript:alert(1)', 'http://localhost:1234'), '');
  assert.equal(ui.imageURL('data:image/svg+xml,evil', 'http://localhost:1234'), '');
  assert.equal(ui.imageURL('http://external.test/evil', 'http://localhost:1234'), '');
  assert.equal(ui.imageURL('https://example.test/icon.png', 'http://localhost:1234'), 'https://example.test/icon.png');
});

test('incremental hourly values replace a bucket and old values expire', () => {
  const previous = [{key:'old',time:'2026-10-01T00:00:00Z',value:1},{key:'hour',time:'2026-10-15T10:00:00Z',value:10}];
  const incoming = [{key:'hour',time:'2026-10-15T10:00:00Z',value:20}];
  assert.deepEqual(ui.mergeTrend(previous,incoming,'2026-10-08T10:30:00Z',3600000),incoming);
  assert.equal(ui.mergeTrend(previous,[],'2026-10-15T11:01:00Z',3600000).length,0);
});

test('display grouping distinguishes targets and identical item instances', () => {
  const a={target_id:1,item_id:'a'},b={target_id:2,item_id:'a'},c={target_id:1,item_id:'b'};
  assert.equal(new Set([a,b,c].map(ui.eventKey)).size,3);
});

test('homepage has no automatic historical writeback, including reload path', () => {
  const page = fs.readFileSync(path.join(__dirname,'../index.html'),'utf8');
  assert.doesNotMatch(page,/mark_redundant|fullClean|incrementalClean|findRedundantIds/);
  assert.match(page,/auth\/logout/);
});

test('blurring a free text item name keeps the search and monitor condition', () => {
  const page=fs.readFileSync(path.join(__dirname,'../global_search.html'),'utf8');
  const source=page.match(/function clrAC\(\)\{[^\n]*\}/)[0];
  const input={value:'法师之血 重革腰带',dataset:{}};
  const dropdown={classList:{remove(){}}};
  require('node:vm').runInNewContext(source+';clrAC()',{
    document:{getElementById:id=>id==='si0'?input:dropdown},setTimeout:fn=>fn()
  });
  assert.equal(input.value,'法师之血 重革腰带');
});

test('frontend libraries match the recorded pinned package hashes', () => {
  const versions=JSON.parse(fs.readFileSync(path.join(__dirname,'../vendor/VERSIONS.json'),'utf8'));
  const crypto=require('node:crypto');
  for(const entry of Object.values(versions)) {
    assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,'../vendor',entry.file))).digest('hex'),entry.sha256);
  }
});
