"""effluent-bugfix-01 撤准争：两路会话同批撤销须抢准排锁。

口径：同一路准排行被两路会话同时点撤销，须抢准排锁
release:{release_id}；败方只记争用痕，正式区零改（准排行在、
对账灯亮、采样批仍已准排）。胜方放锁后败方重试须一次改通。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

PERMIT = "DP-2026-E01"
CODE = "SM-BF01-ZH-001"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "zh.db")
    init_db()
    yield


def _auth(c, user="admin", password="admin123"):
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _release_fixture(c, h):
    outfalls = c.get("/api/outfalls", headers=h).json()["items"]
    of = next(x for x in outfalls if x["outfall_code"] == "OF-E1-B")
    created = c.post(
        "/api/samples",
        headers=h,
        json={
            "outfall_id": of["id"],
            "permit_ref": PERMIT,
            "sample_code": CODE,
            "flow_m3h": 1200,
            "metrics": {"cod": 30.0, "nh3n": 2.0, "tp": 0.2},
        },
    ).json()
    assert created["status"] == "已裁定"
    r = c.post(
        "/api/releases",
        headers=h,
        json={"sample_id": created["id"], "officer": "值班员"},
    )
    assert r.status_code == 200
    releases = c.get("/api/releases", headers=h).json()["items"]
    row = next(x for x in releases if x["sample_code"] == CODE)
    return created["id"], row["id"]


def test_duel_revoke_loser_only_logs_contention():
    c = TestClient(app)
    ha = _auth(c)  # 会话甲：admin
    hb = _auth(c, "writer", "writer123")  # 会话乙：writer
    sample_id, release_id = _release_fixture(c, ha)
    lock_key = f"release:{release_id}"

    # 会话甲撤销在途，持有准排锁
    r = c.post(f"/api/locks/acquire?lock_key={lock_key}", headers=ha)
    assert r.status_code == 200

    # 会话乙同批点撤销：须被锁挡下
    resp = c.post(f"/api/releases/{release_id}/revoke", headers=hb)
    assert resp.status_code == 409

    # 败方零改：准排行在、对账灯亮、采样批仍已准排
    releases = c.get("/api/releases", headers=ha).json()["items"]
    assert any(x["id"] == release_id for x in releases)
    assert c.get(f"/api/reconcile/{PERMIT}", headers=ha).json()["released"] is True
    sample = c.get(f"/api/samples/{sample_id}", headers=ha).json()["sample"]
    assert sample["status"] == "已准排"

    # 败方只记争用痕
    contention = c.get("/api/locks", headers=ha).json()["contention"]
    hit = next((x for x in contention if x["lock_key"] == lock_key), None)
    assert hit is not None
    assert hit["actor"] == "writer"
    assert hit["action"] == "争用失败"

    # 胜方放锁，败方重试：一次改通，删行清标
    c.post(f"/api/locks/release?lock_key={lock_key}", headers=ha)
    retry = c.post(f"/api/releases/{release_id}/revoke", headers=hb)
    assert retry.status_code == 200
    assert retry.json()["reconcile_cleared"] is True
    releases = c.get("/api/releases", headers=ha).json()["items"]
    assert all(x["id"] != release_id for x in releases)
    assert c.get(f"/api/reconcile/{PERMIT}", headers=ha).json()["released"] is False
