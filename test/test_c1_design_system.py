"""
UBS 2.0 C1 - THE DESIGN SYSTEM.

C1 replaced improvisation with a vocabulary: eight type steps instead of 36
literal font sizes, four real font weights instead of nine (four of which the
browser was synthesising), one spacing scale, and four status families instead
of three treatments with no "action needed" among them.

This suite is the ratchet. Every check here exists because the value it pins
drifted once already, and the point of a design system is that the next person
reaches for the token rather than typing a number. It therefore asserts the
PROPERTIES rather than a snapshot: how many distinct literals survive, whether
every weight is one the loaded faces ship, whether each status family is used
where it should be - so it keeps biting as the UI grows.

Contrast ratios are computed here, not copied from the audit, so a colour that
is quietly lightened later fails rather than merely looking wrong.

    python test/test_c1_design_system.py
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _ubs_test_db  # noqa: F401,E402  - throwaway database; see test/_ubs_test_db.py

CSS_RAW = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
# Comments explain the OLD values and the reasoning. They are prose, not code,
# and must never satisfy or break an assertion - the precedent set in C0.
CSS = re.sub(r"/\*.*?\*/", "", CSS_RAW, flags=re.S)
JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

_results = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


ROOT_BLOCK = re.search(r":root\s*\{(.*?)\}", CSS, flags=re.S).group(1)
TOKENS = {m.group(1): m.group(2).strip()
          for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", ROOT_BLOCK)}


def token(name):
    return TOKENS.get(name, "")


def resolve(value, depth=0):
    if depth > 6:
        return value
    return re.sub(r"var\((--[\w-]+)\)",
                  lambda m: resolve(TOKENS.get(m.group(1), m.group(0)), depth + 1),
                  value)


def decls(prop, text=CSS):
    return [v.strip() for v in
            re.findall(rf"(?<![-\w]){re.escape(prop)}\s*:\s*([^;}}]+)", text)]


# ── contrast, computed ───────────────────────────────────────────────────
def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexcolor):
    h = hexcolor.strip().lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def main():
    # ═══════════════════════════════════════════════════════════════════
    section("1. THE TYPE SCALE IS THE ONLY SOURCE OF SIZES")
    for t in ("--t-hero", "--t-display", "--t-h1", "--t-h2", "--t-h3",
              "--t-body", "--t-sm", "--t-label"):
        check(f"{t} is defined ({token(t)})", bool(token(t)))
    check("the scale is ordered, display down to label",
          [int(re.search(r"(\d+)px", token(t)).group(1)) for t in
           ("--t-display", "--t-h1", "--t-h2", "--t-h3", "--t-body",
            "--t-sm", "--t-label")] == [30, 24, 18, 15, 14, 13, 11])
    check("every step has a line height",
          all(token(f"--lh-{s}") for s in
              ("hero", "display", "h1", "h2", "h3", "body", "sm", "label")))

    sizes = [v for v in decls("font-size") if not v.startswith("var(")]
    # 16px on inputs is what stops iOS zooming the page on focus. It is the one
    # deliberate literal and it is allowed to stay one.
    unexpected = [s for s in sizes if s != "16px"]
    check(f"font-size literals outside the scale: {len(unexpected)} ({unexpected})",
          not unexpected)
    # RE-PINNED (Phase C3): this required EXACTLY one 16px literal. The guard
    # needs two declarations, not one - the bare type selectors it was written
    # with (0,0,1) lose to the base input styling's attribute list (0,1,1), so
    # it never reached an <input> until C3 repeated it at matching specificity.
    # Counting the declarations was never the property; what matters is that
    # every literal left in the stylesheet IS that guard and nothing else has
    # crept in beside it, which is what line 110 above already measures.
    check(f"every remaining literal is the iOS zoom guard ({sizes})",
          sizes and set(sizes) == {"16px"} and "16px stops iOS zoom" in CSS_RAW)
    check(f"{len([v for v in decls('font-size') if v.startswith('var(')])} "
          "declarations use a token", len(decls("font-size")) - len(sizes) > 250)

    section("1b. NO FONT SHORTHAND SMUGGLES A SIZE BACK IN")
    # Three `font:` shorthands carried a size and a weight past the first
    # migration pass and rendered 14.5px and 12.5px in the browser.
    shorthand = re.findall(r"(?<![-\w])font\s*:\s*([^;}]+)", CSS)
    bad = [f for f in shorthand if re.search(r"\d+(\.\d+)?px", f)]
    check(f"no font shorthand carries a literal size ({bad})", not bad)

    section("1c. INLINE STYLES DO NOT BYPASS THE SCALE")
    for name, text in (("app.js", JS), ("index.html", HTML)):
        inline = re.findall(r'style="[^"]*font-size:\s*([^;"]+)', text)
        literal = [v for v in inline if not v.strip().startswith("var(")]
        check(f"{name}: no inline literal font-size ({literal})", not literal)

    # ═══════════════════════════════════════════════════════════════════
    section("2. ONLY WEIGHTS THE LOADED FACES ACTUALLY SHIP")
    imported = re.search(r"@import url\('([^']+)'\)", CSS_RAW).group(1)
    families = dict(re.findall(r"family=([A-Za-z+ ]+):wght@([0-9;]+)", imported))
    loaded = {k.replace("+", " "): set(v.split(";")) for k, v in families.items()}
    check(f"Manrope ships {sorted(loaded['Manrope'])}",
          {"500", "600", "700", "800"} <= loaded["Manrope"])
    check(f"Inter ships {sorted(loaded['Inter'])}",
          {"400", "600", "700"} <= loaded["Inter"])
    check(f"IBM Plex Mono ships {sorted(loaded['IBM Plex Mono'])}",
          {"500", "600"} <= loaded["IBM Plex Mono"])

    for t, v in (("--w-regular", "400"), ("--w-semibold", "600"),
                 ("--w-bold", "700"), ("--w-black", "800")):
        check(f"{t} is {v}", token(t) == v)

    weights = {v for v in decls("font-weight") if not v.startswith("var(")}
    check(f"font-weight literals remaining: {len(weights)} ({sorted(weights)})",
          not weights)
    used = {resolve(v) for v in decls("font-weight")}
    synthetic = {w for w in used if w not in {"400", "600", "700", "800"}}
    check(f"no synthesised weight survives ({sorted(synthetic)})", not synthetic)
    check("  ...which means 550, 650, 740 and 750 are gone",
          not any(re.search(rf"font-weight\s*:\s*{w}\b", CSS)
                  for w in (550, 650, 740, 750)))

    section("2b. MONO NEVER ASKS FOR A WEIGHT IT DOES NOT HAVE")
    # IBM Plex Mono ships 500 and 600. .enr asked for 700 and got a fake bold.
    mono_rules = re.findall(r"([^{}]*mono[^{}]*)\{([^{}]*)\}", CSS, flags=re.I)
    offenders = []
    for sel, body in mono_rules:
        m = re.search(r"font-weight\s*:\s*([^;}]+)", body)
        if m and resolve(m.group(1)) not in ("", "400", "500", "600"):
            offenders.append(f"{sel.strip()[:30]}={m.group(1)}")
    check(f"no mono rule exceeds 600 ({offenders})", not offenders)

    # ═══════════════════════════════════════════════════════════════════
    section("3. THE SPACING SCALE IS ADOPTED, NOT JUST DECLARED")
    for i, px in enumerate((4, 8, 12, 16, 24, 32, 48, 64), start=1):
        check(f"--s{i} is {px}px", token(f"--s{i}") == f"{px}px")

    SPACING_PROPS = ("padding", "padding-top", "padding-right", "padding-bottom",
                     "padding-left", "margin", "margin-top", "margin-right",
                     "margin-bottom", "margin-left", "gap", "row-gap",
                     "column-gap")
    tokenised = literal = 0
    for prop in SPACING_PROPS:
        for v in decls(prop):
            for part in v.split():
                if part.startswith("var("):
                    tokenised += 1
                elif re.fullmatch(r"-?\d*\.?\d+px", part):
                    literal += 1
    share = tokenised / (tokenised + literal)
    check(f"{share:.0%} of spacing values are tokens "
          f"({tokenised} token, {literal} literal)", share >= 0.85)
    # The audit measured 16% before C1. A regression below that floor means
    # someone has started typing numbers again.
    check("  ...far above the 16% this started at", share > 0.50)

    # ═══════════════════════════════════════════════════════════════════
    section("4. FOUR STATUS FAMILIES, ONE PER MEANING")
    for fam in ("neutral", "action", "complete", "attention"):
        check(f"--status-{fam}-bg and -ink are defined",
              token(f"--status-{fam}-bg") and token(f"--status-{fam}-ink"))
    for cls in (".status-neutral", ".status-action-needed", ".status-complete",
                ".status-attention"):
        check(f"{cls} is a real class", f"{cls}{{" in CSS.replace(" ", ""))

    check("the old names survive as aliases, so no caller breaks",
          all(f"{c}{{" in CSS.replace(" ", "")
              for c in (".status-opp", ".status-verified", ".status-danger")))
    # REWRITTEN (Phase C3), not loosened - the same rewrite C2 applied to the
    # identical assertion about .btn. A raw count of ".status-pill{" counts
    # every block that MENTIONS the pill, so C3's responsive override
    # (min-height:44px, inside the touch band) read as a second source of truth
    # although it establishes no appearance at all. What must be unique is the
    # block that defines the pill's APPEARANCE - before C1 there were two of
    # those, disagreeing. Two disagreeing appearance blocks still fail here; a
    # responsive size override no longer does.
    _pill = [b for s, b in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS)
             if ".status-pill" in s.replace(" ", "")
             and "display:inline-flex" in b.replace(" ", "")]
    check("the duplicate pill block is gone - one source of truth "
          f"({len(_pill)})", len(_pill) == 1)

    section("4b. THE FAMILY COMES FROM THE STEP KEY, NEVER THE LABEL")
    check("app.js maps workflow_step_key to a family",
          "WORKFLOW_STATUS" in JS and "e.workflow_step_key" in JS)
    check("  ...and falls back to the backend's own flags",
          "workflow_is_terminal" in JS and "workflow_is_blocked" in JS)
    check("  ...the label text is never parsed to pick a colour",
          not re.search(r"workflow_step_label\s*\.\s*(includes|match|indexOf)", JS))
    check("the backend vocabulary is untouched",
          "BLOCKED_STEP_KEYS" in (ROOT / "services" / "perch" / "workflow.py")
          .read_text(encoding="utf-8"))

    section("4c. EVERY FAMILY IS ACTUALLY USED")
    table = JS.split("const WORKFLOW_STATUS")[1].split("};")[0]
    for fam in ("neutral", "action-needed", "complete", "attention"):
        n = table.count(f"'{fam}'")
        check(f"{fam}: {n} step(s)", n >= 1)
    check("waiting on the customer is action-needed, not neutral",
          all(f"{k}:" in table.replace(" ", "")
              for k in ("proof_docs", "self_attestation", "contracts_accept")))
    check("  ...and those three are gold, not grey",
          all(re.search(rf"{k}:\s*\['action-needed'", table)
              for k in ("proof_docs", "self_attestation", "contracts_accept")))
    check("a settled 'not eligible' outcome is neutral, not an error",
          re.search(r"lmi_not_qualified:\s*\['neutral',\s*'Not eligible'\]", table))
    check("a genuine problem is attention",
          re.search(r"contracts_accept_uncertain:\s*\['attention'", table))

    section("4d. SHORT LABELS IN LISTS, FULL LABEL PRESERVED")
    check("the list pill uses the short label", "statusShortLabel(e)" in JS)
    check("  ...and carries the full one on title", "'title=\"' + esc(full)" in JS)
    check("  ...and in the accessible name", "' — ' + full" in JS)
    check("the 40-character sentence is gone from the list",
          "'Not eligible'" in JS)
    longest = max(len(m) for m in re.findall(r"\['(?:neutral|action-needed|"
                                             r"complete|attention)',\s*'([^']+)'\]", table))
    check(f"the longest short label is {longest} characters", longest <= 22)

    # ═══════════════════════════════════════════════════════════════════
    section("5. COLOUR MEETS WCAG AA WHERE IT CARRIES TEXT")
    WHITE, BG, SUNK = "#FFFFFF", token("--bg"), token("--surface-sunk")
    for name, surface in (("white", WHITE), ("--bg", BG), ("--surface-sunk", SUNK)):
        for ink in ("--ink", "--ink-muted", "--ink-faint"):
            r = ratio(token(ink), surface)
            check(f"{ink} on {name}: {r:.2f}:1", r >= 4.5)
    check("the three inks stay in order, so hierarchy survives",
          ratio(token("--ink"), WHITE) > ratio(token("--ink-muted"), WHITE)
          > ratio(token("--ink-faint"), WHITE))

    section("5b. GREEN THAT CARRIES WHITE TEXT IS THE ACCESSIBLE ONE")
    check(f"white on --brand-solid: {ratio('#FFFFFF', token('--brand-solid')):.2f}:1",
          ratio("#FFFFFF", token("--brand-solid")) >= 4.5)
    check(f"  ...where white on --brand is only "
          f"{ratio('#FFFFFF', token('--brand')):.2f}:1, which is why it is not used that way",
          ratio("#FFFFFF", token("--brand")) < 4.5)
    check("the primary button uses it",
          "background:var(--brand-solid);color:#fff" in CSS.replace(" ", ""))
    check("  ...and hover goes darker still, not lighter",
          ratio("#FFFFFF", token("--brand-dark"))
          > ratio("#FFFFFF", token("--brand-solid")))
    # Only the `color` property - not border-color, accent-color or
    # border-top-color, all of which legitimately carry the identity green.
    brand_text = re.findall(r"(?<![-\w])color\s*:\s*var\(--brand\)\s*[;}]", CSS)
    check(f"--brand is never set as TEXT colour ({len(brand_text)} uses)",
          not brand_text)
    check(f"brand text uses --brand-dark: "
          f"{ratio(token('--brand-dark'), WHITE):.2f}:1 on white",
          ratio(token("--brand-dark"), WHITE) >= 4.5)

    section("5c. EVERY STATUS FAMILY IS LEGIBLE ON ITS OWN FILL")
    for fam in ("neutral", "action", "complete", "attention"):
        bg = resolve(token(f"--status-{fam}-bg"))
        ink = token(f"--status-{fam}-ink")
        r = ratio(ink, bg)
        check(f"{fam}: {ink} on {bg} = {r:.2f}:1", r >= 4.5)

    # ═══════════════════════════════════════════════════════════════════
    section("6. THE REST OF THE VOCABULARY")
    for t in ("--radius-xs", "--radius-sm", "--radius", "--radius-lg",
              "--radius-pill", "--shadow-xs", "--shadow", "--shadow-md",
              "--shadow-lg", "--shadow-btn", "--shadow-modal",
              "--border-hairline", "--border-control", "--focus-ring",
              "--focus-ring-danger", "--ring-selected",
              "--font-sans", "--font-display", "--font-mono"):
        check(f"{t} is defined", bool(token(t)))

    check("focus is one ring, used by name",
          CSS.count("box-shadow:var(--focus-ring)") >= 6)
    check("  ...and selection is NOT the same token",
          "box-shadow:var(--ring-selected)" in CSS)
    check("the three faces are named once, not retyped per rule",
          "'Inter'" in token("--font-sans")
          and "'Manrope'" in token("--font-display")
          and "'IBM Plex Mono'" in token("--font-mono"))
    # 46 declarations spelled the same three faces five different ways, and
    # eight were a bare 'Manrope' with NO fallback - so a blocked font
    # request left those headings in the browser default rather than the
    # system sans the other rules named.
    families = re.findall(r"font-family:\s*(?!var\()([^;}]+)", CSS)
    stray = [f.strip() for f in families if f.strip() != "inherit"]
    check(f"every font-family is a token ({stray})", not stray)
    # A <button> does not inherit font-family. The sign-in screen's two
    # buttons declared none and rendered in the UA default while every
    # label around them was Inter.
    # Every block that targets the control, not just the first one found.
    # re.search() took the FIRST ".ubs-toggle{" in the file, which from C3 on
    # is the touch-sizing rule ".pw-eye,.ubs-toggle{min-width:44px...}" - a
    # block that sets a size and was never meant to set a face. The property
    # is that the control names its face SOMEWHERE, so all of its blocks are
    # collected and the declaration is looked for across them. (Same helper
    # bug as the selector-list matching fixed in C2 and C3.)
    for sel in (".ubs-toggle", ".ubs-submit", ".search-input", ".pw-eye"):
        bodies = [b for s, b in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS)
                  if sel in [p.strip() for p in s.replace("\n", " ").split(",")]]
        check(f"{sel} names its own face ({len(bodies)} blocks)",
              any("font-family:var(--font-sans)" in b for b in bodies))
    for t_, must in (("--font-sans", "sans-serif"),
                     ("--font-display", "sans-serif"),
                     ("--font-mono", "monospace")):
        check(f"  ...{t_} ends in a real fallback", token(t_).endswith(must))

    section("6b. MOTION TOKENS NO LONGER COLLIDE WITH THE TYPE SCALE")
    # --t-base meaning 200ms next to --t-body meaning 14px was a trap.
    check("durations are --dur-*",
          all(token(f"--dur-{s}") for s in ("quick", "base", "slow")))
    check("  ...and no --t-* token is a duration",
          not any(v.endswith("ms") for k, v in TOKENS.items()
                  if k.startswith("--t-")))
    check("  ...reduced motion still collapses them",
          "--dur-base:1ms" in CSS)

    # ═══════════════════════════════════════════════════════════════════
    section("7. C1 DID NOT TOUCH BEHAVIOUR")
    check("Perch next_step is still the router",
          "NEXT_STEP_PATH_MAP" in (ROOT / "services" / "perch" / "workflow.py")
          .read_text(encoding="utf-8"))
    check("the status pill is still the resume control",
          "onclick=\"openEnrollment(" in JS)
    check("auth handling is unchanged", "tryRestoreSession" in JS)
    check("the markets vocabulary is unchanged",
          all(s in (ROOT / "services" / "perch" / "workflow.py")
              .read_text(encoding="utf-8")
              for s in ("lmi_not_qualified", "contracts_accept_uncertain")))
    check("the photo/document deletion issue is still recorded as OPEN",
          "**Status:** OPEN" in (ROOT / "KNOWN_ISSUES.md").read_text(encoding="utf-8"))

    print(f"\n{'=' * 72}")
    print(f"{sum(_results)} checks passed, {len(_results) - sum(_results)} failed")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
