/**
 * watchlist.js · the part of the rail that is SET ARITHMETIC, not painting.
 *
 * The watchlist has two owners on purpose. The broker owns MEMBERSHIP — which
 * symbols this customer follows — because that is user state and two apps
 * cannot share user state through one app's localStorage. The desk owns
 * PRESENTATION — sections, order, colour flags, sort mode — because the
 * broker's (customer_id, symbol) table cannot hold any of it, and because
 * reordering fires on every drag while membership changes a few times a week.
 *
 * Reconciling those two is the only interesting logic in the whole feature,
 * it is pure, and it is exactly the kind of thing that "obviously works"
 * until it silently drops a section. So it lives here, where `node --test
 * tests/js` can reach it, rather than inline in a 1500-line page.
 *
 * The browser loads it with a plain <script> and finds it on window.WLIB.
 * Node requires it. No bundler, no build step.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.WLIB = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /** Where symbols the shared list knows about and no section does end up. */
  var INBOX = 'FROM YOUR BANK';

  /** Every symbol on the rail, in the order the sections are drawn. */
  function symbolsOf(wl) {
    var out = [];
    (wl && wl.sections || []).forEach(function (sec) {
      (sec.items || []).forEach(function (i) { if (i && i.s) out.push(i.s); });
    });
    return out;
  }

  /**
   * Reshape the local layout so its MEMBERSHIP is the shared list's, while
   * every bit of presentation the local layout already had survives.
   *
   * Three rules, and the second is the one that makes this a reconciliation
   * rather than an overwrite:
   *
   *   1. a symbol the shared list dropped leaves the rail. Membership has one
   *      owner; a delete that does not stick is worse than no sharing at all.
   *   2. a symbol on BOTH keeps its section, its position and its colour flag.
   *      This is the whole reason the desk keeps a layout: adopting the
   *      shared list must not flatten eight curated sections into one.
   *   3. a symbol only the shared list has is appended to an inbox section,
   *      created only if there is something to put in it. It is NOT scattered
   *      by guesswork into whichever section looks related, because a wrong
   *      guess is indistinguishable from a bug the user then has to undo.
   *
   * Empty sections are KEPT. A section is a thing the user named and may be
   * about to refill, and tidying it away the first time its last ticker is
   * removed elsewhere is destroying work to save a line of screen.
   *
   * Pure: returns a new sections array, mutates nothing it was handed.
   */
  function adopt(wl, shared, inboxName) {
    var want = {};
    (shared || []).forEach(function (s) { if (s) want[String(s).toUpperCase()] = true; });

    var kept = {};
    var sections = (wl && wl.sections || []).map(function (sec) {
      var items = (sec.items || []).filter(function (i) {
        if (!i || !i.s || !want[i.s]) return false;
        if (kept[i.s]) return false;          // a symbol filed twice locally
        kept[i.s] = true;                     // stays where it was filed first
        return true;
      });
      return Object.assign({}, sec, { items: items });
    });

    var extra = [];
    (shared || []).forEach(function (s) {
      var sym = String(s).toUpperCase();
      if (!kept[sym]) { kept[sym] = true; extra.push({ s: sym }); }
    });

    if (extra.length) {
      var name = inboxName || INBOX;
      var inbox = null;
      for (var k = 0; k < sections.length; k++) {
        if (sections[k].name === name) { inbox = sections[k]; break; }
      }
      if (inbox) inbox.items = inbox.items.concat(extra);
      else sections.push({ name: name, open: true, items: extra });
    }

    return sections;
  }

  return { adopt: adopt, symbolsOf: symbolsOf, INBOX: INBOX };
});
