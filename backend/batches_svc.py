"""温控批次入库：温度点序列 JSON。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import acquire_durable_lock, release_durable_lock
from models import BatchIn
from quota import chamber_quota, count_active_batches
from validators import summarize_points, validate_probe_online, validate_temp_point

router = APIRouter(tags=["batches"])

# 测试缝：获仓间锁后、写业务前调用；生产为 no-op，测试可 monkeypatch 制造持锁重叠。
def _lock_acquired_gate(holder: str, lock_key: str) -> None:
    return None


def _next_batch_code(conn, chamber_id: int) -> str:
    ch = conn.execute(
        "SELECT chamber_code FROM cold_chambers WHERE id=?", (chamber_id,)
    ).fetchone()
    prefix = ch["chamber_code"] if ch else f"CH-{chamber_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=?",
        (chamber_id,),
    ).fetchone()["c"]
    return f"TB-{prefix}-{n + 1:03d}"


@router.get("/api/batches")
def list_batches(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        if status:
            rows = conn.execute(
                """SELECT b.*, c.chamber_code, p.probe_code FROM temp_batches b
                   JOIN cold_chambers c ON c.id=b.chamber_id
                   JOIN probes p ON p.id=b.probe_id
                   WHERE b.status=? ORDER BY b.id DESC""",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT b.*, c.chamber_code, p.probe_code FROM temp_batches b
                   JOIN cold_chambers c ON c.id=b.chamber_id
                   JOIN probes p ON p.id=b.probe_id
                   ORDER BY b.id DESC"""
            ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["temp_points"] = json.loads(d["temp_points"] or "[]")
            items.append(d)
        return {"items": items}


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT b.*, c.name chamber_name, c.target_min, c.target_max, p.probe_code
               FROM temp_batches b
               JOIN cold_chambers c ON c.id=b.chamber_id
               JOIN probes p ON p.id=b.probe_id WHERE b.id=?""",
            (batch_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "批次不存在")
        data = dict(row)
        data["temp_points"] = json.loads(data["temp_points"] or "[]")
        exc = conn.execute(
            "SELECT * FROM excursion_cursors WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM seal_snapshots WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        return {
            "batch": data,
            "excursions": [dict(e) for e in exc],
            "seals": [dict(s) for s in seals],
        }


@router.post("/api/batches")
def create_batch(body: BatchIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"chamber:{body.chamber_id}"
    # 先占仓间锁（独立提交、跨会话可见）；后到争用只记痕、直接拒绝、不写业务
    if not acquire_durable_lock(lock_key, user):
        raise HTTPException(409, "争用：后到只记争用，不写业务")
    try:
        _lock_acquired_gate(user, lock_key)
        with connect() as conn:
            # 与锁同一写口径：立即开启写事务，配额复校与插入原子，杜绝检查/插入竞态
            conn.execute("BEGIN IMMEDIATE")
            ch = conn.execute(
                "SELECT * FROM cold_chambers WHERE id=?", (body.chamber_id,)
            ).fetchone()
            if not ch:
                conn.rollback()
                raise HTTPException(404, "仓间不存在")
            # 甲路径：配额已满 -> 拒写，正式表零增、计数零改
            active = count_active_batches(conn, body.chamber_id)
            quota_view = chamber_quota(conn, ch, active)
            if quota_view["quota_full"]:
                conn.rollback()
                write_audit(
                    conn,
                    user,
                    "batch_quota_reject",
                    ch["chamber_code"],
                    {
                        "chamber_id": body.chamber_id,
                        "active_batch_count": active,
                        "quota_batches": quota_view["quota_batches"],
                    },
                )
                conn.commit()
                raise HTTPException(409, "仓间在批配额已满")
            probe = conn.execute(
                "SELECT * FROM probes WHERE id=?", (body.probe_id,)
            ).fetchone()
            if not probe or probe["chamber_id"] != body.chamber_id:
                conn.rollback()
                raise HTTPException(422, "探头与仓间不匹配")
            validate_probe_online(probe)
            points = body.temp_points or []
            for pt in points:
                validate_temp_point(
                    float(pt.get("c", pt.get("temp", 0))),
                    float(ch["target_min"]),
                    float(ch["target_max"]),
                )
            tmin, tmax = summarize_points(points)
            code = body.batch_code or _next_batch_code(conn, body.chamber_id)
            if conn.execute(
                "SELECT id FROM temp_batches WHERE batch_code=?", (code,)
            ).fetchone():
                conn.rollback()
                raise HTTPException(409, "批次编码已存在")
            status = "已入库" if points else "草稿"
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO temp_batches(batch_code,chamber_id,probe_id,cargo_ref,
                   temp_points,generation,status,min_observed,max_observed,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.chamber_id,
                    body.probe_id,
                    body.cargo_ref,
                    json.dumps(points, ensure_ascii=False),
                    1,
                    status,
                    tmin if points else None,
                    tmax if points else None,
                    now,
                ),
            )
            write_audit(
                conn,
                user,
                "batch_inbound",
                code,
                {"batch_id": cur.lastrowid, "point_count": len(points)},
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "batch_code": code,
                "status": status,
                "point_count": len(points),
            }
    finally:
        release_durable_lock(lock_key, user)