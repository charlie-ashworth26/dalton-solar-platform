"""
UBS 2.0 PHASE C0 - the three standalone defects from the Phase C audit.

Each was visible in the running app and none depended on the design-system
work that follows, which is why they shipped on their own.

  1. CONTRACT NAMES WERE STRUCK THROUGH. `.agr-link-off` carried
     `text-decoration: line-through` to mean "this document cannot be opened
     from here". Perch issues time-limited document URLs and UBS deliberately
     stores none, so every RESUMED enrollment took that branch - which put a
     line through the three contract names on the screen where a customer is
     asked to agree to them.

  2. THE UNAUTHORISED SALES REPORT LEAKED THE BACKEND. Phase B made
     /sales-report reachable by URL. A rep who followed one got the server's
     own sentence, naming the internal role identifier, as bare red text
     beside a live Export CSV button and above an empty card.

  3. THE DISABLED PRIMARY BUTTON WAS UNREADABLE. `opacity: .45` over the
     brand fill produced white on #B9C8C0 - 2.0:1 - so the wizard's Continue
     button read as absent rather than as not-yet-available.

Runtime proof that the 403 branch actually runs, and that the rendered state
carries no backend text, lives in test/phase_c0_harness.js. The checks here
cover the stylesheet and the structure the harness exercises, and compute the
contrast ratios directly so the suite needs no browser.

    python test/test_phase_c0_defects.py
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

# Collapsed whitespace with whole-line // comments dropped, so these
# assertions test the code and not its formatting.
JSN = " ".join(" ".join(
    ln for ln in JS.splitlines() if not ln.strip().startswith("//")
).split())

_results = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def token(name):
    """Resolve a CSS custom property to its hex value, from the stylesheet."""
    m = re.search(rf"{re.escape(name)}\s*:\s*(#[0-9A-Fa-f]{{3,8}})", CSS)
    assert m, f"token {name} not found"
    return m.group(1)


def _lin(c):
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def contrast(fg_hex, bg_hex):
    def rgb(h):
        h = h.lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    def lum(c):
        r, g, b = rgb(c)
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    a, b = lum(fg_hex), lum(bg_hex)
    hi, lo = max(a, b), min(a, b)
    return round((hi + 0.05) / (lo + 0.05), 2)


def rule(selector):
    """The declaration block for an exact selector, or ''. """
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else ""


def main():
    # ═══════════════════════════════════════════════════════════════════
    section("1. CONTRACT NAMES ARE NOT STRUCK THROUGH")

    off = rule(".agr-link-off")
    check("the unopenable-agreement style still exists", bool(off))
    check("  ...and no longer strikes the name through",
          "line-through" not in off)
    check("  ...it explicitly clears the decoration rather than relying on a "
          "default", "text-decoration:none" in off.replace(" ", ""))
    check("  ...the name is set in body ink, not muted away",
          "color:var(--ink)" in off.replace(" ", ""))
    check("  ...and carries weight, so it still reads as a named document",
          "font-weight:var(--w-semibold)" in off.replace(" ", ""))
    check("NOTHING in the agreement styles strikes text through",
          not any("line-through" in rule(s) for s in
                  (".agr-link", ".agr-link-off", ".agr-docs-list",
                   ".agr-ack", ".agr-details dd")))
    # Scoped to EXECUTABLE css, the same rule this project's other suites use
    # for source checks: the fix is explained in a comment that necessarily
    # quotes the declaration it removed, and a comment styles nothing.
    css_code = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    check("no declaration anywhere in the stylesheet strikes text through",
          "line-through" not in css_code)
    check("which name is OPENABLE is unchanged - still the url_present test",
          "if(idx === null || !c.url_present){" in JSN)
    check("  ...and the unopenable one still explains itself on hover",
          'title="This document is not available to open"' in JS)

    # ═══════════════════════════════════════════════════════════════════
    section("2. THE UNAUTHORISED SALES REPORT IS A DESIGNED STATE")

    check("the loader branches on the HTTP STATUS, not on message text",
          "if(e && e.status === 403){" in JSN)
    check("  ...which apiFetch attaches for exactly this purpose",
          "apiErr.status = res.status;" in JSN)
    check("the Export CSV button is hidden for that rep",
          "if(exportBtn) exportBtn.style.display = 'none';" in JSN)
    check("  ...and restored on every load, so an admin gets it back",
          "if(exportBtn) exportBtn.style.display = '';" in JSN)
    # Scoped to loadSalesReport's own body: the identical "show e.message"
    # line also exists in loadReps, so a whole-file index comparison would
    # compare against the wrong function.
    loader = JSN[JSN.index("async function loadSalesReport(){"):]
    loader = loader[:loader.index("function renderSalesReportAdminOnly")]
    check("the refusal returns BEFORE the bare red error paragraph is reached",
          "renderSalesReportAdminOnly(host); return; }" in loader
          and loader.index("renderSalesReportAdminOnly(host);")
          < loader.index("err.textContent = e.message"))
    check("  ...and that paragraph is still there for a genuine failure",
          "err.textContent = e.message" in loader)
    check("a designed state replaces the empty container",
          "function renderSalesReportAdminOnly(host){" in JSN
          and 'class="state-block"' in JS)

    body = JS[JS.index("function renderSalesReportAdminOnly(host){"):]
    body = body[:body.index("\n}")]
    rendered = " ".join(re.sub(r"'\s*\+\s*'", "", body).split()).replace("\\'", "'")
    for leak in ("sales_rep", "not permitted", "403", "Request failed",
                 "/api/", "role", "Error", "status"):
        check(f"  ...and leaks no backend detail: {leak!r} absent",
              leak.lower() not in rendered.lower())
    check("the state says who it is for",
          "Admins only" in rendered
          and "limited to admin accounts" in rendered)
    check("  ...and what to do instead",
          "Ask an admin" in rendered)
    check("  ...with a way out that is not the action they cannot take",
          "showView('dashboard')" in rendered
          and "btn-secondary" in rendered and "btn-primary" not in rendered)
    check("the state block is styled, not inheriting a helper paragraph",
          ".state-block{" in CSS.replace(" ", "")
          and ".state-title{" in CSS.replace(" ", "")
          and ".state-body{" in CSS.replace(" ", ""))
    check("  ...and has a phone rule",
          ".state-block{padding:var(--s6) var(--s3);}" in CSS.replace("\r", ""))

    section("2b. SERVER-SIDE AUTHORISATION IS UNTOUCHED")
    # The fix is presentation only. The backend still refuses, and still
    # refuses in its own words - which now simply never reach the screen.
    ADMINROUTES = (ROOT / "routes" / "admin_routes.py").read_text(encoding="utf-8")
    check("the report route still requires the admin role",
          '@require_role("admin")' in ADMINROUTES)
    check("  ...and the frontend still asks the same endpoint",
          "apiFetch('/api/admin/sales-report')" in JSN)
    check("the export control is only ever HIDDEN, never re-enabled client-side",
          "exportBtn.disabled" not in JSN)

    # ═══════════════════════════════════════════════════════════════════
    section("3. THE DISABLED BUTTON IS LEGIBLE AND STILL DISABLED")

    # C2 collapsed two button systems into one, and with them the separate
    # .btn-primary:disabled rule - the base rule had been overriding it
    # anyway. The C0 guarantee is unchanged and is now asserted where it
    # lives: one disabled treatment, and nothing that re-fades the primary.
    base = rule(".btn:disabled,.btn[aria-disabled=true]").replace(" ", "")
    prim = rule(".btn-primary:disabled").replace(" ", "")
    check("the global disabled rule no longer fades the button out",
          "opacity" not in base)
    check("  ...and the primary has no competing disabled rule of its own",
          prim == "")
    check("  ...so a disabled primary takes the one legible treatment",
          "background:var(--surface-sunk)" in base)
    check("nothing re-fades a disabled button anywhere in the stylesheet",
          not re.search(r":disabled[^{]*\{[^}]*opacity", css_code))
    check("the old unreadable fill is gone from the stylesheet",
          "#B9C8C0" not in css_code)
    for name, decl in (("base", base),):
        check(f"the {name} disabled state sets an explicit fill",
              "background:var(--surface-sunk)" in decl)
        check(f"  ...explicit text colour ({name})",
              "color:var(--ink-muted)" in decl)
        check(f"  ...a real border ({name})",
              "border-color:var(--border-strong)" in decl)
        check(f"  ...no shadow, so it cannot read as raised ({name})",
              "box-shadow:none" in decl)
        check(f"  ...and keeps the not-allowed cursor ({name})",
              "cursor:not-allowed" in decl)

    fg, bg = token("--ink-muted"), token("--surface-sunk")
    ratio = contrast(fg, bg)
    check(f"disabled text meets WCAG AA on its own fill "
          f"({fg} on {bg} = {ratio}:1)", ratio >= 4.5)
    old = contrast("#FFFFFF", "#B9C8C0")
    check(f"  ...where the previous treatment did not ({old}:1)", old < 4.5)

    # C1 moved the enabled primary from --brand to --brand-solid: white on
    # --brand is 4.38:1, just under AA. The disabled state must still be
    # unmistakably different from whichever green the primary carries.
    brand = token("--brand-solid")
    check(f"it is still visibly NOT the enabled primary "
          f"(enabled fill {brand}, disabled fill {bg})",
          brand.lower() != bg.lower())
    check("  ...and the enabled primary carries white on the accessible green",
          "background:var(--brand-solid);color:#fff"
          in rule(".btn-primary").replace(" ", ""))

    section("3b. WHAT ENABLES A BUTTON IS UNCHANGED")
    for fn in ("checkBillReady", "checkLmiReady", "updateAgreeButton"):
        check(f"{fn}() still owns its own control",
              f"function {fn}(" in JS)
    check("the read-only lock still disables the same control list",
          "const READ_ONLY_LOCK_IDS = [" in JSN
          and "function lockEnrollmentReadOnly(blocked){" in JSN)

    # ═══════════════════════════════════════════════════════════════════
    section("4. C0 CHANGED NOTHING IT WAS NOT MEANT TO")

    check("no enrollment business logic moved",
          "WORKFLOW_STEP_TO_WIZARD" in JSN
          and "if(terminal || blocked){ target = 5;" in JSN)
    check("Perch next_step is still the router",
          "e.workflow_step_key" in JSN and "readOnlyStatusCopy(" in JSN)
    check("the Virginia and New York paths are untouched",
          "perchContext.utilityDisplay = e.utility_name || '';" in JSN
          and "if(addressFormInUse) take('a-state'," in JSN)
    check("the document-removal path is untouched",
          "async function removeBill(){" in JS
          and "await docSetRemove('utility_bill', stored.key);" in JSN
          and 'class="docset-remove"' in JSN)

    # This asserted the photo-deletion issue was still recorded as OPEN, so
    # that C0 could not quietly close it. It was really fixed on 2026-10-28
    # (two hydration paths; the second discarded the document-set id), so the
    # requirement is now that the entry records the resolution and its cause.
    known = (ROOT / "KNOWN_ISSUES.md").read_text(encoding="utf-8")
    check("the photo-deletion issue is recorded as resolved, with its cause",
          "Removing a previously saved bill / proof photo - RESOLVED" in known
          and "TWO HYDRATION PATHS" in known)
    check("  ...and C0 still did not touch the removal control",
          'class="docset-remove"' in JSN and "docSetRemove(" in JS)

    print(f"\n{'=' * 72}")
    print(f"{sum(_results)} checks passed, {len(_results) - sum(_results)} failed")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
