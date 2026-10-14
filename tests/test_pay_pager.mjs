import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, '..', 'courtview.html'), 'utf8');
const BEGIN = 'PAY-PAGER:BEGIN';
const END = 'PAY-PAGER:END';
const b = html.indexOf(BEGIN);
const e = html.indexOf(END);
if (b < 0 || e < 0 || e < b) throw new Error('PAY-PAGER markers missing from courtview.html');
const bodyStart = html.indexOf('\n', b) + 1;
const bodyEnd = html.lastIndexOf('\n', e);
const src = html.slice(bodyStart, bodyEnd);
const api = new Function(`${src}
return { londonYMD, londonDayStartMs, payPagerStart, payPagerWindow, payPagerAdvance, payPagerRun, fmtPayLocal, fmtBackTo };`)();
const { payPagerStart, payPagerWindow, payPagerAdvance, payPagerRun, fmtPayLocal, fmtBackTo, londonDayStartMs } = api;

const NOW = Date.parse('2026-10-14T00:44:00Z'); // 01:44 BST on 14 Oct
const iso = ms => new Date(ms).toISOString();

test('start in current month uses London today', () => {
  const c = payPagerStart(2026, 9, NOW);
  assert.deepEqual(c, { y: 2026, m: 9, d: 14, skip: 0, done: false });
  const w = payPagerWindow(c, NOW);
  assert.equal(iso(w.start), '2026-10-13T23:00:00.000Z');
  assert.equal(w.end, NOW);
});

test('past month starts on last day, window end is day end', () => {
  const c = payPagerStart(2026, 8, NOW);
  assert.equal(c.d, 30);
  const w = payPagerWindow(c, NOW);
  assert.equal(iso(w.end), '2026-09-30T22:59:59.999Z');
});

test('DST fall-back day is 25 hours', () => {
  const w = payPagerWindow({ y: 2026, m: 9, d: 25, skip: 0, done: false }, Date.parse('2027-01-01T00:00:00Z'));
  assert.equal(iso(w.start), '2026-10-24T23:00:00.000Z');
  assert.equal(w.end, londonDayStartMs(2026, 9, 26) - 1);
  assert.equal(iso(w.end), '2026-10-25T23:59:59.999Z');
});

test('advance', () => {
  const c = { y: 2026, m: 9, d: 14, skip: 0, done: false };
  const frozen = JSON.stringify(c);
  assert.deepEqual(payPagerAdvance(c, { has_more: false }), { y: 2026, m: 9, d: 13, skip: 0, done: false });
  assert.deepEqual(payPagerAdvance(c, { has_more: true }), { y: 2026, m: 9, d: 14, skip: 100, done: false });
  assert.equal(JSON.stringify(c), frozen);
  const d1 = { y: 2026, m: 9, d: 1, skip: 0, done: false };
  assert.equal(payPagerAdvance(d1, { has_more: false }).done, true);
  const r = payPagerAdvance(d1, { has_more: true });
  assert.equal(r.skip, 100);
  assert.equal(r.done, false);
});

const rows = (n, tag) => Array.from({ length: n }, (_, i) => ({ id: `${tag}${i}` }));

test('run: stops once enough rows, pages within a day', async () => {
  const calls = [];
  const fetchPage = async (win, skip) => {
    calls.push(skip);
    if (calls.length === 1) return { payments: rows(1, 'a'), has_more: false };
    if (calls.length === 2) return { payments: rows(100, 'b'), has_more: true };
    return { payments: rows(50, 'c'), has_more: false };
  };
  const start = payPagerStart(2026, 9, NOW);
  const res = await payPagerRun(start, fetchPage, NOW, () => false);
  assert.deepEqual(calls, [0, 0]);
  assert.equal(res.rows.length, 101);
  assert.equal(res.cursor.d, 14 - 1);
  assert.equal(res.cursor.skip, 100);
  assert.equal(res.error, false);
});

test('run: empty days stop after 7 requests', async () => {
  let n = 0;
  const res = await payPagerRun(payPagerStart(2026, 9, NOW), async () => { n++; return { payments: [], has_more: false }; }, NOW, () => false);
  assert.equal(n, 7);
  assert.equal(res.cursor.d, 7);
  assert.equal(res.requests, 7);
});

test('run: null page errors without advancing', async () => {
  const start = payPagerStart(2026, 9, NOW);
  const res = await payPagerRun(start, async () => null, NOW, () => false);
  assert.equal(res.error, true);
  assert.deepEqual(res.cursor, start);
});

test('run: stale aborts', async () => {
  let calls = 0;
  const res = await payPagerRun(payPagerStart(2026, 9, NOW), async () => { calls++; return { payments: [], has_more: false }; }, NOW, () => calls >= 1);
  assert.equal(res.aborted, true);
  assert.equal(calls, 1);
});

test('run: past month from day 3 ends done after day 1', async () => {
  const res = await payPagerRun({ y: 2026, m: 8, d: 3, skip: 0, done: false }, async () => ({ payments: [], has_more: false }), NOW, () => false);
  assert.equal(res.cursor.done, true);
  assert.equal(res.requests, 3);
});

test('fmtPayLocal', () => {
  assert.equal(fmtPayLocal('2026-10-13 23:30:00'), '2026-10-14 00:30');
  assert.equal(fmtPayLocal('2026-12-01 10:05:09'), '2026-12-01 10:05');
  assert.equal(fmtPayLocal('garbage'), 'garbage');
});

test('fmtBackTo', () => {
  assert.equal(fmtBackTo({ y: 2026, m: 9, d: 3 }), '3 Oct');
});
