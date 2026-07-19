/**
 * Unit tests for the watchlist reconciliation · web/static/watchlist.js.
 * Run with:  node --test tests/js
 *
 * The rail is the desk's most-used surface and its logic is set arithmetic
 * with eight curated sections riding on it. Every test here is a way the
 * naive version loses somebody's work: flattening the layout, resurrecting a
 * delete, silently dropping a colour flag, or duplicating a symbol that was
 * filed in two places.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const WLIB = require('../../web/static/watchlist.js');

const wl = (...sections) => ({ sort: null, sections });
const S = (name, syms, extra = {}) =>
  Object.assign({ name, open: true, items: syms.map(s => ({ s })) }, extra);

// ------------------------------------------------------------------- adopt

test('a symbol on both lists keeps its section, its position and its flag', () => {
  const local = wl(
    S('INDEX', ['SPY', 'QQQ']),
    { name: 'CORE', open: false, items: [{ s: 'NVDA', flag: '#f4657f' }, { s: 'PLTR' }] },
  );
  const out = WLIB.adopt(local, ['NVDA', 'SPY', 'PLTR', 'QQQ']);

  assert.deepEqual(out.map(s => s.name), ['INDEX', 'CORE'], 'sections survive in order');
  assert.deepEqual(out[0].items.map(i => i.s), ['SPY', 'QQQ'], 'and so does position within one');
  assert.equal(out[1].open, false, 'collapse state is presentation and stays local');
  assert.equal(out[1].items[0].flag, '#f4657f', 'the colour flag rides along with the symbol');
});

test('a symbol the shared list dropped leaves the rail · a delete has to stick', () => {
  const out = WLIB.adopt(wl(S('CORE', ['NVDA', 'PLTR'])), ['NVDA']);
  assert.deepEqual(out[0].items.map(i => i.s), ['NVDA'],
    'PLTR was removed on another browser and must not survive here');
});

test('a symbol only the shared list has lands in an inbox section, not scattered by guesswork', () => {
  const out = WLIB.adopt(wl(S('CORE', ['NVDA'])), ['NVDA', 'BTC', 'AAPL']);

  assert.deepEqual(out.map(s => s.name), ['CORE', WLIB.INBOX]);
  assert.deepEqual(out[1].items.map(i => i.s), ['BTC', 'AAPL'], 'in the shared list order');
  assert.equal(out[1].open, true, 'and open, because arriving invisibly is the same as not arriving');
});

test('the inbox is created only when there is something to put in it', () => {
  const out = WLIB.adopt(wl(S('CORE', ['NVDA'])), ['NVDA']);
  assert.deepEqual(out.map(s => s.name), ['CORE'], 'no empty section appears on every load');
});

test('a second arrival appends to the existing inbox rather than making a second one', () => {
  const first = WLIB.adopt(wl(S('CORE', ['NVDA'])), ['NVDA', 'BTC']);
  const second = WLIB.adopt({ sections: first }, ['NVDA', 'BTC', 'AAPL']);

  assert.deepEqual(second.map(s => s.name), ['CORE', WLIB.INBOX]);
  assert.deepEqual(second[1].items.map(i => i.s), ['BTC', 'AAPL']);
});

test('an empty section the user named is kept, not tidied away', () => {
  const out = WLIB.adopt(wl(S('INDEX', ['SPY']), S('ON DECK', ['RKLB'])), ['SPY']);
  assert.deepEqual(out.map(s => s.name), ['INDEX', 'ON DECK'],
    'the section is a thing they made and may be about to refill');
  assert.deepEqual(out[1].items, []);
});

test('a symbol filed in two sections locally is kept once, where it was filed first', () => {
  const out = WLIB.adopt(wl(S('INDEX', ['SPY']), S('CORE', ['SPY', 'NVDA'])), ['SPY', 'NVDA']);
  assert.deepEqual(out[0].items.map(i => i.s), ['SPY']);
  assert.deepEqual(out[1].items.map(i => i.s), ['NVDA'], 'and not duplicated into the inbox either');
});

test('the shared list is matched case-insensitively · the broker upper-cases, the rail may not', () => {
  const out = WLIB.adopt(wl(S('CORE', ['NVDA'])), ['nvda']);
  assert.deepEqual(out[0].items.map(i => i.s), ['NVDA'], 'not dropped and not re-added as a duplicate');
  assert.equal(out.length, 1, 'no inbox section for a symbol that was already there');
});

test('adopt mutates nothing it was handed · a failed render must not corrupt the rail', () => {
  const local = wl(S('CORE', ['NVDA', 'PLTR']));
  const before = JSON.stringify(local);
  WLIB.adopt(local, ['NVDA']);
  assert.equal(JSON.stringify(local), before);
});

test('an empty shared list empties the rail · it is a real answer, not a missing one', () => {
  // The DEGRADATION case is handled a layer up: the page only calls adopt
  // when the bootstrap said available AND linked. Reaching here with [] means
  // the broker genuinely answered "you follow nothing".
  const out = WLIB.adopt(wl(S('CORE', ['NVDA'])), []);
  assert.deepEqual(out[0].items, []);
});

// --------------------------------------------------------------- symbolsOf

test('symbolsOf walks every section, collapsed ones included', () => {
  const local = wl(S('INDEX', ['SPY']), S('CORE', ['NVDA'], { open: false }));
  assert.deepEqual(WLIB.symbolsOf(local), ['SPY', 'NVDA'],
    'a collapsed section is still on the list · hiding it is presentation');
});

test('symbolsOf survives a malformed or absent layout', () => {
  assert.deepEqual(WLIB.symbolsOf(null), []);
  assert.deepEqual(WLIB.symbolsOf({}), []);
  assert.deepEqual(WLIB.symbolsOf(wl({ name: 'X', open: true })), []);
});
