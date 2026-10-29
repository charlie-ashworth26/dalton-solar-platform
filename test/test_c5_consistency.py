"""
UBS 2.0 C5 - THE CONSISTENCY PASS.

WHAT THIS EXISTS TO PREVENT RETURNING.

    By the end of C4 the application was well built screen by screen and
    inconsistent across them. The same instant was written three ways
    ("Oct 28, 2026 at 12:04 AM", a date over a time, and "10/28/2026,
    12:04:00 AM" on the one screen an admin exports from). The same enrollment
    status was a semantic pill on the dashboard and bare wrapped text on the
    Sales Report. The same act - attaching a document - used two different
    upload components depending on which step you were standing in. The same
    row was built twice, once per list, and the two had already drifted.

    And a whole class of value could only be read by hovering: measured at
    1024px with touch - table layout AND a tablet - 144 of 288 cells on
    Customers were clamped to one line with their full text in a title
    attribute, which a finger cannot reach.

This file is the source-level ratchet for the properties C5 established. The
pixel proof lives in the browser harnesses (scratch verify-c5.js and
touch1024.js); these are the ones that, if reverted, would bring the drift
back without anyone noticing.

    python test/test_c5_consistency.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
ADMIN = open(os.path.join(ROOT, "routes", "admin_routes.py"), encoding="utf-8").read()

FLAT_CSS = re.sub(r"\s+", "", CSS)
# Comments explain what was removed and necessarily quote it, so a check for
# "this string is gone" must look at the CODE, not at the prose about it.
JS_CODE = re.sub(r"/\*[\s\S]*?\*/", "", JS)
JS_CODE = re.sub(r"^\s*//.*$", "", JS_CODE, flags=re.M)
_passed = 0
_failed = []


def check(label, cond):
    global _passed
    if cond:
        _passed += 1
        print(f"  [PASS] {label}")
    else:
        _failed.append(label)
        print(f"  [FAIL] {label}")


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def hover_none_block():
    """The @media (hover: none) block, body only."""
    m = re.search(r"@media\s*\(\s*hover:\s*none\s*\)\s*\{", CSS)
    if not m:
        return ""
    i, depth = m.end(), 1
    while i < len(CSS) and depth:
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
        i += 1
    return CSS[m.end():i - 1]


def main():
    # ═══════════════════════════════════════════════════════════════════
    section("1. ONE DATE STRATEGY")
    # The report had its own formatter calling toLocaleString(), which renders
    # a different order, a different separator and seconds nobody asked for.
    body = JS[JS.index("function formatReportDate("):]
    body = body[:body.index("\n}") + 2]
    check("the report's date formatter defers to the shared one",
          "formatTimestamp(" in body)
    check("  ...and no longer renders a locale string of its own",
          "d.toLocaleString()" not in body)
    check("every table cell timestamp goes through one helper",
          JS.count("function timestampCellHtml(") == 1)
    check("  ...and the enrollment lists, Customers and the report all use it",
          JS.count("timestampCellHtml(") >= 5)

    # ═══════════════════════════════════════════════════════════════════
    section("2. ONE STATUS TREATMENT")
    report = JS[JS.index("async function loadSalesReport("):]
    report = report[:report.index("\n}") + 2]
    check("the Sales Report renders the shared status pill",
          "status-pill" in report and "statusFamilyFor(r)" in report)
    check("  ...labelled by the shared resolver", "statusShortLabel(r)" in report)
    # C1's rule: a colour is never chosen by reading a label.
    check("  ...and never by parsing the label text",
          not re.search(r"status\s*\.\s*(includes|match|indexOf|toLowerCase)", report))
    check("the key behind the label is serialized for it",
          '"workflow_step_key": step_key' in ADMIN)
    check("  ...without disturbing the label the CSV exports",
          '"status": status,' in ADMIN)

    # ═══════════════════════════════════════════════════════════════════
    section("3. ONE UPLOAD COMPONENT")
    lmi = HTML[HTML.index('id="step-lmi"'):HTML.index('id="step-agreement"')]
    check("the eligibility document uses the same drop target as the bill",
          'class="upload" id="lmi-dropzone"' in lmi)
    check("  ...with the same icon, headline and sub copy",
          "up-ico" in lmi and "<strong>Drop the document here" in lmi)
    check("  ...and no legacy .dropzone survives on the step",
          'class="dropzone"' not in lmi)
    check("both chips come from one builder",
          JS.count("function uploadChipHtml(") == 1
          and "uploadChipHtml(name, status, 'removeBill()')" in JS
          and "uploadChipHtml(name, status, 'removeLmi()')" in JS)
    # THE DELETION DEFECT IS STILL OPEN. C5 unified how the states LOOK and
    # deliberately did not touch the removal path.
    check("the open deletion defect is still recorded as open",
          "REMOVAL IS NOT FIXED HERE" in JS)

    # ═══════════════════════════════════════════════════════════════════
    section("4. NOTHING IS READABLE ONLY BY HOVERING")
    hv = re.sub(r"\s+", "", hover_none_block())
    check("there is a hover-free rule at all", bool(hv))
    for sel in ("td.cell-cust", "td.cell-util", "td.cell-code", "td.cell-date",
                "#reps-table td", "#sales-report-table td"):
        check(f"  ...covering {sel}", sel.replace(" ", "") in hv)
    check("  ...the mono span inside a code cell too", "td.mono" in hv)
    check("  ...and it lets them wrap rather than clip",
          "white-space:normal" in hv and "text-overflow:clip" in hv)
    # Specificity, the reason the first attempt at this silently lost: the
    # block must come AFTER the column rules it has to beat.
    check("  ...and it is the last word in the stylesheet",
          CSS.index("@media (hover: none)") > CSS.index("#reps-table .table-wrap"))

    # ═══════════════════════════════════════════════════════════════════
    section("5. ONE ROW BUILDER FOR THE ENROLLMENT LISTS")
    check("Customers renders through the shared builder",
          "sorted.map(enrollmentRowHtml).join('')" in JS)
    check("  ...so neither list hand-builds a customer cell",
          JS.count('data-label="Customer" class="cell-cust"') == 1)
    check("  ...and the empty state fits what is actually empty",
          "'No customers yet'" in JS and "'Nothing matches that search'" in JS)

    # ═══════════════════════════════════════════════════════════════════
    section("6. THE CUSTOMER'S OWN PAGE")
    loader = JS[JS.index("async function loadCustomerAgreement("):]
    loader = loader[:loader.index("\n}") + 2]
    check("the portal writes the values it fetches",
          "portal-utility" in loader and "portal-address" in loader)
    check("  ...into elements that exist",
          all(f'id="{i}"' in HTML for i in
              ("portal-utility", "portal-address", "portal-status")))
    # SAVINGS IS GONE FROM THIS PAGE, on purpose and by request. The row this
    # suite originally required rendered "[object Object] off your electricity
    # supply" against a real payload: program_savings is a structure that the
    # rep-facing views pass through formatSavings(), and the portal
    # concatenated it raw. The check is inverted rather than deleted so the
    # row cannot quietly come back.
    loader_code = JS_CODE[JS_CODE.index("async function loadCustomerAgreement("):]
    loader_code = loader_code[:loader_code.index("\n}") + 2]
    check("  ...and savings is not among them",
          "portal-savings" not in HTML and "program_savings" not in loader_code)
    # The guard that makes the whole class of defect impossible here: a row
    # takes a string or it hides.
    check("a row renders a string or nothing, never a stringified object",
          "(typeof text === 'string') ? text.trim() : ''" in loader)
    check("the section is headed Your agreement",
          '<div class="review-head">Your agreement</div>' in HTML)
    check("  ...and no longer into #portal-project, which never did",
          "getElementById('portal-project')" not in JS_CODE)
    check("the status is the shared pill, not a hardcoded gold badge",
          "statusFamilyFor(me)" in loader and "badge-gold" not in HTML)
    check("  ...worded for a customer", "CUSTOMER_STATUS_LABEL" in JS)
    # A row with no value is hidden - and the attribute has to actually win
    # against display:flex, which is how it failed the first time.
    check("an absent value hides its row",
          ".review-line[hidden]{display:none;}" in FLAT_CSS)
    portal = HTML[HTML.index('id="screen-customer-portal"'):]
    portal = portal[:portal.index("</div>\n</div>")] if "</div>\n</div>" in portal else portal[:4000]
    check("  ...and the page says nothing about solar",
          "solar" not in portal.lower())

    # ═══════════════════════════════════════════════════════════════════
    section("7. ONE NAME, ONE PLACE, FOR THE ELIGIBILITY STEP")
    check("the step's copy has a single source", "const LMI_COPY = {" in JS)
    check("  ...applied through one function", JS.count("function applyLmiCopy(") == 1)
    check("  ...and nothing writes that heading by hand",
          "lmiTitle.textContent='LMI documentation'" not in JS.replace(" ", ""))
    check("the step is called what the stepper calls it",
          "title: 'Eligibility'" in JS and '<h2 id="lmi-title">Eligibility</h2>' in HTML)
    check("  ...and the resumed wording names no vendor",
          "Perch requires proof documentation" not in JS_CODE)

    # ═══════════════════════════════════════════════════════════════════
    section("8. THE WIZARD'S FIELDS SHARE ONE GRID")
    check("the descriptor-driven fields are wrapped in a grid",
          "'<div class=\"wf-fields\">'" in JS)
    check("  ...of the same twelve columns Customer & Bill uses",
          ".wf-fields{display:grid;grid-template-columns:repeat(12,minmax(0,1fr))"
          in FLAT_CSS)
    check("  ...a field takes the full row unless the step says otherwise",
          ".wf-fields>.field{grid-column:1/-1" in FLAT_CSS)
    check("  ...and the width hint is a presentation hint, like placeholder",
          "f.width === 'half'" in JS)

    print("\n" + "=" * 72)
    print(f"{_passed} passed, {len(_failed)} failed")
    print("=" * 72)
    if _failed:
        raise AssertionError(_failed[0])


if __name__ == "__main__":
    main()
