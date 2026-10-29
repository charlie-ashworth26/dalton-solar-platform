"""
UBS 2.0 C2 - SHARED COMPONENTS.

C1 gave the application a vocabulary of tokens. C2 spends it: the things a rep
actually clicks - buttons, cards, fields, alerts, tables, uploads - are built
once and reused, instead of being hand-styled per screen.

What this suite holds to, and why each one is here:

  * ONE button system. Two complete sets of .btn rules were in this file, with
    different heights and two different meanings for .btn-ghost. Nine button
    heights rendered across seven screens.
  * A CLOSED set of heights: 32 compact, 40 default, 48 touch.
  * Danger is distinguishable from secondary. On the Reps table, Edit, Reset
    password and Deactivate were three identical grey buttons.
  * No bare error text. Every error in the application was a line of coloured
    words with no container and no icon, and the field that caused it kept its
    normal border.
  * No backend internals on screen. The Sales Report told an unauthorised rep
    `Role 'sales_rep' is not permitted to view this report`.
  * One table shell: an empty table said something different, in a different
    style, on each of the three screens that had one.
  * One upload chip, built twice before, and only one of the two copies
    escaped the filename it printed.

It asserts PROPERTIES - how many heights exist, whether any raw message can
reach the screen - rather than a snapshot, so it keeps biting as the UI grows.

    python test/test_c2_components.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _ubs_test_db  # noqa: F401,E402  - throwaway database; see test/_ubs_test_db.py

CSS_RAW = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
CSS = re.sub(r"/\*.*?\*/", "", CSS_RAW, flags=re.S)
JS_RAW = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
JS = re.sub(r"/\*.*?\*/", "", JS_RAW, flags=re.S)
JS = re.sub(r"^\s*//.*$", "", JS, flags=re.M)
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

_results = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def rules(selector):
    """Every declaration block whose selector list contains `selector`."""
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
        parts = [" ".join(s.split()) for s in sel.split(",")]
        if any(p == selector for p in parts):
            out.append(body.replace(" ", ""))
    return out


def rule(selector):
    """All declarations that apply to `selector`, joined.

    A component is usually defined across more than one block - a shared
    token line, the block itself, a responsive override - so taking only the
    first one tests whichever happened to be written first.
    """
    return ";".join(rules(selector))


ROOT_BLOCK = re.search(r":root\s*\{(.*?)\}", CSS, flags=re.S).group(1)
TOKENS = {m.group(1): m.group(2).strip()
          for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", ROOT_BLOCK)}


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def ratio(a, b):
    def lum(h):
        h = h.strip().lstrip("#")
        r, g, bl = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(bl)
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def main():
    # ═══════════════════════════════════════════════════════════════════
    section("1. ONE BUTTON SYSTEM, NOT TWO")
    # A responsive height override and the reduced-motion list are legitimate
    # extra blocks. What must be unique is the block that establishes the
    # button's APPEARANCE - before C2 there were two of those, disagreeing.
    base = [b for b in rules(".btn") if "display:inline-flex" in b]
    check(f"one block defines the button's appearance ({len(base)})",
          len(base) == 1)
    for variant in (".btn-primary", ".btn-secondary", ".btn-ghost",
                    ".btn-danger", ".btn-quiet"):
        defs = [b for b in rules(variant) if "background:" in b]
        check(f"{variant} is defined once ({len(defs)})", len(defs) == 1)
    check(".btn-ghost no longer has two different meanings",
          len([b for b in rules(".btn-ghost") if "background:" in b]) == 1)

    section("1b. HEIGHTS ARE A CLOSED SET")
    heights = set()
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
        if not re.search(r"(?<![-\w])\.btn\b", sel):
            continue
        m = re.search(r"(?<![-\w])height\s*:\s*(\d+)px", body)
        if m:
            heights.add(int(m.group(1)))
    check(f"every .btn height is 32, 40 or 48 ({sorted(heights)})",
          heights <= {32, 40, 48})
    check("  ...and all three are used", heights == {32, 40, 48})
    check("the default is 40px", "height:40px" in base[0])
    check("compact is 32px", "height:32px" in rule(".btn-sm"))
    check("touch is 48px", "height:48px" in rule(".btn-lg"))

    section("1c. DANGER IS NOT SECONDARY")
    danger, secondary = rule(".btn-danger"), rule(".btn-secondary")
    check("danger carries the danger colour", "color:var(--danger)" in danger)
    check("  ...and a danger border", "border-color:var(--danger)" in danger)
    check("  ...which secondary does not", "var(--danger)" not in secondary)
    check("  ...so the two cannot be confused at a glance",
          danger != secondary)
    check("danger goes solid only on hover",
          "background:var(--danger)" in rule(".btn-danger:hover:not(:disabled)")
          and "background:var(--surface)" in danger)
    check("  ...and its focus ring is the danger one",
          "box-shadow:var(--focus-ring-danger)" in rule(".btn-danger:focus-visible"))

    section("1d. DEACTIVATE IS THE DANGER BUTTON")
    # The concrete case from the audit: three identical grey buttons in a row.
    check("Deactivate uses btn-danger",
          re.search(r"active \? 'btn-danger' : 'btn-secondary'", JS) is not None)
    check("  ...and Activate, which is not destructive, does not",
          "'btn-danger' : 'btn-secondary'" in JS)
    check("  ...while Edit stays secondary",
          'btn btn-secondary btn-sm" onclick="editRep(' in JS)
    # RE-PINNED (Phase C4). C2 fixed "three identical grey buttons" by making
    # Deactivate the danger button - which still left Edit and Reset password
    # identical to each other. C4 was asked to organise these actions clearly,
    # so Reset password, the rarer of the two, steps back to the ghost weight.
    # What C2 named is unchanged and is asserted here as what it always meant:
    # three actions, three different weights, danger reserved for the one with
    # consequences.
    check("  ...and Reset password steps back to ghost, not a second secondary",
          'btn btn-ghost btn-sm" onclick="resetRepPassword(' in JS)
    _weights = [m for m in re.findall(
        r'btn (btn-[a-z]+) btn-sm" onclick="(?:editRep|resetRepPassword)', JS)]
    check(f"  ...so the row's three actions carry three weights ({_weights})",
          len(set(_weights)) == 2 and 'btn-danger' not in _weights)

    section("1e. DISABLED IS LEGIBLE, AND NOTHING FADES")
    dis = rule(".btn:disabled")
    check("the disabled rule sets an explicit fill",
          "background:var(--surface-sunk)" in dis)
    check("  ...and explicit text", "color:var(--ink-muted)" in dis)
    check("  ...and never opacity", "opacity" not in dis)
    faded = re.findall(r"[^{}]*:disabled[^{]*\{[^}]*opacity[^}]*\}", CSS)
    check(f"no disabled rule anywhere fades a control ({len(faded)})", not faded)
    r = ratio(TOKENS["--ink-muted"], TOKENS["--surface-sunk"])
    check(f"disabled text is AA on its own fill ({r:.2f}:1)", r >= 4.5)

    # ═══════════════════════════════════════════════════════════════════
    section("2. THREE SURFACES, AND A LIMIT ON NESTING")
    for s in (".surface-panel", ".surface-raised", ".surface-inset"):
        check(f"{s} is defined", bool(rule(s)))
    check("raised earns its depth from shadow, not a border",
          "box-shadow:var(--shadow-md)" in rule(".surface-raised")
          and "border:0" in rule(".surface-raised"))
    check("inset has no shadow at all",
          "box-shadow:none" in rule(".surface-inset"))
    check("a panel inside a panel loses its shadow",
          "box-shadow:none" in rule(".surface-panel .surface-panel"))
    check("every surface uses the C1 radius and spacing tokens",
          all("var(--radius" in rule(s) and "var(--s" in rule(s)
              for s in (".surface-panel", ".surface-raised", ".surface-inset")))

    # ═══════════════════════════════════════════════════════════════════
    section("3. FIELDS SAY WHICH FIELD")
    check("an invalid field takes a red border",
          "border-color:var(--danger)" in
          rule("input.err"))
    check("  ...and the danger focus ring",
          "box-shadow:var(--focus-ring-danger)" in rule("input.err"))
    check("setFieldError marks the input AND writes the message",
          "classList.add('err')" in JS
          and "setAttribute('aria-invalid', 'true')" in JS)
    check("  ...and points the field at its message for a screen reader",
          "setAttribute('aria-describedby'" in JS)
    check("  ...and clearing it removes both",
          "classList.remove('err')" in JS
          and "removeAttribute('aria-invalid')" in JS)
    check("the field error is not bare text: it has an icon",
          "--alert-ico" in rule(".field-error"))

    # ═══════════════════════════════════════════════════════════════════
    section("4. ONE ALERT, FOUR VARIANTS")
    for v in ("info", "success", "warn", "error"):
        body = rule(f".alert-{v}")
        check(f".alert-{v} has a fill", "background:" in body)
        check(f"  ...and an icon of its own", "--alert-ico:url(" in body)
    icons = set(re.findall(r"--alert-ico:url\(\"([^\"]+)\"\)", CSS))
    check(f"the four icons are four different shapes ({len(icons)})",
          len(icons) >= 4)
    check("the alert is a container, not a line of text",
          "padding:var(--s3)" in rule(".alert")
          and "border-radius:var(--radius)" in rule(".alert"))
    check("its colours are the C1 status families, so an error alert and an "
          "attention pill agree",
          "var(--status-attention-bg)" in rule(".alert-error")
          and "var(--status-action-bg)" in rule(".alert-warn"))

    section("4b. EVERY ERROR ELEMENT ADOPTED IT")
    ERROR_IDS = ["login-error", "portal-error", "rep-form-error", "rep-list-error",
                 "sales-report-error", "program-error", "contact-submit-error",
                 "bill-submit-error", "sa-error", "lmi-submit-error", "agr-error",
                 "admin-modal-error"]
    for eid in ERROR_IDS:
        m = re.search(r'<[a-z]+[^>]*id="' + eid + r'"[^>]*>', HTML)
        cls = re.search(r'class="([^"]*)"', m.group(0)).group(1) if m else ""
        check(f"#{eid} is a shared alert", "alert alert-error" in cls)
    leftovers = re.findall(r'id="[^"]*err[^"]*"[^>]*style="[^"]*color:var\(--danger\)', HTML)
    check(f"no error element still carries its own inline red ({len(leftovers)})",
          not leftovers)
    check("the sign-in screen's private error style is gone",
          ".ubs-error{" not in CSS)

    # ═══════════════════════════════════════════════════════════════════
    section("5. NO BACKEND INTERNALS REACH THE SCREEN")
    raw = re.findall(r"(?:textContent|innerHTML)\s*=\s*[^;\n]*\berr(?:or)?\.message", JS)
    check(f"no display site reads err.message directly ({len(raw)})", not raw)
    check("userMessage() exists and is the single filter",
          "function userMessage(err, fallback)" in JS)
    check("  ...it is actually used", JS.count("userMessage(") >= 10)
    check("  ...403 never names an internal role",
          "You do not have access to this" in JS)
    check("  ...a bare status code never reaches a rep",
          r"/^Request failed \(\d+\)$/" in JS)
    check("  ...nor a traceback or a Python exception name",
          "/traceback/i" in JS
          and "KeyError|TypeError|ValueError|AttributeError|NoneType" in JS)
    check("the raw message is still on the error object for logging",
          "apiErr.status = res.status" in JS_RAW and "apiErr.body = data" in JS_RAW)
    check("  ...so callers that branch on it are unaffected",
          "err.status === 403" in JS or "err.status===403" in JS.replace(" ", ""))

    # ═══════════════════════════════════════════════════════════════════
    section("6. ONE TABLE SHELL")
    check("the empty state is a component, not three inline treatments",
          "function tableEmptyHtml(" in JS and "function tableEmptyRowHtml(" in JS)
    check("  ...used by the dashboard", "tableEmptyRowHtml(5," in JS)
    check("  ...by Customers", "tableEmptyRowHtml(6," in JS)
    check("  ...and by Reps", "host.innerHTML = tableEmptyHtml(" in JS)
    check("no inline-styled empty cell survives",
          'colspan="5" style="text-align:center' not in JS
          and 'class="crm-empty"' not in JS)
    check("an empty state says what to do, not only that there is nothing",
          "Start one from the button above" in JS
          and "Try a customer name" in JS)
    check("a loading state exists so blank never means 'loading'",
          "function tableLoadingHtml(" in JS and "tableLoadingHtml(4)" in JS)
    check("  ...and it is drawn, not just defined",
          ".table-skeleton" in CSS)
    check("a row count is shown", 'id="customers-count"' in HTML
          and "customers-count" in JS)
    check("truncation is scoped to the free-text columns",
          "td.cell-cust" in CSS and "td.cell-util" in CSS)
    check("  ...and never clips a row's actions",
          "max-width:none" in rule(".data-table td.cell-actions"))
    check("  ...nor breaks an identifier mid-token",
          "white-space:nowrap" in rule(".data-table td .mono"))
    check("status cells use the C1 families",
          "status-complete" in JS and "status-neutral" in JS)
    check("  ...and a deactivated rep is neutral, not an error",
          "active ? 'status-complete' : 'status-neutral'" in JS)

    # ═══════════════════════════════════════════════════════════════════
    section("7. ONE UPLOAD CHIP")
    check("both flows build the chip through one function",
          "function uploadChipHtml(" in JS
          and JS.count("uploadChipHtml(") >= 3)
    check("  ...the bill flow", "uploadChipHtml(name, status, 'removeBill()')" in JS)
    check("  ...and the proof-document flow",
          "uploadChipHtml(name, status, 'removeLmi()')" in JS)
    check("the filename is escaped in both, which it was not before",
          "esc(name)" in JS[JS.index("function uploadChipHtml"):
                            JS.index("function uploadChipHtml") + 700])
    for state in ("is-uploading", "is-attached", "is-failed", "is-removing"):
        check(f".{state} is styled", f".file-chip.{state}" in CSS)
    check("the state is read from the status the caller already passes",
          "UPLOAD_CHIP_STATE" in JS and "'Attached': 'is-attached'" in JS)

    section("7b. THE DELETION DEFECT IS NEITHER FIXED NOR HIDDEN")
    check("removeBill still routes to the one real delete path",
          "async function removeBill(){" in JS
          and "docSetRemove('utility_bill'" in JS.replace(" ", "").replace(
              "docSetRemove('utility_bill'", "docSetRemove('utility_bill'"))
    check("the issue is still recorded as OPEN",
          "**Status:** OPEN" in (ROOT / "KNOWN_ISSUES.md").read_text(encoding="utf-8"))
    check("  ...and C2 says so where the component is defined",
          "does not persist reliably" in CSS_RAW or "does not persist" in JS_RAW)

    # ═══════════════════════════════════════════════════════════════════
    section("8. C2 CHANGED NO BEHAVIOUR")
    wf = (ROOT / "services" / "perch" / "workflow.py").read_text(encoding="utf-8")
    check("Perch next_step is still the router", "NEXT_STEP_PATH_MAP" in wf)
    check("the workflow vocabulary is unchanged",
          all(k in wf for k in ("lmi_not_qualified", "contracts_accept_uncertain",
                                "BLOCKED_STEP_KEYS", "TERMINAL_STEP_KEYS")))
    check("the status pill is still the resume control",
          'onclick="openEnrollment(' in JS)
    check("auth handling is unchanged", "tryRestoreSession" in JS)
    check("the C1 tokens are intact",
          all(t in TOKENS for t in ("--t-body", "--w-bold", "--s1", "--brand-solid",
                                    "--focus-ring", "--status-action-bg")))
    check("--brand-solid is still #1B7D45, as approved",
          TOKENS["--brand-solid"].upper() == "#1B7D45")
    check("contracts awaiting acceptance is still action-needed gold",
          re.search(r"contracts_accept:\s*\['action-needed'", JS) is not None)

    print(f"\n{'=' * 72}")
    print(f"{sum(_results)} checks passed, {len(_results) - sum(_results)} failed")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
