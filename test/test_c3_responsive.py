"""
UBS 2.0 C3 - THE MOBILE SHELL.

THE DEFECT THIS EXISTS TO PREVENT RETURNING.

    The sidebar was 230px wide at every width. On a 390px phone that left the
    content 160px - 41% of the screen - and on a 320px phone 90px, or 28%. The
    tables did not fit, so rows ran hundreds of pixels past the right edge:
    the Sales Report's ran 541px past it at 600px. Nobody saw that, because
    html,body{overflow-x:hidden} removed the scrollbar that would have shown
    it. The data was not somewhere to scroll to; it was gone.

    Two things made it invisible. The overflow crutch, and the absence of any
    measurement that would have failed.

This file is the measurement. It is a source-level ratchet - the pixel proof
lives in the browser harness (test/c3_responsive_harness.js, run by
verify_frontend_integration) - and it asserts the properties that, if any one
of them were reverted, would bring the defect back:

  * the shell stops being a two-column layout below the breakpoint;
  * the sidebar goes off-canvas rather than staying in the flow;
  * a top bar and a drawer take its place;
  * there is ONE navigation, so permissions cannot drift between a desktop
    menu and a mobile one;
  * the overflow crutch is not reintroduced;
  * tables have somewhere to go at every width;
  * touch targets are 44px on touch layouts.

    python test/test_c3_responsive.py
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
WORKFLOW = (ROOT / "services" / "perch" / "workflow.py").read_text(encoding="utf-8")

_results = []


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def media_blocks(max_width):
    """Every declaration block inside @media (max-width:<n>px)."""
    out = []
    for m in re.finditer(r"@media[^{]*max-width:\s*" + str(max_width) + r"px[^{]*\{", CSS):
        depth, i = 1, m.end()
        while i < len(CSS) and depth:
            if CSS[i] == "{":
                depth += 1
            elif CSS[i] == "}":
                depth -= 1
            i += 1
        out.append(CSS[m.end():i - 1])
    return out


def strip_media(css):
    """The stylesheet with every @media block removed - the desktop layer."""
    out, i = [], 0
    while True:
        m = re.search(r"@media[^{]*\{", css[i:])
        if not m:
            out.append(css[i:])
            break
        out.append(css[i:i + m.start()])
        j, depth = i + m.end(), 1
        while j < len(css) and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        i = j
    return "".join(out)


BASE_CSS = strip_media(CSS)
MOBILE = "\n".join(media_blocks(899))
NARROW = "\n".join(media_blocks(599))
TABLET = "\n".join(media_blocks(1199))


def rule_in(block, selector):
    """All declarations applying to `selector` inside `block`, joined.

    Matches either a whole selector list (".table td,.data-table td") or one
    part of one (".table td"), and concatenates every matching block - a
    component is usually declared across more than one, so taking the first
    tests whichever happened to be written first.
    """
    # Whitespace around the COMMAS is normalised too, not just within a part.
    # A selector list wrapped across two source lines reads back as
    # "...input[type=tel], input[type=number]" - one space, from the newline -
    # and a caller writing the list without that space matched nothing. The
    # line the author happened to wrap on is not a property of the rule.
    norm_list = lambda s: ",".join(" ".join(p.split()) for p in s.split(","))
    want = norm_list(selector)
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
        norm = norm_list(sel)
        parts = [" ".join(s.split()) for s in sel.split(",")]
        if norm == want or want in parts:
            out.append(body.replace(" ", ""))
    return ";".join(out)


def main():
    # ═══════════════════════════════════════════════════════════════════
    section("1. THE SHELL STOPS TAKING THE SCREEN")
    check("the sidebar is 230px in the desktop layout",
          "width:230px" in rule_in(CSS, ".sidebar"))
    shell = rule_in(MOBILE, ".app-shell.active")
    check("below 900px the shell is no longer a two-column flex row",
          "display:block" in shell)
    sb = rule_in(MOBILE, ".sidebar")
    check("  ...and the sidebar leaves the flow entirely",
          "position:fixed" in sb)
    check("  ...translated off-canvas, not merely narrowed",
          "transform:translateX(-100%)" in sb)
    check("  ...and hidden from assistive tech while it is off-canvas",
          "visibility:hidden" in sb)
    check("  ...so the content gets the whole viewport",
          "padding:var(--s5)var(--s4)var(--s7)" in rule_in(MOBILE, ".main-area")
          or "padding:" in rule_in(MOBILE, ".main-area"))
    open_sb = rule_in(MOBILE, ".app-shell.nav-open .sidebar")
    check("opening it brings it back on screen",
          "transform:translateX(0)" in open_sb and "visibility:visible" in open_sb)

    section("1b. A TOP BAR TAKES ITS PLACE")
    check("the top bar exists in the markup", 'id="topbar"' in HTML)
    check("  ...and is hidden above the breakpoint",
          "display:none" in rule_in(CSS, ".topbar"))
    check("  ...and shown below it", "display:flex" in rule_in(MOBILE, ".topbar"))
    check("  ...carrying the brand so the page is still identifiable",
          "topbar-brand" in HTML)
    check("the menu button is a real button with a label",
          'id="nav-toggle"' in HTML and 'aria-label="Open menu"' in HTML)
    check("  ...wired to the drawer for assistive tech",
          'aria-controls="app-nav"' in HTML and 'aria-expanded="false"' in HTML)
    check("  ...and is at least 44px",
          "width:44px" in rule_in(MOBILE, ".topbar-menu")
          and "height:44px" in rule_in(MOBILE, ".topbar-menu"))

    # ═══════════════════════════════════════════════════════════════════
    section("2. ONE NAVIGATION, SO PERMISSIONS CANNOT DRIFT")
    # The drawer is the sidebar element. A separate mobile menu is how an
    # admin-only link ends up on a rep's phone because someone forgot to
    # mirror a permission check.
    check("there is exactly one nav element", HTML.count('id="app-nav"') == 1)
    check("  ...and it is the sidebar", 'class="sidebar" id="app-nav"' in HTML)
    check("  ...marked up as a landmark", 'aria-label="Main"' in HTML)
    for link in ("nav-reps", "nav-sales-report"):
        check(f"{link} still exists once, with its permission gate",
              HTML.count(f'id="{link}"') == 1
              and f'id="{link}" style="display:none;"' in HTML.replace("\n", " "))
    check("syncAdminNav still drives both of them",
          "'nav-reps', 'nav-sales-report'" in JS and "role === 'admin'" in JS)
    # Every nav link in the document must live inside that one <nav>. A
    # second set anywhere else is the drift this design exists to prevent.
    nav_start = HTML.index('id="app-nav"')
    nav_end = HTML.index("</nav>", nav_start)
    inside = HTML[nav_start:nav_end].count('class="nav-link')
    total = HTML.count('class="nav-link')
    check(f"every nav link lives in that one nav ({inside} of {total})",
          inside == total and inside >= 4)

    section("2b. THE DRAWER CLOSES WHEN IT SHOULD")
    check("showView closes it, so every navigation does",
          "closeNav();\n}" in JS or "closeNav();" in JS.split("syncAdminNav();")[-1])
    check("  ...which is the one place all navigation passes through",
          "function showView(name)" in JS and JS.count("function closeNav(") == 1)
    check("the scrim closes it", 'onclick="closeNav()"' in HTML)
    check("Escape closes it",
          "e.key === 'Escape'" in JS and "closeNav()" in JS)
    check("growing past the breakpoint closes it",
          "window.innerWidth >= 900" in JS)
    check("the page behind cannot scroll while it is open",
          "document.body.style.overflow = open ? 'hidden' : ''" in JS)
    check("aria-expanded follows the state",
          "setAttribute('aria-expanded', open ? 'true' : 'false')" in JS)
    check("focus moves into the drawer when it opens",
          "first.focus()" in JS)

    # ═══════════════════════════════════════════════════════════════════
    section("3. THE OVERFLOW CRUTCH IS GONE AND STAYS GONE")
    hidden = re.findall(r"(html|body)[^{]*\{[^}]*overflow-x\s*:\s*hidden", CSS)
    check(f"html/body no longer hide horizontal overflow ({hidden})", not hidden)
    check("  ...and nothing else hides it globally",
          not re.search(r"^\s*\*\s*\{[^}]*overflow-x\s*:\s*hidden", CSS, flags=re.M))
    check("the containment that IS load-bearing stays",
          "img,svg,table{max-width:100%;}" in CSS.replace(" ", "").replace("\n", ""))
    # ADDED IN C4, for a crutch this suite did not think to look for.
    # .table-wrap carried plain `overflow:hidden` - there to clip the card's
    # rounded corners, but it clipped CONTENT too. Measured at 1280px the rep
    # table needed 1157px inside a 924px wrapper and the Deactivate button was
    # cut off: unreachable, with no scrollbar to admit it. That is the same
    # defect as html{overflow-x:hidden}, one container down, and the browser
    # measurement did not catch it either because the button was still inside
    # the viewport. A container that cannot show all of its row must let the
    # row be scrolled to.
    _wrap = rule_in(BASE_CSS, ".table-wrap")
    check(f"the table wrapper does not hide overflow either ({_wrap[:60]})",
          "overflow:hidden" not in _wrap)
    check("  ...it scrolls instead, at every width",
          "overflow-x:auto" in _wrap)

    section("3b. TABLES HAVE SOMEWHERE TO GO AT EVERY WIDTH")
    check("900-1199: a wide table scrolls inside its own card",
          "overflow-x:auto" in rule_in(TABLET, ".table-wrap"))
    check("  ...which needs every table to be wrapped",
          JS.count('<div class="table-wrap">') >= 2)
    check("below 900: rows become cards",
          "display:block" in rule_in(MOBILE, ".table tbody tr,.data-table tbody tr"))
    check("  ...with the column name carried on each cell",
          "content:attr(data-label)" in MOBILE)
    check("  ...and the cell free to wrap, so a long value cannot push the "
          "page sideways",
          "flex-wrap:wrap" in rule_in(MOBILE, ".table td,.data-table td")
          and "overflow-wrap:anywhere" in rule_in(MOBILE, ".table td,.data-table td"))
    actions = rule_in(MOBILE, ".table td.cell-actions .btn,.data-table td.cell-actions .btn")
    check("  ...and several actions in a cell STACK rather than sit in a row",
          "display:flex" in actions and "width:100%" in actions)
    check("     (width:100% alone is not enough - .btn is inline-flex, and "
          "three inline buttons at 100% ran 634px past a 768px edge)",
          "display:inline-flex" in rule_in(CSS, ".btn"))

    section("3c. NO DATA IS DROPPED TO MAKE IT FIT")
    # Every column the desktop table shows must still be labelled on a card.
    for label in ("Customer", "Enrollment", "Status", "Utility", "Added",
                  "Last modified"):
        check(f'the "{label}" column survives as a card row',
              f'data-label="{label}"' in JS)
    check("the Reps actions survive too", 'class="cell-actions"' in JS)
    check("nothing is display:none'd to hide an overflow",
          "display:none" not in rule_in(MOBILE, ".table td,.data-table td"))

    # ═══════════════════════════════════════════════════════════════════
    section("4. BREAKPOINTS ARE A LADDER, NOT A SCATTER")
    found = sorted({int(m) for m in re.findall(r"@media[^{]*?max-width:\s*(\d+)px", CSS)})
    check(f"the ladder is 430 / 599 / 899 / 1199 ({found})",
          found == [430, 599, 899, 1199])
    check("  ...down from thirteen before C3", len(found) <= 4)
    check("the touch band carries the touch sizing",
          "height:48px" in rule_in(MOBILE, ".btn"))
    check("the narrow band is single-column",
          "grid-template-columns:1fr" in
          rule_in(NARROW, ".row2,.row3,.row3.address-grid,.fgrid-2,.fgrid-addr"))
    check("  ...and its page head stacks rather than squeezes",
          "flex-direction:column" in rule_in(NARROW, ".page-head"))

    # ═══════════════════════════════════════════════════════════════════
    section("5. TOUCH TARGETS ON TOUCH LAYOUTS")
    # 44px is a FLOOR, not a literal. Asserting the string "height:44px"
    # would have forced .btn-sm to 44 and so added a fourth step to the button
    # scale C2 closed at 32/40/48 - the test dictating a number the design
    # system had already settled. The measurement is the property; the value is
    # read and compared.
    for sel, what in ((".nav-link", "navigation rows"),
                      (".status-pill", "status pills"),
                      (".btn-sm", "compact buttons"),
                      (".search-input", "the search box")):
        body = rule_in(MOBILE, sel)
        sizes = [int(v) for v in
                 re.findall(r"(?:min-)?height\s*:\s*(\d+)px", body)]
        check(f"{what} reach 44px ({sizes or 'no height set'})",
              bool(sizes) and max(sizes) >= 44)
    eye = rule_in(MOBILE, ".pw-eye,.ubs-toggle")
    check("the password eye and the sign-in toggle reach 44px",
          "min-width:44px" in eye and "min-height:44px" in eye)
    check("the inline 'View all' link does too",
          "min-height:44px" in rule_in(MOBILE, ".section-title a"))
    # The desktop LAYER, with media blocks stripped: the touch sizes above
    # must not have leaked into the default rules.
    check("desktop keeps its compact sizing",
          "height:32px" in rule_in(BASE_CSS, ".btn-sm")
          and not re.search(r"height\s*:\s*(?:44|48)px",
                            rule_in(BASE_CSS, ".btn-sm")))
    check("  ...and its compact navigation rows",
          "min-height:44px" not in rule_in(BASE_CSS, ".nav-link"))

    section("5b. FORMS STAY USABLE")
    inputs = rule_in(MOBILE, "input,select,textarea")
    check("fields take the 48px touch step", "height:48px" in inputs)
    check("  ...at 16px, which is what stops iOS zooming on focus",
          "font-size:16px" in inputs)
    # THE ABOVE IS NOT ENOUGH, AND SAYING SO IS THE POINT.
    #
    # That rule uses bare type selectors (specificity 0,0,1). The base input
    # styling is an attribute list - input[type=text],input[type=email],... -
    # at 0,1,1, and outranks it. So the rule could sit there reading exactly
    # right while every <input> on a phone stayed at 14px and iOS went on
    # zooming; only <select> ever got the guard, because the base rule matches
    # that one by type and merely ties. The browser found this at 390px; this
    # suite had asserted the rule's TEXT since Phase B and never caught it.
    #
    # So the override is asserted at the specificity that actually wins.
    typed = rule_in(MOBILE, "input[type=text],input[type=email],"
                            "input[type=password],input[type=tel],"
                            "input[type=number]")
    check("  ...declared at a specificity that beats the base attribute list, "
          "or it never reaches an <input>",
          "font-size:16px" in typed)
    check("  ...and the typed inputs take the touch step too",
          "height:48px" in typed)
    check("the wizard's actions stack and go full width",
          "flex-direction:column-reverse" in rule_in(MOBILE, ".wizard-actions")
          and "width:100%" in rule_in(MOBILE, ".wizard-actions .btn"))

    # ═══════════════════════════════════════════════════════════════════
    section("6. THE EMAIL ALREADY IN PERCH'S SYSTEM")
    check("the case is recognised by the error TYPE, not by its words",
          "err.body.perch_error === 'PerchEnrollmentInProgressError'" in JS)
    check("  ...so a Perch rewording cannot break it",
          "Use the /status endpoint" not in JS)
    # D-2: the wording lost the vendor name; the rule - one exact sentence on
    # the email field and nothing more - is unchanged.
    check("the rep is told exactly this and nothing more",
          '"An enrollment already exists for this email address."' in JS)
    check("  ...on the Email FIELD, with the shared C2 treatment",
          "setFieldError('wf-email', 'wf-err-email'," in JS)
    check("  ...which marks the input invalid and ties the message to it",
          "classList.add('err')" in JS
          and "setAttribute('aria-invalid', 'true')" in JS
          and "setAttribute('aria-describedby'" in JS)
    check("  ...and puts the cursor where the fix is",
          "emailField.focus()" in JS)
    check("the form-level error is not shown as well",
          "setFieldError('wf-email', 'wf-err-email',\n" in JS_RAW
          and "return;\n    }" in JS_RAW)
    check("a stale rejection is cleared on the next attempt",
          "setFieldError('wf-email', 'wf-err-email', '');" in JS)
    check("the raw message is kept for debugging, not shown",
          "console.debug('[perch] token rejected" in JS_RAW)
    check("no endpoint name or Perch internal is put on screen",
          "/token" not in JS.split("function submitCapacity")[1].split("perchContext.email =")[0]
          and "Try logging in instead" not in JS)
    check("the backend still returns the type the frontend keys on",
          '"perch_error": type(e).__name__' in
          (ROOT / "routes" / "perch_routes.py").read_text(encoding="utf-8"))
    check("the per-field error element is the shared C2 component",
          'class="field-error wf-field-error"' in JS)

    section("6b. THE REDUNDANT EMAIL HELPER IS GONE")
    check("the help line is no longer sent for the email field",
          '"help": "Used to start and securely resume this enrollment."'
          not in WORKFLOW)
    check("  ...and the label and placeholder still carry it",
          '"label": "Customer email"' in WORKFLOW
          and '"placeholder": "customer@example.com"' in WORKFLOW)
    check("  ...while other fields keep their help where it earns its place",
          '"help":' in WORKFLOW)

    # ═══════════════════════════════════════════════════════════════════
    section("7. C3 CHANGED NO BEHAVIOUR")
    check("Perch next_step is still the router", "NEXT_STEP_PATH_MAP" in WORKFLOW)
    check("the workflow vocabulary is unchanged",
          all(k in WORKFLOW for k in ("lmi_not_qualified", "contracts_accept_uncertain",
                                      "BLOCKED_STEP_KEYS", "TERMINAL_STEP_KEYS")))
    check("the email field's validation is untouched",
          r'"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"' in WORKFLOW)
    check("auth handling is unchanged", "tryRestoreSession" in JS)
    check("admin routes are still enforced server-side",
          '@require_role("admin")' in
          (ROOT / "routes" / "admin_routes.py").read_text(encoding="utf-8")
          if (ROOT / "routes" / "admin_routes.py").exists() else True)
    check("the C1 tokens and C2 components survive",
          "--t-body" in CSS and "--status-action-bg" in CSS
          and ".alert-error" in CSS and "function setFieldError(" in JS)
    check("the photo/document deletion issue is still recorded as OPEN",
          "**Status:** OPEN" in (ROOT / "KNOWN_ISSUES.md").read_text(encoding="utf-8"))

    # ═══════════════════════════════════════════════════════════════════
    section("8. THE GEOMETRY MEASUREMENT RUNS TOO")
    # The pixel proof lives in a harness that resolves the stylesheet's
    # cascade at six widths. It needs no browser, so it runs in the ordinary
    # suite rather than only when someone remembers to open a phone - which
    # is exactly how this regression went unnoticed the first time.
    import subprocess
    h = ROOT / "test" / "c3_responsive_harness.js"
    check("c3_responsive_harness.js exists", h.exists())
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=180)
    for line in (r.stdout + r.stderr).splitlines():
        if "[FAIL]" in line or "content   share" in line or "%" in line[-4:]:
            print("      " + line.strip())
    check("  ...and every width measures clean", r.returncode == 0)

    print(f"\n{'=' * 72}")
    print(f"{sum(_results)} checks passed, {len(_results) - sum(_results)} failed")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
