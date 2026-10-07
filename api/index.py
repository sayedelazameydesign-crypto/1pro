"""Vercel Function entry point.

Vercel builds this repository as a Flask backend-framework project, resolves the
WSGI callable named ``app`` here, and hands every request to it **with its
original path** -- so the Flask router below owns ``/``, ``/health``, ``/readyz``
and all of ``/api/*`` without any help from ``vercel.json``.

Do not add a catch-all ``rewrites`` entry back to that file. Internal rewrites in
backend-framework projects deliver the *destination* path to the app, so
``/(.*) -> /api/index`` made Flask see ``PATH_INFO=/api/index`` for every request
and answer its own 404 page on ``/``, ``/health``, ``/readyz`` and ``/api/*``
(observed live on cela-umber.vercel.app). ``tests/test_vercel_wrapper.py`` and
``scripts/deploy_doctor.py --target vercel`` fail if the property returns.
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
