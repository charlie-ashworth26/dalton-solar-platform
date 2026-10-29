"""
pytest entry point.

Each suite is a self-contained validation script that can also be run directly
(`python test/test_perch_milestone2.py`). These wrappers let `python -m pytest`
run the same suites without duplicating their assertions.

Each suite resets the database at the start, so they are safe to run in
sequence but must NOT be run in parallel (-p no:randomly / no -n).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("PERCH_API_MODE", "mock")

#: Suites that assert the **v1** Perch contract on purpose.
#:
#: Running the regression with PERCH_API_VERSION=v2 makes every one of them
#: fail, and that is the correct result: they pin behaviour v2 deliberately
#: changed. They are NOT product failures and must never be "fixed" by editing
#: production code to satisfy them under v2.
#:
#: Named here so the distinction is recorded in the suite registry itself,
#: rather than living in a report someone has to remember. A v2 run is read as
#: "these and only these may fail"; any other failure is real.
V1_CONTRACT_SUITES = (
    "test_perch_milestone2",
    "test_program_selection",
    "test_post_b2_followups",
    "test_program_persistence",
    "test_contract_branch_routing",
    "test_commit_boundary",
    "test_savings_and_utility",
    # Both assert the v1 FLAT capacity shape and the pre-E2.1 savings
    # behaviour: under v2 a program with no published rate of its own is left
    # without one ("rates left empty rather than substituted"), which is the
    # E2.1 decision not to invent a rate Perch did not publish.
    "test_lmi_dual_state",
    "test_review_consistency",
)


def test_perch_milestone2():
    """Perch documented-contract suite (auth, capacity, workflow, renderer)."""
    import test_perch_milestone2 as suite
    suite.main()


def test_phase1_end_to_end():
    """Pre-Perch enrollment lifecycle: draft -> QA -> submission -> developer."""
    import e2e_scenario as suite
    suite.main()


def test_phase2_frontend_contract():
    """Frontend/API contract for the served application."""
    import verify_frontend_integration as suite
    suite.main()


def test_phase4a_rep_visibility():
    """Phase 4A: rep enrollment visibility and resume."""
    import test_phase4a_rep_visibility as suite
    suite.main()


def test_stabilization_multi_enrollment():
    """Stabilization: multi-enrollment session state lifecycle."""
    import test_stabilization_multi_enrollment as suite
    suite.main()


def test_response_hardening():
    """Perch responses are never blindly assumed to be JSON."""
    import test_response_hardening as suite
    suite.main()


def test_config_startup():
    """Configuration loading and startup reporting."""
    import test_config_startup as suite
    suite.main()


def test_customer_auth_and_completed_view():
    """Completed-enrollment view, customer auth, password toggle, metadata."""
    import test_customer_auth_and_completed_view as suite
    suite.main()


def test_contract_ux_redesign():
    """Redesigned shared contract UX; legacy engine removed."""
    import test_contract_ux_redesign as suite
    suite.main()



def test_account_mask_write_guard():
    """A display mask is never written back as the account number."""
    import test_account_mask_write_guard as suite
    suite.main()


def test_lmi_multifile_proof():
    """Every selected LMI proof file is uploaded, persisted and submitted."""
    import test_lmi_multifile_proof as suite
    suite.main()


def test_document_removal_persistence():
    """Removing or replacing a saved bill/proof document PERSISTS (KNOWN_ISSUES #1)."""
    import test_document_removal_persistence as suite
    suite.main()


def test_document_ingestion():
    """Document ingestion + OCR robustness."""
    import test_document_ingestion as suite
    suite.main()


def test_enrollment_ownership():
    """Rep identity + enrollment ownership enforcement."""
    import test_enrollment_ownership as suite
    suite.main()


def test_admin_rep_management():
    """Admin rep account management."""
    import test_admin_rep_management as suite
    suite.main()


def test_hosting_readiness():
    """Render staging hosting readiness."""
    import test_hosting_readiness as suite
    suite.main()


def test_phase_a_ui_data():
    """Phase A UI/data correctness + non-LMI branch."""
    import test_phase_a_ui_data as suite
    suite.main()


def test_program_selection():
    """Program availability + explicit customer type selection."""
    import test_program_selection as suite
    suite.main()


def test_post_b2_followups():
    """Enrollment lifecycle, small-CS mapping, capacity copy, timestamps."""
    import test_post_b2_followups as suite
    suite.main()


def test_program_persistence():
    """Dual-program selection persists across navigation and resume."""
    import test_program_persistence as suite
    suite.main()


def test_contract_branch_routing():
    """Persisted program choice governs the Perch enrollment and contracts."""
    import test_contract_branch_routing as suite
    suite.main()


def test_market_rules_signing():
    """Phase 1: HMAC raw canonical-query signing + market-rules client."""
    import test_market_rules_signing as suite
    suite.main()


def test_market_rules_service():
    """Phase 2: market-rules cache, NY fallback, and the read-only endpoints."""
    import test_market_rules_service as suite
    suite.main()


def test_virginia_enrollment():
    """Phase 3: VA state, market-driven fields/counties/status, income hard stop."""
    import test_virginia_enrollment as suite
    suite.main()


def test_availability_utilities():
    """Availability step: ZIP -> state -> market-driven utility choices."""
    import test_availability_utilities as suite
    suite.main()


def test_phase_a_reliability():
    """UBS 2.0 Phase A: enrollment hydration/persistence, admin nav, report SQL."""
    import test_phase_a_reliability as suite
    suite.main()


def test_phase_b_experience():
    """UBS 2.0 Phase B: deep links, the read-only enrollment experience, the
    customer's completed view, and one utility display name."""
    import test_phase_b_experience as suite
    suite.main()


def test_phase_c0_defects():
    """UBS 2.0 Phase C0: contract-name strikethrough, the unauthorised Sales
    Report state, and disabled-button contrast."""
    import test_phase_c0_defects as suite
    suite.main()


def test_db_safety():
    """UBS 2.0 C0.5: the application database cannot be erased by a test
    run, a seed, a startup, or a deployment."""
    import test_db_safety as suite
    suite.main()


def test_db_backup():
    """UBS 2.0 C0.6: verified backups that never write to the source, a
    retention policy, and a restore that cannot overwrite a live database."""
    import test_db_backup as suite
    suite.main()


def test_c1_design_system():
    """UBS 2.0 C1: the type scale, the spacing scale, the four status
    families, and the colours that carry text."""
    import test_c1_design_system as suite
    suite.main()


def test_c2_components():
    """UBS 2.0 C2: the shared buttons, surfaces, fields, alerts, table
    shell and upload chip."""
    import test_c2_components as suite
    suite.main()


def test_c3_responsive():
    """UBS 2.0 C3: the mobile shell, the breakpoint ladder, the removal of
    the overflow crutch, touch targets, and the Perch email-exists error."""
    import test_c3_responsive as suite
    suite.main()


def test_c5_consistency():
    """UBS 2.0 C5: one date strategy, one status treatment, one upload
    component, one row builder, and no value readable only by hovering."""
    import test_c5_consistency as suite
    suite.main()


def test_e1_v2_transport():
    """UBS 2.0 E1: Perch v2 base URLs and version selection, next_step
    normalization across v1 URLs and v2 bare paths, and the v1/v2/422 error
    envelopes with no machine code reaching a rep."""
    import test_e1_v2_transport as suite
    suite.main()


def test_e2_capacity_model():
    """UBS 2.0 E2: the v2 entry-shaped capacity model, the five capacity types
    mapped to UBS programs, county restrictions, savings-rate arrays, LMI vs
    LMI IRA, and the committed program selection that cannot switch."""
    import test_e2_capacity_model as suite
    suite.main()


def test_self_attestation():
    """LMI self-attestation: the rep-side form, the published thresholds and the submission Perch actually accepts."""
    import test_self_attestation as suite
    suite.main()


def test_magic_link():
    """Customer magic-link handoff: issue, redeem, expiry and scope."""
    import test_magic_link as suite
    suite.main()


def test_orphan_enrollment_cleanup():
    """Provisional enrollments left by a failed attempt are discarded only when provably local-only."""
    import test_orphan_enrollment_cleanup as suite
    suite.main()


def test_unified_login():
    """One sign-in form for staff and customers; the backend decides which."""
    import test_unified_login as suite
    suite.main()


def test_sales_report():
    """Sales reporting totals and the rep-visible breakdown."""
    import test_sales_report as suite
    suite.main()


def test_bill_page_integrity():
    """Every page of a multi-page bill survives to submission."""
    import test_bill_page_integrity as suite
    suite.main()

def test_bill_ocr_utilities():
    """Reading NYSEG / RG&E / Dominion bills, and the upload left untouched."""
    import test_bill_ocr_utilities as suite
    suite.main()


def test_gui_perch_bridge():
    """The browser/Perch bridge: persisted workflow state and what the GUI reads from it."""
    import test_gui_perch_bridge as suite
    suite.main()


def test_lmi_step_reconcile():
    """LMI step reconciliation against Perch's authoritative next_step."""
    import test_lmi_step_reconcile as suite
    suite.main()


def test_password_only_patch():
    """A password-only progress save never nulls customer identity columns."""
    import test_password_only_patch as suite
    suite.main()


def test_contract_link_resilience():
    """Contract links survive refresh, resume and a re-fetched packet."""
    import test_contract_link_resilience as suite
    suite.main()


def test_commit_boundary():
    """The Perch commit boundary: what may still be edited after /enroll. [v1 contract]"""
    import test_commit_boundary as suite
    suite.main()


def test_lmi_dual_state():
    """LMI document and self-attestation are independent states, not one toggle."""
    import test_lmi_dual_state as suite
    suite.main()


def test_review_consistency():
    """Review & finish shows the authoritative program, not an inferred one."""
    import test_review_consistency as suite
    suite.main()


def test_savings_and_utility():
    """Persisted savings and utility display for the dashboard and review.

    [v1 contract] - asserts the v1 flat capacity shape, so a forced-v2 run is
    expected to fail it; see V1_CONTRACT_SUITES.

    Script-style: this suite runs its checks AT IMPORT and has no main(), so
    importing it is how it is invoked. importlib is used rather than a plain
    import so a second call in the same process re-runs it instead of being a
    no-op from the module cache.
    """
    import importlib
    import test_savings_and_utility as suite
    importlib.reload(suite)
