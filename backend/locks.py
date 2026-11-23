"""仓间锁与争用登记（公共并发面）。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = now_iso()
    row = conn.execute(
        "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO chamber_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now),
        )
        return True
    if row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder']}", now),
    )
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM chamber_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


def _log_contention(lock_key: str, actor: str, detail: str) -> None:
    """争用留痕走独立连接并立即提交，绝不随业务事务回滚而丢失。"""
    with connect() as conn:
        conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, actor, "争用", detail, now_iso()),
        )
        conn.commit()


def acquire_durable_lock(lock_key: str, holder: str) -> bool:
    """占仓间锁：独立短连接、提交后对其它会话立即可见。

    - 无人持锁：插入锁行并提交，返回 True；
    - 自己已持锁：可重入，返回 True；
    - 他人持锁：只写一条争用痕（独立提交），返回 False，不写任何业务。
    """
    # 先纯读观测已提交的持锁状态（读不阻塞写者）
    with connect() as check:
        row = check.execute(
            "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
        ).fetchone()
    if row is not None:
        if row["holder"] == holder:
            return True
        _log_contention(lock_key, holder, f"持有者={row['holder']}")
        return False
    # 抢占：BEGIN IMMEDIATE 立即取 RESERVED 写锁，杜绝双插
    try:
        with connect() as lc:
            lc.execute("BEGIN IMMEDIATE")
            lc.execute(
                "INSERT INTO chamber_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now_iso()),
            )
            lc.commit()
        return True
    except sqlite3.IntegrityError:
        # 极端情况下被其它写者抢先：按争用记痕，不增业务
        _log_contention(lock_key, holder, "抢锁冲突")
        return False


def release_durable_lock(lock_key: str, holder: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM chamber_locks WHERE lock_key=? AND holder=?",
            (lock_key, holder),
        )
        conn.commit()


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chamber_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(conn, user, "lock_acquire", body.lock_key, {"ok": ok})
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}