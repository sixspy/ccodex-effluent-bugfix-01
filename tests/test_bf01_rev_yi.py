"""effluent-bugfix-01 撤准乙：影子销账未升正不得被对账当已撤销。

口径：对账已准排灯只认正式区活准排行，不认审计残留、不认影子
销账草稿。撤销若只删行不落正式清标（影子销账），对账两侧必然
撕裂——本测从灯源归属钉住：灯随正式区行生灭，双向都不得错。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

PERMIT = "DP-2026-E01"
CODE = "SM-BF01-YI-001"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "yi.db")
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


def test_reconcile_light_follows_official_register_only():
    c = TestClient(app)
    h = _auth(c)
    sample_id, release_id = _release_fixture(c, h)

    # 正式准排行在位：对账灯须亮（正式区驱动，非影子）
    assert c.get(f"/api/reconcile/{PERMIT}", headers=h).json()["released"] is True

    # 撤销：销账须升正——响应与审计均报已清标，不得留影子草稿
    resp = c.post(f"/api/releases/{release_id}/revoke", headers=h)
    assert resp.status_code == 200
    assert resp.json()["reconcile_cleared"] is True
    audit = c.get("/api/audit", headers=h).json()["items"]
    rev = next(x for x in audit if x["action"] == "release_revoke")
    assert json.loads(rev["payload"])["cleared_reconcile"] is True

    # 正式区行已删：对账灯须灭——审计残留的准排痕迹不得当灯源
    assert c.get(f"/api/reconcile/{PERMIT}", headers=h).json()["released"] is False
    assert c.get(f"/api/proof/sample/{CODE}", headers=h).json()["released"] is False

    # 重新准排同批：灯须复亮——灯只认正式区行，双向同源
    r2 = c.post(
        "/api/releases",
        headers=h,
        json={"sample_id": sample_id, "officer": "值班员"},
    )
    assert r2.status_code == 200
    assert c.get(f"/api/reconcile/{PERMIT}", headers=h).json()["released"] is True
