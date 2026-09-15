"""Vercel entry point for the Flask application.

Keep this file inside the top-level `api/` directory.  The parent directory
contains app.py, templates/, and static/.  Adding the parent to sys.path makes
the import work reliably in Vercel's Python function environment.
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import app  # noqa: E402

# Vercel discovers the Flask WSGI application as `app`.
