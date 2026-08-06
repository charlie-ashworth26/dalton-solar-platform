"""pytest configuration.

Puts the backend root on sys.path so test modules can `from app import app`
regardless of the working directory pytest is invoked from. Without this,
`python -m pytest` from the repo root fails to import the application package.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
