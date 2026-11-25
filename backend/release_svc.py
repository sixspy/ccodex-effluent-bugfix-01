"""准排放裁定放行。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import ReleaseIn
from validators import validate_sample_releasable

router = APIRouter(tags=["release"])


@router.get("/api/releases")
def list_releases(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, s.sample_code, o.outfall_code FROM discharge_releases r
               JOIN sample_batches s ON s.id=r.sample_id
               JOIN outfalls o ON o.id=r.outfall_id
               ORDER BY r.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/releases")
def create_release(body: ReleaseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"sample:{body.sample_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            sample = conn.execute(
                "SELECT * FROM sample_batches WHERE id=?", (body.sample_id,)
            ).fetchone()
            if not sample:
                raise HTTPException(404, "采样批次不存在")
            validate_sample_releasable(sample["status"])
            open_hold = conn.execute(
                "SELECT id FROM exceed_holds WHERE sample_id=? AND status='开放'",
                (body.sample_id,),
            ).fetchone()
            if open_hold:
                raise HTTPException(400, "仍有开放超标挂起，禁止准排")
            existing = conn.execute(
                "SELECT id FROM discharge_releases WHERE sample_id=? AND status='已准排'",
                (body.sample_id,),
            ).fetchone()
            if existing:
                raise HTTPException(400, "已准排，勿重复")
            ref = f"RL-{sample['sample_code']}"
            conn.execute(
                """INSERT INTO discharge_releases(sample_id,outfall_id,release_ref,status,officer,released_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    body.sample_id,
                    sample["outfall_id"],
                    ref,
                    "已准排",
                    body.officer,
                    now_iso(),
                ),
            )
            conn.execute(
                "UPDATE sample_batches SET status=? WHERE id=?",
                ("已准排", body.sample_id),
            )
            write_audit(
                conn,
                user,
                "release",
                sample["sample_code"],
                {"release_ref": ref, "officer": body.officer},
            )
            conn.commit()
            return {"release_ref": ref, "status": "已准排"}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/releases/{release_id}/revoke")
def revoke_release(release_id: int, authorization: AuthHeader = None):
    """撤准甲路径：同事务删准排行并清对账已准排标记，否则整笔回退。

    两路会话同批撤销须抢准排锁 release:{release_id}，败方只记争用痕零改。
    """
    user = require_writer(authorization)
    lock_key = f"release:{release_id}"
    with connect() as conn:
        try:
            require_lock(conn, lock_key, user)
        except HTTPException:
            conn.commit()  # 败方只落争用痕，正式区零改
            raise
        try:
            row = conn.execute(
                "SELECT * FROM discharge_releases WHERE id=?", (release_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "准排不存在")
            # 同一事务：删准排行 + 清对账已准排标记（采样批状态回落已裁定）。
            # 状态守卫只回已准排批，已封批等他态不得混用。
            conn.execute("DELETE FROM discharge_releases WHERE id=?", (release_id,))
            conn.execute(
                "UPDATE sample_batches SET status='已裁定' WHERE id=? AND status='已准排'",
                (row["sample_id"],),
            )
            write_audit(
                conn,
                user,
                "release_revoke",
                str(release_id),
                {
                    "release_ref": row["release_ref"],
                    "sample_id": row["sample_id"],
                    "cleared_reconcile": True,
                },
            )
            conn.commit()
            return {"ok": True, "release_id": release_id, "reconcile_cleared": True}
        except Exception:
            conn.rollback()  # 任一步失败整笔回退
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
