"""Vercel Function entry point (file-based function in the /api directory).

Vercel's Python runtime detects the WSGI callable named ``app`` and serves it;
``vercel.json`` rewrites every path to ``/api/index`` so the Flask router owns
all routes (including ``/health`` and ``/readyz``).
"""
import sys
from pathlib import Path

# The function's import root is the project root, but be explicit so the import
# also works when this module is loaded from an unexpected working directory.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import app  # noqa: E402,F401  (re-exported as the WSGI entry)

application = app
