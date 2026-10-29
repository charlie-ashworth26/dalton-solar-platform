/* UBS 2.0 C3 — THE 390px MEASUREMENT.
 *
 * THE DEFECT:
 *   The sidebar was 230px wide at every width. At 390px that left the content
 *   160px — 41% of the screen. Tables did not fit, rows ran hundreds of pixels
 *   past the right edge, and html,body{overflow-x:hidden} removed the
 *   scrollbar that would have shown it. The data was not somewhere to scroll
 *   to; it was gone.
 *
 * WHY THIS FILE EXISTS:
 *   The defect survived because nothing measured it. This resolves the
 *   stylesheet's cascade at six viewport widths and works out how much width
 *   the shell takes from the content at each one. It needs no browser and no
 *   server, so it runs in the ordinary suite rather than only when someone
 *   remembers to open a phone — which is the whole reason the regression went
 *   unnoticed the first time.
 *
 *   The live-browser numbers are richer, but a check that only runs by hand
 *   is not a regression check.
 *
 *   node test/c3_responsive_harness.js
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const RAW = fs.readFileSync(path.join(ROOT, 'static', 'css', 'app.css'), 'utf8');
// Comments explain the OLD values and the reasoning; they are prose, not code.
const CSS = RAW.replace(/\/\*[\s\S]*?\*\//g, '');

let pass = 0, fail = 0;
function check(label, cond, detail) {
  if (cond) { pass++; console.log(`  [PASS] ${label}${detail ? '  — ' + detail : ''}`); }
  else { fail++; console.log(`  [FAIL] ${label}${detail ? '  — ' + detail : ''}`); }
}
const section = (t) => console.log(`\n${'='.repeat(72)}\n${t}\n${'='.repeat(72)}`);

/* ── a very small cascade resolver ───────────────────────────────────────
   Enough to answer "what applies to this selector at this width": the
   top-level rules, then each max-width block that the width satisfies, in
   source order. Later wins, which is how the cascade behaves for equal
   specificity — and every rule this file asks about is a plain class. */
function blocks(css) {
  const out = [];
  let i = 0;
  while (i < css.length) {
    const at = css.indexOf('@media', i);
    const brace = css.indexOf('{', i);
    if (brace === -1) break;
    if (at !== -1 && at < brace) {
      const cond = css.slice(at + 6, css.indexOf('{', at)).trim();
      let j = css.indexOf('{', at) + 1, depth = 1;
      while (j < css.length && depth) {
        if (css[j] === '{') depth++;
        else if (css[j] === '}') depth--;
        j++;
      }
      out.push({ cond, body: css.slice(css.indexOf('{', at) + 1, j - 1), at });
      i = j;
    } else {
      // Collect the run of top-level rules before the next @media.
      const end = at === -1 ? css.length : at;
      out.push({ cond: null, body: css.slice(i, end), at: i });
      i = end;
    }
  }
  return out;
}

const BLOCKS = blocks(CSS);

function appliesAt(cond, width) {
  if (!cond) return true;
  if (/prefers-reduced-motion|print/.test(cond)) return false;
  const max = /max-width:\s*(\d+)px/.exec(cond);
  const min = /min-width:\s*(\d+)px/.exec(cond);
  if (max && width > +max[1]) return false;
  if (min && width < +min[1]) return false;
  return true;
}

/* Declarations applying to `selector` at `width`, later rules last. */
function declsAt(selector, width) {
  const want = selector.replace(/\s+/g, ' ').trim();
  const found = [];
  for (const b of BLOCKS) {
    if (!appliesAt(b.cond, width)) continue;
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(b.body))) {
      const norm = m[1].replace(/\s+/g, ' ').trim();
      const parts = norm.split(',').map((s) => s.trim());
      if (norm === want || parts.includes(want)) found.push(m[2]);
    }
  }
  return found.join(';');
}

/* The last value wins, as it does in the cascade. */
function prop(selector, name, width) {
  const d = declsAt(selector, width);
  const re = new RegExp('(?:^|;|\\s)' + name + '\\s*:\\s*([^;]+)', 'g');
  let m, last = null;
  while ((m = re.exec(d))) last = m[1].trim();
  return last;
}

/* How much of the viewport the shell takes before any content is laid out. */
function shellCost(width) {
  const shellDisplay = prop('.app-shell.active', 'display', width) || 'flex';
  const pos = prop('.sidebar', 'position', width) || 'static';
  const transform = prop('.sidebar', 'transform', width) || 'none';
  const sidebarWidth = parseInt(prop('.sidebar', 'width', width) || '0', 10);
  const inFlow = shellDisplay === 'flex' && pos !== 'fixed' && pos !== 'absolute';
  const offCanvas = /translateX\(-100%\)/.test(transform);
  return {
    shellDisplay, pos, transform, sidebarWidth, inFlow, offCanvas,
    cost: inFlow ? sidebarWidth : 0,
    content: width - (inFlow ? sidebarWidth : 0),
  };
}

const WIDTHS = [1440, 1024, 768, 600, 390, 320];

section('1. HOW MUCH WIDTH THE SHELL TAKES, BY VIEWPORT');
console.log('  width   shell     sidebar      content   share');
const measured = {};
for (const w of WIDTHS) {
  const s = shellCost(w);
  measured[w] = s;
  const share = (s.content / w);
  console.log(
    `  ${String(w).padEnd(7)} ${s.shellDisplay.padEnd(9)} `
    + `${(s.inFlow ? s.sidebarWidth + 'px in flow' : 'off-canvas').padEnd(12)} `
    + `${String(s.content).padStart(5)}   ${(share * 100).toFixed(0)}%`);
}

section('2. BELOW THE BREAKPOINT THE CONTENT GETS THE WHOLE VIEWPORT');
for (const w of [768, 600, 390, 320]) {
  const s = measured[w];
  check(`${w}px: the sidebar is out of the flow`, !s.inFlow, s.pos);
  check(`${w}px:   ...translated off-canvas`, s.offCanvas, s.transform);
  check(`${w}px:   ...so the content gets all ${w}px`, s.content === w,
    `${s.content}px (${((s.content / w) * 100).toFixed(0)}%)`);
}

section('3. THE 390px DEFECT, NAMED AND PINNED');
// The exact numbers from the Phase C audit, so a regression reads as the
// regression it is rather than as an arbitrary threshold.
const at390 = measured[390];
check('390px: the content is NOT 160px as it was before C3',
  at390.content !== 160, `${at390.content}px`);
check('390px:   ...and not 41% of the screen', at390.content / 390 > 0.9,
  `${((at390.content / 390) * 100).toFixed(0)}%`);
const at320 = measured[320];
check('320px: the content is NOT 90px as it was before C3',
  at320.content !== 90, `${at320.content}px`);
check('320px:   ...and not 28% of the screen', at320.content / 320 > 0.9,
  `${((at320.content / 320) * 100).toFixed(0)}%`);

section('4. THE DESKTOP LAYOUT IS UNCHANGED');
for (const w of [1440, 1024]) {
  const s = measured[w];
  check(`${w}px: the sidebar is still in the flow`, s.inFlow, s.pos);
  check(`${w}px:   ...at its original 230px`, s.sidebarWidth === 230,
    `${s.sidebarWidth}px`);
}

section('5. A TOP BAR REPLACES IT, NOT NOTHING');
for (const w of [768, 600, 390, 320]) {
  check(`${w}px: the top bar is shown`,
    prop('.topbar', 'display', w) === 'flex', prop('.topbar', 'display', w));
  const h = prop('.topbar-menu', 'height', w);
  check(`${w}px:   ...with a menu button of at least 44px`,
    parseInt(h || '0', 10) >= 44, h);
}
for (const w of [1440, 1024]) {
  check(`${w}px: the top bar stays hidden`,
    prop('.topbar', 'display', w) === 'none', prop('.topbar', 'display', w));
}

section('6. NOTHING HIDES HORIZONTAL OVERFLOW');
const crutch = /(?:^|\})\s*(?:html|body|html\s*,\s*body)[^{}]*\{[^}]*overflow-x\s*:\s*hidden/
  .test(CSS);
check('html/body do not set overflow-x:hidden', !crutch);
check('  ...nor does a universal selector',
  !/^\s*\*[^{}]*\{[^}]*overflow-x\s*:\s*hidden/m.test(CSS));
check('the containment that IS load-bearing stays',
  /img,\s*svg,\s*table\s*\{[^}]*max-width\s*:\s*100%/.test(CSS));

section('7. TABLES HAVE SOMEWHERE TO GO AT EVERY WIDTH');
for (const w of [1024, 768]) {
  const ox = prop('.table-wrap', 'overflow-x', w);
  const rowDisplay = prop('.table tbody tr,.data-table tbody tr', 'display', w);
  if (w >= 900) {
    check(`${w}px: a wide table scrolls inside its own card`, ox === 'auto', ox);
  } else {
    check(`${w}px: rows become cards`, rowDisplay === 'block', rowDisplay);
  }
}
for (const w of [600, 390, 320]) {
  check(`${w}px: rows become cards`,
    prop('.table tbody tr,.data-table tbody tr', 'display', w) === 'block');
  const td = declsAt('.table td,.data-table td', w);
  check(`${w}px:   ...and a long value may wrap rather than push the page`,
    /flex-wrap:\s*wrap/.test(td) && /overflow-wrap:\s*anywhere/.test(td));
  const actionBtn = declsAt(
    '.table td.cell-actions .btn,.data-table td.cell-actions .btn', w);
  check(`${w}px:   ...and stacked actions are block-level, not inline`,
    /display:\s*flex/.test(actionBtn) && /width:\s*100%/.test(actionBtn));
}

section('8. TOUCH TARGETS ON TOUCH LAYOUTS ONLY');
// 44px is the floor the brief asks for, so these compare the resolved value
// rather than matching a literal. .btn-sm resolves to 48 - the touch step C2
// already defined - because pinning it to exactly 44 would have added a fourth
// step to a button scale that was deliberately closed at 32/40/48.
const touchSel = [['.nav-link', 'min-height'], ['.status-pill', 'min-height'],
  ['.btn-sm', 'height'], ['.search-input', 'height'],
  ['.pw-eye,.ubs-toggle', 'min-height'], ['.section-title a', 'min-height']];
for (const w of [768, 390]) {
  for (const [sel, name] of touchSel) {
    const v = parseInt(prop(sel, name, w) || '0', 10);
    check(`${w}px: ${sel} reaches 44px`, v >= 44, `${name}:${v}px`);
  }
}
check('desktop keeps the compact button', prop('.btn-sm', 'height', 1440) === '32px',
  prop('.btn-sm', 'height', 1440));
check('desktop keeps the compact nav row',
  !prop('.nav-link', 'min-height', 1440), prop('.nav-link', 'min-height', 1440));

section('9. THE BREAKPOINT LADDER');
const widths = [...new Set(
  (CSS.match(/@media[^{]*?max-width:\s*(\d+)px/g) || [])
    .map((s) => +/(\d+)px/.exec(s)[1]))].sort((a, b) => a - b);
check(`four breakpoints, not thirteen (${widths.join(', ')})`,
  widths.length === 4 && widths.join(',') === '430,599,899,1199');

console.log(`\n${'='.repeat(72)}`);
console.log(`${pass} passed, ${fail} failed`);
console.log('='.repeat(72));
process.exit(fail ? 1 : 0);
