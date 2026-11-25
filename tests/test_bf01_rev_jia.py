"""effluent-bugfix-01 撤准甲：撤销须同事务删准排行并清对账已准排标记。

夹具：采样入库 → 准排（已准排）→ 值班点撤销。
钉住缺陷：正式准排行已删，但对账已准排标记仍在（对账灯仍亮、
采样批仍计准排）。甲路径口径：删行与清标同事务，否则整笔回退。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

PERMIT = "DP-2026-E01"
CODE = "SM-BF01-JIA-001"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "jia.db")
    init_db()
    yield


def _auth(c, user="admin", password="admin123"):
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _release_fixture(c, h):
    """已准排夹具：返回 (sample_id, release_id)。"""
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


def test_revoke_deletes_row_and_clears_reconcile_marker_same_tx():
    c = TestClient(app)
    h = _auth(c)
    sample_id, release_id = _release_fixture(c, h)

    # 准排在位：对账灯亮、总览计数含本批
    assert c.get(f"/api/reconcile/{PERMIT}", headers=h).json()["released"] is True
    dash_before = c.get("/api/dashboard", headers=h).json()["discharge_releases"]

    resp = c.post(f"/api/releases/{release_id}/revoke", headers=h)
    assert resp.status_code == 200
    # 甲路径：写口须报清标已同事务完成，非影子销账
    assert resp.json()["reconcile_cleared"] is True

    # 准排页无行
    releases = c.get("/api/releases", headers=h).json()["items"]
    assert all(x["id"] != release_id for x in releases)
    # 对账已准排灯灭：删行须同事务清标，不得删行留标
    recon = c.get(f"/api/reconcile/{PERMIT}", headers=h).json()
    assert recon["released"] is False
    # 采样证明同源：released 熄灭
    proof = c.get(f"/api/proof/sample/{CODE}", headers=h).json()
    assert proof["released"] is False
    # 采样批状态回落已裁定，流量不再计准排
    sample = c.get(f"/api/samples/{sample_id}", headers=h).json()["sample"]
    assert sample["status"] == "已裁定"
    # 总览准排计数回落
    dash_after = c.get("/api/dashboard", headers=h).json()["discharge_releases"]
    assert dash_after == dash_before - 1
    # 审计留痕：销账已清标
    audit = c.get("/api/audit", headers=h).json()["items"]
    rev = next(x for x in audit if x["action"] == "release_revoke")
    assert json.loads(rev["payload"])["cleared_reconcile"] is True
