"""
The POD ID must survive everything a rep does to an enrollment.

THE ROOT CAUSE THIS PINS

    NYSEG and RG&E require a POD ID, and it exists in exactly one place a rep
    can get it from: the bill. Reps reported it disappearing.

    The browser sent `secondary_account_identifier` on EVERY save, blank
    included - progress saves, the Continue payload, background saves made
    from a screen where the field was not even visible because the utility
    rules had not loaded yet. The PATCH handler wrote every key it was given.
    So any save made while that input happened to be empty wrote NULL over a
    stored POD, and the rep had to find the bill again.

    It is the same shape as the masked-account-number defect found in Phase D:
    a value the CLIENT did not really have, overwriting one the SERVER did.
    The fix has the same two halves, and this suite asserts the SERVER half
    directly - because that is the half that holds no matter which client, or
    which version of the client, is talking to it.

WHAT IS ASSERTED

    A POD changes only when the rep explicitly edits it. Every other path -
    a save that omits it, a save that carries a blank, a reopen, a step
    change, a bill removed or replaced, an unrelated PATCH - leaves it alone.
    And the format rules still refuse a bad one, for both utilities, because
    a field that cannot be erased is no use if it can be corrupted instead.

Run: python test/test_pod_persistence.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ubs_test_db  # noqa: F401  - throwaway database; see test/_ubs_test_db.py

os.environ.setdefault("PERCH_API_MODE", "mock")

# UPLOADS GO SOMEWHERE THIS SUITE OWNS.
#
# Several document suites repoint routes.document_routes.UPLOAD_DIR and
# DATA_ROOT at their own TemporaryDirectory when they are imported, and never
# put them back. Run alone, this suite inherits the real defaults and passes;
# run after one of those, it writes into a directory that has since been
# cleaned up, and /enroll then refuses with "the exact saved utility-bill
# document is required" - a failure about the fixture, not the product.
#
# Claiming a directory here is what the other document suites already do, and
# it makes the order tests run in stop mattering for this one.
import tempfile as _tempfile

from routes import document_routes as _document_routes
import helpers as _helpers

_uploads = _tempfile.TemporaryDirectory(prefix="ubs-pod-")
_document_routes.DATA_ROOT = _uploads.name
_document_routes.UPLOAD_DIR = os.path.join(_uploads.name, "uploads")
_helpers.DATA_ROOT = _uploads.name
os.makedirs(_document_routes.UPLOAD_DIR, exist_ok=True)

from app import app
from db import init_db, query_one
import seed
from services.perch import adapter, utilities

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()

NYSEG = "nyseg"
RGE = "rochester-gas-electric"
POD = {NYSEG: "N01000000000001", RGE: "R01000000000002"}
POD_CORRECTED = {NYSEG: "N01000000000099", RGE: "R01000000000099"}
ACCOUNT = "90000000011"          # 11 digits, which both utilities use


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(f"Failed: {label}")


def main():
    init_db(reset=True)
    seed.seed()
    c = app.test_client()
    rep = {"Authorization": "Bearer " + c.post(
        "/api/auth/signin", json={"email": "charlie@daltonsolar.com",
                                  "password": "RepPass1!"}).get_json()["token"]}

    seq = [0]

    def stored_pod(eid):
        """The POD as DB truth, not as any payload renders it."""
        row = query_one(
            "SELECT ua.secondary_account_identifier AS pod FROM utility_accounts ua "
            "JOIN enrollments e ON e.utility_account_id = ua.id WHERE e.id = ?",
            (eid,))
        return row["pod"] if row else None

    def shown_pod(eid):
        """The POD as the wizard hydrates it on reopen."""
        detail = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
        return (detail.get("utility_account") or {}).get(
            "secondary_account_identifier")

    def new_enrollment(utility, pod=None):
        """A draft with a utility account, created the way the wizard does."""
        seq[0] += 1
        tag = f"pod{seq[0]}"
        eid = c.post("/api/enrollments", headers=rep, json={}).get_json()["id"]
        acct = {"utility_name": utility, "account_number": ACCOUNT}
        if pod:
            acct["secondary_account_identifier"] = pod
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "customer": {"first_name": "Dana", "last_name": "Reyes",
                         "email": f"{tag}@example.com", "phone": "6075550142"},
            "service_address": {"street": "742 Example Way", "city": "Ithaca",
                                "state": "NY", "zip": "14850"},
            "utility_account": acct})
        return eid

    for utility in (NYSEG, RGE):
        name = "NYSEG" if utility == NYSEG else "RG&E"
        pod, corrected = POD[utility], POD_CORRECTED[utility]

        # ════════════════════════════════════════════════════════════════════
        section(f"{name}: A POD THE REP ENTERED SURVIVES EVERY SAVE")
        # ════════════════════════════════════════════════════════════════════
        eid = new_enrollment(utility, pod)
        check(f"it is stored as entered ({stored_pod(eid)})", stored_pod(eid) == pod)
        check("  ...and comes back on reopen", shown_pod(eid) == pod)

        # THE DEFECT ITSELF: a save carrying a blank POD.
        r = c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "utility_account": {"utility_name": utility,
                                "account_number": ACCOUNT,
                                "secondary_account_identifier": ""}})
        check(f"a save carrying a BLANK POD is accepted ({r.status_code})",
              r.status_code == 200)
        check("  ...AND THE STORED POD SURVIVES IT", stored_pod(eid) == pod)

        r = c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "utility_account": {"utility_name": utility,
                                "account_number": ACCOUNT,
                                "secondary_account_identifier": None}})
        check("a save carrying a NULL POD leaves it alone too",
              r.status_code == 200 and stored_pod(eid) == pod)

        r = c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "utility_account": {"utility_name": utility,
                                "account_number": ACCOUNT,
                                "secondary_account_identifier": "   "}})
        check("  ...as does one carrying only whitespace",
              r.status_code == 200 and stored_pod(eid) == pod)

        # An unrelated PATCH that never mentions the field.
        c.patch(f"/api/enrollments/{eid}", headers=rep,
                json={"customer": {"phone": "6075550199"}})
        check("an unrelated PATCH does not touch it", stored_pod(eid) == pod)
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "service_address": {"street": "9 Other St", "city": "Ithaca",
                                "state": "NY", "zip": "14850"}})
        check("  ...nor does an address change", stored_pod(eid) == pod)
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "utility_account": {"meter_number": "8000000001"}})
        check("  ...nor a save of a different account field", stored_pod(eid) == pod)

        # ════════════════════════════════════════════════════════════════════
        section(f"{name}: SAVE & EXIT, REOPEN, AND BACK AGAIN")
        # ════════════════════════════════════════════════════════════════════
        # The exact loop that destroyed the account number in Phase D: read the
        # payload the rep is served, send it back unchanged, read it again.
        for round_trip in range(3):
            detail = c.get(f"/api/enrollments/{eid}", headers=rep).get_json()
            ua = detail.get("utility_account") or {}
            c.patch(f"/api/enrollments/{eid}", headers=rep, json={
                "utility_account": {
                    "utility_name": ua.get("utility_name"),
                    "account_number": ua.get("account_number"),
                    "secondary_account_identifier": ua.get(
                        "secondary_account_identifier")}})
            check(f"  round trip {round_trip + 1}: the POD is intact",
                  stored_pod(eid) == pod)
        check("and the account number survived the same loop",
              query_one("SELECT ua.account_number AS a FROM utility_accounts ua "
                        "JOIN enrollments e ON e.utility_account_id = ua.id "
                        "WHERE e.id = ?", (eid,))["a"] == ACCOUNT)

        # ════════════════════════════════════════════════════════════════════
        section(f"{name}: A REAL EDIT STILL WRITES")
        # ════════════════════════════════════════════════════════════════════
        r = c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "utility_account": {"secondary_account_identifier": corrected}})
        check(f"a corrected POD is written ({stored_pod(eid)})",
              r.status_code == 200 and stored_pod(eid) == corrected)
        check("  ...and is what reopen shows", shown_pod(eid) == corrected)
        c.patch(f"/api/enrollments/{eid}", headers=rep, json={
            "utility_account": {"secondary_account_identifier": ""}})
        check("  ...and a later blank cannot undo the correction",
              stored_pod(eid) == corrected)

        # ════════════════════════════════════════════════════════════════════
        section(f"{name}: THE BILL CAN COME AND GO")
        # ════════════════════════════════════════════════════════════════════
        def upload_bill(target, tag):
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (600, 800), (240, 244, 250)).save(buf, "PNG")
            buf.seek(0)
            r = c.post(f"/api/enrollments/{target}/document-sets", headers=rep,
                       data={"category": "utility_bill",
                             "files": (buf, f"bill-{tag}.png")},
                       content_type="multipart/form-data")
            assert r.status_code == 200, r.data
            return r.get_json()["files"][0]["document_id"]

        doc1 = upload_bill(eid, "first")
        check("uploading a bill leaves the POD alone", stored_pod(eid) == corrected)
        doc2 = upload_bill(eid, "second")
        check("  ...and so does a second bill", stored_pod(eid) == corrected)
        rm = c.delete(f"/api/enrollments/{eid}/documents/{doc1}", headers=rep)
        check(f"removing a bill leaves it alone ({rm.status_code})",
              rm.status_code == 200 and stored_pod(eid) == corrected)
        rm2 = c.delete(f"/api/enrollments/{eid}/documents/{doc2}", headers=rep)
        check("  ...and so does removing the last one",
              rm2.status_code == 200 and stored_pod(eid) == corrected)

        # ════════════════════════════════════════════════════════════════════
        section(f"{name}: THE FORMAT RULES STILL BITE")
        # ════════════════════════════════════════════════════════════════════
        other = POD[RGE] if utility == NYSEG else POD[NYSEG]
        prefix = utilities.pod_id_rule(utility)["prefix"]
        check(f"a POD with another utility's prefix is refused ({other[:3]})",
              bool(utilities.validate_pod_id(utility, other)))
        check(f"  ...the message names this utility's prefix ({prefix})",
              prefix in utilities.validate_pod_id(utility, other))
        check("a POD of the wrong length is refused",
              bool(utilities.validate_pod_id(utility, prefix + "123")))
        check("an empty POD is refused for a utility that requires one",
              bool(utilities.validate_pod_id(utility, "")))
        check("  ...and the correct one passes",
              utilities.validate_pod_id(utility, pod) is None)

    # ════════════════════════════════════════════════════════════════════════
    section("A BAD POD STILL BLOCKS THE ENROLLMENT")
    # ════════════════════════════════════════════════════════════════════════
    # NYSEG has a capacity fixture, so this leg runs end to end. The adapter's
    # pre-flight is the layer that must refuse - no spy.
    eid = c.post("/api/perch/enrollments/capacity", headers=rep,
                 json={"email": "pod.block@example.com", "zip_code": "12901",
                       "utility_name": NYSEG}).get_json()["enrollment_id"]
    c.patch(f"/api/enrollments/{eid}", headers=rep, json={
        "customer": {"first_name": "Dana", "last_name": "Reyes",
                     "email": "pod.block@example.com", "phone": "6075550142"},
        "service_address": {"street": "742 Example Way", "city": "Plattsburgh",
                            "state": "NY", "zip": "12901"},
        "billing_address": {"same_as_service": True},
        "utility_account": {"utility_name": NYSEG, "account_number": ACCOUNT,
                            "secondary_account_identifier": POD[RGE]}})
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 800), (240, 244, 250)).save(buf, "PNG")
    buf.seek(0)
    doc = c.post(f"/api/enrollments/{eid}/document-sets", headers=rep,
                 data={"category": "utility_bill", "files": (buf, "bill.png")},
                 content_type="multipart/form-data").get_json()["files"][0]["document_id"]
    r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=rep,
               json={"document_id": doc})
    body = r.get_json() or {}
    check(f"an enrollment with the wrong POD prefix is refused ({r.status_code})",
          r.status_code >= 400)
    check(f"  ...with a message a rep can act on: {body.get('error')!r}",
          "POD" in (body.get("error") or ""))
    check("  ...pointing at the POD field", body.get("field") == "c-pod")
    check("  ...and the rejected value is still stored, not wiped",
          stored_pod(eid) == POD[RGE])

    # And the same enrollment goes through once the POD is corrected.
    c.patch(f"/api/enrollments/{eid}", headers=rep, json={
        "utility_account": {"secondary_account_identifier": POD[NYSEG]}})
    captured = {}
    real = adapter.create_enrollment

    def spy(enrollment_id, payload, user_id=None):
        captured["pod"] = (payload["utility_accounts"][0]
                           .get("secondary_account_identifier"))
        return {"customer_type": "Residential",
                "next_step_url": "https://example.invalid/contracts"}

    adapter.create_enrollment = spy
    try:
        r = c.post(f"/api/perch/enrollments/{eid}/enroll", headers=rep,
                   json={"document_id": doc})
    finally:
        adapter.create_enrollment = real
    check(f"a corrected POD enrolls ({r.status_code})", r.status_code == 200)
    check(f"  ...and reaches the boundary exactly as stored ({captured.get('pod')})",
          captured.get("pod") == POD[NYSEG])
    check("  ...and Continue did not disturb what is stored",
          stored_pod(eid) == POD[NYSEG])

    # ════════════════════════════════════════════════════════════════════════
    section("THE BROWSER'S HALF OF THE SAME RULE")
    # ════════════════════════════════════════════════════════════════════════
    # Runtime behaviour is proven in test/pod_persistence_harness.js. These are
    # the structural facts that keep the client from re-creating the defect.
    check("a progress save sends the POD only when there is one to send",
          "if(podProgress) payload.utility_account.secondary_account_identifier"
          in JS)
    check("  ...and the Continue payload follows the same rule",
          "? {secondary_account_identifier:(state.customer.podId || '').trim()} : {}"
          in JS)
    check("reopen hydrates the stored POD back into the form",
          "state.customer.podId = e.utility_account.secondary_account_identifier"
          in JS)
    check("  ...and the form is painted from it",
          "document.getElementById('c-pod').value = state.customer.podId" in JS)
    check("readiness still turns on the POD rule",
          "const podOk = podLooksValid(rule, document.getElementById('c-pod').value);"
          in JS)

    # ════════════════════════════════════════════════════════════════════════
    section("THE WRITE THAT WAS IGNORED IS RECORDED")
    # ════════════════════════════════════════════════════════════════════════
    # Silently keeping a value is right, but it must be visible to whoever has
    # to explain later why a save did not change anything.
    rows = query_one(
        "SELECT COUNT(*) AS n FROM audit_logs "
        "WHERE action = 'empty_value_ignored_on_write'")
    check(f"the ignored blank writes were audited ({rows['n']})", rows["n"] > 0)
    sample = query_one(
        "SELECT details_json FROM audit_logs "
        "WHERE action = 'empty_value_ignored_on_write' ORDER BY id LIMIT 1")
    check("  ...naming the field, and carrying no customer value",
          "secondary_account_identifier" in (sample["details_json"] or "")
          and POD[NYSEG] not in (sample["details_json"] or ""))

    print("\n" + "=" * 72)
    print("POD ID PERSISTENCE - ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
