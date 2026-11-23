"""冷库仓间登记与配额。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from models import ChamberIn
from quota import active_counts_by_chamber, chamber_quota, count_active_batches

router = APIRouter(tags=["chambers"])


@router.get("/api/chambers")
def list_chambers(authorization: AuthHeader = None, region: str | None = None):
    require_user(authorization)
    with connect() as conn:
        if region:
            rows = conn.execute(
                "SELECT * FROM cold_chambers WHERE region=? ORDER BY id",
                (region,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cold_chambers ORDER BY id"
            ).fetchall()
        # 五页同源：在批计数只从 quota 模块这一个口径取
        counts = active_counts_by_chamber(conn)
        items = []
        for r in rows:
            d = dict(r)
            active = counts.get(int(r["id"]), 0)
            d.update(chamber_quota(conn, r, active))
            items.append(d)
        return {"items": items}


@router.get("/api/chambers/{chamber_id}")
def get_chamber(chamber_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ch = conn.execute(
            "SELECT * FROM cold_chambers WHERE id=?", (chamber_id,)
        ).fetchone()
        if not ch:
            raise HTTPException(404, "仓间不存在")
        probes = conn.execute(
            "SELECT * FROM probes WHERE chamber_id=? ORDER BY id",
            (chamber_id,),
        ).fetchall()
        batches = conn.execute(
            """SELECT id,batch_code,status,generation FROM temp_batches
               WHERE chamber_id=? ORDER BY id DESC LIMIT 20""",
            (chamber_id,),
        ).fetchall()
        active = count_active_batches(conn, chamber_id)
        quota_view = chamber_quota(conn, ch, active)
        return {
            "chamber": dict(ch),
            "probes": [dict(p) for p in probes],
            "recent_batches": [dict(b) for b in batches],
            **quota_view,
        }


@router.post("/api/chambers")
def create_chamber(body: ChamberIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    if body.target_min >= body.target_max:
        raise HTTPException(422, "目标温区下限须小于上限")
    with connect() as conn:
        dup = conn.execute(
            "SELECT id FROM cold_chambers WHERE chamber_code=?",
            (body.chamber_code,),
        ).fetchone()
        if dup:
            raise HTTPException(409, "仓间编码已存在")
        now = now_iso()
        cur = conn.execute(
            """INSERT INTO cold_chambers(chamber_code,region,name,target_min,target_max,
               quota_batches,status,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (
                body.chamber_code,
                body.region,
                body.name,
                body.target_min,
                body.target_max,
                body.quota_batches,
                "运行",
                now,
            ),
        )
        write_audit(
            conn,
            user,
            "chamber_create",
            body.chamber_code,
            {"id": cur.lastrowid},
        )
        conn.commit()
        return {"id": cur.lastrowid, "chamber_code": body.chamber_code, "status": "运行"}