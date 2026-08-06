"""Perch Partner Enrollment API integration.

Routes should import from services.perch.adapter only. Importing client,
mock_client, config, or token_manager directly from a route defeats the
abstraction boundary that makes the mock -> live swap a config change.
"""
