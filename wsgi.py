"""WSGI entry point for runners that expect a repo-root module.

Used for local/other-platform serving, e.g.::

    gunicorn wsgi:application --bind 0.0.0.0:8000

Vercel itself does not load this file: vercel.json points the single function
at ``api/index.py``. Keeping both costs nothing and lets the same Flask app be
started from the repository root without touching ``backend/``.
"""
import sys
from pathlib import Path

# Allow `python wsgi.py` / gunicorn from any working directory.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import app  # noqa: E402

application = app
