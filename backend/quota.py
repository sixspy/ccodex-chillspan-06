"""仓间在批配额的唯一计数口径（五页同源，禁止各算）。

口径（见 docs/quota-policy.md，甲路径当班封死）：
- 在批 = temp_batches 中状态不属于「已封批/作废」的正式批；
- 影子/待排批不存在（乙路径不采用），任何未晋升批次不得计入；
- 剩余名额 = quota_batches - active；满额 = active >= quota_batches。
"""
from __future__ import annotations

import sqlite3

# 非在批状态：封批与作废不占名额。全模块只允许引用这一个常量。
NON_ACTIVE_STATUSES = ("已封批", "作废")


def count_active_batches(conn: sqlite3.Connection, chamber_id: int) -> int:
    placeholders = ",".join("?" for _ in NON_ACTIVE_STATUSES)
    row = conn.execute(
        f"""SELECT COUNT(*) c FROM temp_batches
            WHERE chamber_id=? AND status NOT IN ({placeholders})""",
        (chamber_id, *NON_ACTIVE_STATUSES),
    ).fetchone()
    return int(row["c"])


def active_counts_by_chamber(conn: sqlite3.Connection) -> dict[int, int]:
    """一次性取全仓在批计数，供列表/对账同源使用。"""
    placeholders = ",".join("?" for _ in NON_ACTIVE_STATUSES)
    rows = conn.execute(
        f"""SELECT chamber_id, COUNT(*) c FROM temp_batches
            WHERE status NOT IN ({placeholders}) GROUP BY chamber_id""",
        NON_ACTIVE_STATUSES,
    ).fetchall()
    return {int(r["chamber_id"]): int(r["c"]) for r in rows}


def chamber_quota(conn: sqlite3.Connection, chamber, active: int | None = None) -> dict:
    """把仓间行扩展为五页同源的配额视图（不写库）。"""
    quota = int(chamber["quota_batches"])
    if active is None:
        active = count_active_batches(conn, int(chamber["id"]))
    return {
        "quota_batches": quota,
        "active_batch_count": active,
        "quota_left": quota - active,
        "quota_full": active >= quota,
    }
