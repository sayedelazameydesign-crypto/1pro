#!/usr/bin/env python3
"""Seed a throwaway Marketplace installation and serve the dashboard.

Development only. It writes two rows into whichever database WAHA_DB points at
so the Arabic dashboard has something to render, then starts Flask on 0.0.0.0 so
the page can be looked at in a browser. It never touches Vercel and never needs
a real integration: the rows are a fixture, which is exactly why the dashboard
cannot be trusted as evidence that an install worked -- only a real one can.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("WAHA_DB", "/tmp/waha-marketplace-demo.db")
os.environ.setdefault("WAHA_SECRET", "demo-secret-not-for-production-0123456789")
os.environ.setdefault("VERCEL_INTEGRATION_CLIENT_ID", "oac_demoClientId")
os.environ.setdefault("VERCEL_INTEGRATION_CLIENT_SECRET", "demo-client-secret-0123456789")
os.environ.setdefault("WAHA_MARKETPLACE_BASE_URL", "https://cela-umber.vercel.app")
os.environ.setdefault("WAHA_MARKETPLACE_REDIRECT_URL",
                      "https://cela-umber.vercel.app/marketplace/configure")

import app as backend  # noqa: E402
from marketplace import config as mpconfig  # noqa: E402

INSTALLATION = "icfg_demoInstallation"
store = backend.marketplace_store
store.ensure_schema()
# Idempotent: the demo re-runs against the same file, and a duplicate resource id
# would abort the whole seed with an IntegrityError that says nothing useful.
with backend.connect() as db:
    for table in ("mp_events", "mp_resources", "mp_installations"):
        backend.run(db, f"DELETE FROM {table}")

workspace_id, api_token = backend.marketplace_credentials()
store.upsert_installation(INSTALLATION, access_token="demo-vercel-access-token",
                          scopes=["projects:read"],
                          accepted_policies={"toc": "2026-01-01T00:00:00Z"},
                          account={"name": "فريق العرض", "url": "https://example.test",
                                   "contact": {"email": "ops@example.test", "name": "العمليات"}},
                          team_id="team_demo",
                          billing_plan_id=mpconfig.FREE_PLAN["id"])
resource = store.create_resource("res_demo0001", INSTALLATION, mpconfig.DEFAULT_PRODUCT_ID,
                                 "مساحة العرض", {"workspace_name": "مساحة العرض",
                                                 "focus": "general"},
                                 mpconfig.FREE_PLAN["id"], workspace_id,
                                 os.environ["WAHA_MARKETPLACE_BASE_URL"], api_token)
store.record_event("evt_demo1", "integration-configuration.scope-change-confirmed",
                   created_at="2026-10-07T12:00:00Z", installation_id=INSTALLATION,
                   payload={"configuration": {"id": INSTALLATION}}, handled="recorded")

print("installation :", INSTALLATION)
print("resource     :", resource["id"])
print("workspace    :", workspace_id)
print("dashboard    : /marketplace?installation=%s" % INSTALLATION)
print("session      : %s" % backend.MARKETPLACE.session_token(INSTALLATION))
backend.app.run(host="0.0.0.0", port=5210, debug=False)
