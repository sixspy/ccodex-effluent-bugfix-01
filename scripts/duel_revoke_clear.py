"""effluent-bugfix-01 争用脚本：两路会话并发打同批撤销写口。

会话甲（admin）持准排锁在途撤销，会话乙（writer）同批点撤销：
乙须被 409 挡下只记争用痕零改；甲放锁后乙重试一次改通，
准排行删、对账已准排灯灭、采样批回落已裁定。

用法：python3 scripts/duel_revoke_clear.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402
from app import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PERMIT = "DP-2026-E01"
CODE = "SM-BF01-DUEL-001"


def _auth(c, user, password):
    tok = c.post("/api/login", json={"username": user, "password": password}).json()[
        "token"
    ]
    return {"Authorization": "Bearer " + tok}


def main() -> int:
    db.DB_PATH = pathlib.Path(tempfile.mkdtemp(prefix="duel_revoke_")) / "duel.db"
    db.init_db()

    c = TestClient(app)
    ha = _auth(c, "admin", "admin123")
    hb = _auth(c, "writer", "writer123")

    # 夹具：入库 → 准排
    outfalls = c.get("/api/outfalls", headers=ha).json()["items"]
    of = next(x for x in outfalls if x["outfall_code"] == "OF-E1-B")
    created = c.post(
        "/api/samples",
        headers=ha,
        json={
            "outfall_id": of["id"],
            "permit_ref": PERMIT,
            "sample_code": CODE,
            "flow_m3h": 1200,
            "metrics": {"cod": 30.0, "nh3n": 2.0, "tp": 0.2},
        },
    ).json()
    assert created["status"] == "已裁定", created
    r = c.post(
        "/api/releases",
        headers=ha,
        json={"sample_id": created["id"], "officer": "值班员"},
    )
    assert r.status_code == 200, r.text
    releases = c.get("/api/releases", headers=ha).json()["items"]
    release_id = next(x for x in releases if x["sample_code"] == CODE)["id"]
    lock_key = f"release:{release_id}"
    print(f"[夹具] 准排就绪 release_id={release_id} 锁键={lock_key}")

    # 两路会话并发：甲持锁在途撤销，乙同批点撤销
    lock_held = threading.Event()
    duel_done = threading.Event()
    outcome: dict = {}

    def session_a():
        ca = TestClient(app)
        r = ca.post(f"/api/locks/acquire?lock_key={lock_key}", headers=ha)
        assert r.status_code == 200, r.text
        lock_held.set()
        duel_done.wait(10)
        ca.post(f"/api/locks/release?lock_key={lock_key}", headers=ha)

    def session_b():
        cb = TestClient(app)
        lock_held.wait(10)
        resp = cb.post(f"/api/releases/{release_id}/revoke", headers=hb)
        outcome["loser_status"] = resp.status_code
        duel_done.set()

    ta = threading.Thread(target=session_a)
    tb = threading.Thread(target=session_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    fails = []

    def check(name, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
        if not ok:
            fails.append(name)

    # 败方被锁挡下，只记争用痕
    check("败方409被锁挡下", outcome.get("loser_status") == 409,
          f"got={outcome.get('loser_status')}")
    releases = c.get("/api/releases", headers=ha).json()["items"]
    check("败方零改·准排行仍在", any(x["id"] == release_id for x in releases))
    recon = c.get(f"/api/reconcile/{PERMIT}", headers=ha).json()
    check("败方零改·对账灯仍亮", recon["released"] is True)
    sample = c.get(f"/api/samples/{created['id']}", headers=ha).json()["sample"]
    check("败方零改·采样批仍已准排", sample["status"] == "已准排")
    contention = c.get("/api/locks", headers=ha).json()["contention"]
    hit = [x for x in contention if x["lock_key"] == lock_key]
    check("争用痕已记", len(hit) == 1 and hit[0]["actor"] == "writer",
          json.dumps(hit, ensure_ascii=False))

    # 胜方放锁，败方重试：一次改通
    retry = c.post(f"/api/releases/{release_id}/revoke", headers=hb)
    check("重试200一次改通", retry.status_code == 200
          and retry.json().get("reconcile_cleared") is True,
          retry.text)
    releases = c.get("/api/releases", headers=ha).json()["items"]
    check("改通·准排行已删", all(x["id"] != release_id for x in releases))
    recon = c.get(f"/api/reconcile/{PERMIT}", headers=ha).json()
    check("改通·对账灯灭", recon["released"] is False)
    sample = c.get(f"/api/samples/{created['id']}", headers=ha).json()["sample"]
    check("改通·采样批回落已裁定", sample["status"] == "已裁定")

    if fails:
        print(f"duel_revoke_clear: {len(fails)} 项未过 -> {fails}")
        return 1
    print("duel_revoke_clear: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
