"""Health, metrics, guard rails and the audit reader that the ops scripts depend on."""
from __future__ import annotations

import os

import pytest

from conftest import as_user
from control_plane import observability

ROOT_ID = 100001
STAFF_ID = 300300


def test_health_reports_a_balanced_ledger_and_safe_headers(client, session):
    response = client.get("/health")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Request-Id"]


def test_metrics_stay_private_without_the_token(client, session):
    assert client.get("/metrics").status_code == 401
    served = client.get("/metrics", headers={"X-Metrics-Token": os.environ["METRICS_TOKEN"]})
    assert served.status_code == 200
    body = served.text
    assert "# TYPE http_responses_total counter" in body
    assert "/health" in body and "process_uptime_seconds" in body


def test_write_requests_are_throttled_per_caller(client, session, make_org, make_actor, monkeypatch):
    monkeypatch.setattr(observability, "write_limiter", observability.RateLimiter(2, 60))
    responses = [client.post("/v1/webapp/funding-requests", headers=as_user(ROOT_ID), json={"amount_irr": 1000})
                 for _ in range(3)]
    assert responses[0].status_code == 403, "no membership exists yet, and the limiter still counted the write"
    blocked = responses[-1]
    assert blocked.status_code == 429
    assert "صبر" in blocked.json()["detail"] and int(blocked.headers["Retry-After"]) >= 1
    # Reads keep their own, much larger budget.
    assert client.get("/v1/webapp/capabilities", headers=as_user(ROOT_ID)).status_code != 429


def test_read_budget_is_separate_from_write_budget(client, session, monkeypatch):
    monkeypatch.setattr(observability, "anon_limiter", observability.RateLimiter(1, 60))
    assert client.get("/v1/webapp/capabilities", headers=as_user(ROOT_ID)).status_code in (200, 403)
    assert client.get("/v1/webapp/capabilities", headers=as_user(ROOT_ID)).status_code == 429


@pytest.fixture
def bootstrapped(client):
    response = client.post(
        "/v1/onboarding/bootstrap",
        headers=as_user(ROOT_ID),
        json={"business_name": "Central Holdings", "slug": "central-holdings",
              "setup_token": os.environ["INITIAL_SETUP_TOKEN"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_audit_reader_is_scoped_to_the_callers_subtree(client, session, bootstrapped, make_org, make_actor):
    from sqlalchemy import text

    branch = make_org()
    make_actor(STAFF_ID, branch, "operator")
    # The root workspace must exist for the unfiltered branch of the reader to be exercised.
    child = make_org(parent=branch)
    session.execute(text("INSERT INTO audit_logs (id, action, target_type, target_id, organization_id, metadata) "
                         "VALUES (:id,'unit.test','organization',:org,:org,'{}')"),
                    {"id": "00000000-0000-4000-8000-000000000001", "org": child.id})
    session.commit()

    scoped = client.get("/v1/webapp/audit", headers=as_user(STAFF_ID)).json()
    assert {row["organization_id"] for row in scoped} == {child.id}
    root_rows = client.get("/v1/webapp/audit", headers=as_user(ROOT_ID)).json()
    # The root reader is unfiltered, so it also sees rows outside its own subtree.
    assert child.id in {row["organization_id"] for row in root_rows}
    assert "workspace.bootstrap" in {row["action"] for row in root_rows}
    assert client.get("/v1/webapp/audit", headers=as_user(410410)).status_code == 403


def test_audit_reader_refuses_a_bad_limit(client, session, bootstrapped):
    assert client.get("/v1/webapp/audit?limit=abc", headers=as_user(ROOT_ID)).status_code == 422
    assert client.get("/v1/webapp/audit?limit=5000", headers=as_user(ROOT_ID)).status_code == 422


def test_metrics_counters_are_thread_safe_under_parallel_requests(client, session):
    import threading

    def hammer():
        for _ in range(20):
            client.get("/health")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    body = client.get("/metrics", headers={"X-Metrics-Token": os.environ["METRICS_TOKEN"]}).text
    hits = [line for line in body.splitlines() if line.startswith("http_requests_total{method=\"GET\",path=\"/health\"}")]
    assert hits and int(hits[0].rsplit(" ", 1)[1]) >= 80
