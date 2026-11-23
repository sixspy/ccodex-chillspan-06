"""满额仓间禁新批 —— 红测三件套（甲/乙/争用）。

口径：甲路径（配额已满 -> 正式表零增、拒写、仓间计数零改、只记痕）。
- test_a_full_chamber_rejects_new_batch      盯满额仍建新正式批
- test_b_no_shadow_pending_batch_on_reject   盯影子批未晋升却计入在批清单
- test_c_concurrent_create_single_writer     盯双人同仓间抢建

所有用例均在 tmp_path 临时库上运行，种子夹具本地可跑、互不污染。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

POINTS = [
    {"t": "08:00", "c": -19.1},
    {"t": "09:00", "c": -18.9},
]


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _make_full_chamber(c: TestClient, h: dict, quota: int = 1) -> dict:
    """造满额仓间夹具：quota 个在批正式批，剩余名额为 0。"""
    code = "CK-FULL-01"
    r = c.post(
        "/api/chambers",
        headers=h,
        json={
            "chamber_code": code,
            "region": "华东",
            "name": "满额夹具仓",
            "target_min": -22.0,
            "target_max": -18.0,
            "quota_batches": quota,
        },
    )
    assert r.status_code == 200, r.text
    chamber_id = r.json()["id"]
    pr = c.post(
        "/api/probes",
        headers=h,
        json={
            "probe_code": "PR-FULL-01",
            "chamber_id": chamber_id,
            "model": "T-200",
            "calibration_due": "2027-12-01",
        },
    )
    assert pr.status_code == 200, pr.text
    probe_id = pr.json()["id"]
    for i in range(quota):
        rb = c.post(
            "/api/batches",
            headers=h,
            json={
                "chamber_id": chamber_id,
                "probe_id": probe_id,
                "cargo_ref": f"占位在批货载 {i + 1}",
                "temp_points": POINTS,
                "batch_code": f"TB-FULL-OCC-{i + 1:03d}",
            },
        )
        assert rb.status_code == 200, rb.text
    detail = c.get(f"/api/chambers/{chamber_id}", headers=h).json()
    assert detail["active_batch_count"] == quota
    assert detail["quota_left"] == 0
    return {"chamber_id": chamber_id, "probe_id": probe_id, "code": code}


def _formal_count(chamber_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=?",
            (chamber_id,),
        ).fetchone()["c"]


def _active_count(chamber_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            """SELECT COUNT(*) c FROM temp_batches
               WHERE chamber_id=? AND status NOT IN ('已封批','作废')""",
            (chamber_id,),
        ).fetchone()["c"]


# ---------- 甲：满额拒写，正式表零增，计数零改 ----------

def test_a_full_chamber_rejects_new_batch():
    c = TestClient(app)
    h = _auth(c)
    fx = _make_full_chamber(c, h, quota=1)
    before_formal = _formal_count(fx["chamber_id"])
    before_active = _active_count(fx["chamber_id"])

    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": fx["chamber_id"],
            "probe_id": fx["probe_id"],
            "cargo_ref": "满额强推货载",
            "temp_points": POINTS,
            "batch_code": "TB-FULL-OVER-001",
        },
    )
    # 甲路径：配额已满必须拒写
    assert r.status_code == 409, r.text
    assert "配额" in r.json()["detail"]

    # 正式批次表零增
    assert _formal_count(fx["chamber_id"]) == before_formal
    # 仓间在批计数零改
    assert _active_count(fx["chamber_id"]) == before_active
    # 批次页实增行数为零：清单里不存在被拒批次
    listed = c.get("/api/batches", headers=h).json()["items"]
    assert all(b["batch_code"] != "TB-FULL-OVER-001" for b in listed)

    # 仓间页剩余名额与对账页同源，仍为 0 且平衡
    detail = c.get(f"/api/chambers/{fx['chamber_id']}", headers=h).json()
    assert detail["quota_left"] == 0
    assert detail["active_batch_count"] == before_active
    assert detail["quota_full"] is True
    # 仓间列表行必须携带同源于同一计数口径的在批/剩余/满额字段，五页不得各算
    row = next(
        x for x in c.get("/api/chambers", headers=h).json()["items"]
        if x["id"] == fx["chamber_id"]
    )
    assert row["active_batch_count"] == before_active
    assert row["quota_left"] == 0
    assert row["quota_full"] is True
    recon = c.get(f"/api/reconcile/{fx['code']}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["active_batches"] == before_active
    assert recon["quota_left"] == 0


# ---------- 乙：不得落影子/待排批；落则必须晋升，否则整笔回滚 ----------

def test_b_no_shadow_pending_batch_on_reject():
    c = TestClient(app)
    h = _auth(c)
    fx = _make_full_chamber(c, h, quota=1)
    before_formal = _formal_count(fx["chamber_id"])

    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": fx["chamber_id"],
            "probe_id": fx["probe_id"],
            "cargo_ref": "影子强推货载",
            "temp_points": POINTS,
            "batch_code": "TB-SHADOW-001",
        },
    )
    assert r.status_code == 409, r.text

    # 正式表零增：不存在任何形态（含待排/影子）的该批次
    with connect() as conn:
        shadow_rows = conn.execute(
            "SELECT id, status FROM temp_batches WHERE batch_code=?",
            ("TB-SHADOW-001",),
        ).fetchall()
    assert len(shadow_rows) == 0, f"影子批泄漏进正式表: {[dict(x) for x in shadow_rows]}"
    assert _formal_count(fx["chamber_id"]) == before_formal

    # 未晋升的影子批不得计入在批清单：批次页读不到任何待排/影子状态
    listed = c.get("/api/batches", headers=h).json()["items"]
    leak = [b for b in listed if "待排" in b["status"] or "影子" in b["status"]]
    assert leak == []
    assert all(b["batch_code"] != "TB-SHADOW-001" for b in listed)

    # 仓间在批计数不被影子批顶大，且满额标记与剩余名额同源
    detail = c.get(f"/api/chambers/{fx['chamber_id']}", headers=h).json()
    assert detail["active_batch_count"] == before_formal
    assert detail["quota_left"] == 0
    assert detail["quota_full"] is True
    row = next(
        x for x in c.get("/api/chambers", headers=h).json()["items"]
        if x["id"] == fx["chamber_id"]
    )
    assert row["active_batch_count"] == before_formal
    assert row["quota_left"] == 0
    assert row["quota_full"] is True
    # 影子批若泄漏，对账会出现 active>quota 的失衡；这里必须恒平衡
    recon = c.get(f"/api/reconcile/{fx['code']}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["active_batches"] <= recon["quota_batches"]


# ---------- 争用：双会话同满额仓间抢建，占仓间锁，后到只记痕不增批 ----------

def test_c_concurrent_create_single_writer():
    c = TestClient(app)
    h = _auth(c)
    h_ops = _auth(c, "ops")
    # 配额 2、已占 1：仅剩 1 个名额
    code = "CK-RACE-01"
    r = c.post(
        "/api/chambers",
        headers=h,
        json={
            "chamber_code": code,
            "region": "华北",
            "name": "争用夹具仓",
            "target_min": -25.0,
            "target_max": -20.0,
            "quota_batches": 2,
        },
    )
    assert r.status_code == 200, r.text
    race_chamber_id = r.json()["id"]
    pr = c.post(
        "/api/probes",
        headers=h,
        json={
            "probe_code": "PR-RACE-01",
            "chamber_id": race_chamber_id,
            "model": "T-180",
            "calibration_due": "2027-12-01",
        },
    )
    assert pr.status_code == 200, pr.text
    race_probe_id = pr.json()["id"]
    rb = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": race_chamber_id,
            "probe_id": race_probe_id,
            "cargo_ref": "争用占位货载",
            "temp_points": POINTS,
            "batch_code": "TB-RACE-OCC-001",
        },
    )
    assert rb.status_code == 200, rb.text
    before_formal = _formal_count(race_chamber_id)
    assert before_formal == 1
    lock_key = f"chamber:{race_chamber_id}"

    def _post_batch(headers: dict, suffix: str):
        return c.post(
            "/api/batches",
            headers=headers,
            json={
                "chamber_id": race_chamber_id,
                "probe_id": race_probe_id,
                "cargo_ref": f"争用货载-{suffix}",
                "temp_points": POINTS,
                "batch_code": f"TB-RACE-{suffix}",
            },
        )

    # 阶段一：第三会话 admin 先持住该仓间锁，两个非持锁会话(ops 两令牌)同时抢建
    # -> 双双撞锁。后到争用只记痕、不写业务；这里直接验证"持锁期间绝不增批"。
    # 注：锁对持锁者本人可重入，故争用双方必须都不是持锁人。
    h_race1 = _auth(c, "ops")
    h_race2 = _auth(c, "ops")
    la = c.post(
        "/api/locks/acquire", headers=h, json={"lock_key": lock_key}
    )
    assert la.status_code == 200, la.text
    held_cont_before = len(c.get("/api/contention", headers=h).json()["items"])
    r1 = _post_batch(h_race1, "ADM")
    r2 = _post_batch(h_race2, "OPS")
    assert r1.status_code == 409, r1.text
    assert r2.status_code == 409, r2.text
    assert "争用" in r1.json()["detail"]
    assert "争用" in r2.json()["detail"]
    # 持锁期间正式表零增
    assert _formal_count(race_chamber_id) == before_formal
    # 争用留痕落库且不随拒绝回滚
    cont_during = c.get("/api/contention", headers=h).json()["items"]
    assert len(cont_during) >= held_cont_before + 2
    assert any(x["lock_key"] == lock_key for x in cont_during)

    # 阶段二：释放锁后双请求同仓间抢建（仅剩 1 名额），恰一成一拒、只增 1 行
    rl = c.post(
        "/api/locks/release", headers=h, json={"lock_key": lock_key}
    )
    assert rl.status_code == 200, rl.text
    start = threading.Barrier(2)

    def _race(headers: dict, suffix: str) -> int:
        cc = TestClient(app, raise_server_exceptions=False)
        start.wait()
        return _post_batch_via(cc, headers, race_chamber_id, race_probe_id, suffix)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_race, h, "WIN")
        f2 = pool.submit(_race, h_ops, "LOSE")
        codes = sorted([f1.result(), f2.result()])
    assert codes == [200, 409], codes

    # 正式表只增 1 行，绝不超配额
    assert _formal_count(race_chamber_id) == before_formal + 1
    assert _active_count(race_chamber_id) == 2

    detail = c.get(f"/api/chambers/{race_chamber_id}", headers=h).json()
    assert detail["active_batch_count"] == 2
    assert detail["quota_left"] == 0
    assert detail["quota_full"] is True

    recon = c.get(f"/api/reconcile/{code}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["active_batches"] == 2

    # 全程争用只记痕：该仓间锁至少有一条争用记录
    cont = c.get("/api/contention", headers=h).json()["items"]
    assert any(x["lock_key"] == lock_key for x in cont), cont

    # 锁已释放，不留持锁残留
    locks = c.get("/api/locks", headers=h).json()["items"]
    assert all(x["lock_key"] != lock_key for x in locks), locks


def _post_batch_via(cc: TestClient, headers: dict, chamber_id: int, probe_id: int, suffix: str) -> int:
    rr = cc.post(
        "/api/batches",
        headers=headers,
        json={
            "chamber_id": chamber_id,
            "probe_id": probe_id,
            "cargo_ref": f"争用货载-{suffix}",
            "temp_points": POINTS,
            "batch_code": f"TB-RACE-{suffix}",
        },
    )
    return rr.status_code
