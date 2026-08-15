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
