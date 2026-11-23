# 仓间配额说明（当班封死纪要）

> 适用模块：冷链仓间（chambers）/ 温控批次（batches）/ 封批（seal）/ 探头（probes）/ 对账（reconcile）。
> 封死日期：2026-11-23 当班。文件库（静态资源挂载）与鉴权（auth 登录/角色）不在本次改动面，照旧。

## 1. 唯一正确性口径（甲路径）

仓间满额入批**只允许甲路径**，全模块同写路径，仓间服务、批次服务、封批服务、探头服务与五页前端不得混用第二套口径：

- 仓间在批数 `active = COUNT(temp_batches WHERE chamber_id=? AND status NOT IN ('已封批','作废'))`；
- 当 `active >= quota_batches`（配额已满）时，`POST /api/batches` 必须：
  1. **拒写**：返回 `409 仓间在批配额已满`；
  2. **批次正式表 `temp_batches` 零增**：不插入正式批，也不插入任何"影子/待排"形态的批次；
  3. **仓间计数零改**：`active_batch_count`、`quota_left` 与对账页 `active_batches/quota_left/balanced` 维持原值。

不许第三路；尤其禁止"满额仓间仍落正式新批"。任何偏离本纪要的实现按 **配额偏航** 处理：拒写（拒绝合入/拒绝请求），并留痕。

## 2. 乙路径（影子待排）——本次明确不采用

乙路径定义（仅留档对照，禁止实现）：满额时允许落影子待排批次，正式批次零增，待腾出配额后**同事务晋升**；晋升失败则影子整笔回滚。

本次封死结论：**不建影子表、不对待排状态开口**。测试 `test_b_no_shadow_pending_batch_on_reject` 永久盯防——任何未晋升却计入在批清单的影子批即视为配额偏航。

## 3. 并发口径：占仓间锁，后到只记痕不增批

- 双会话同时在同一仓间抢建批次，必须以 `chamber:{chamber_id}` 为锁键串行化；
- 锁获取与配额复校在同一写事务（`BEGIN IMMEDIATE`）内完成，杜绝"检查时有名额、插入时已超配"的竞态；
- 后到争用：只向 `contention_log` 记一条 `争用` 痕，返回 409，**不增批、不改计数**；
- 事务结束（提交或回滚）后必须释放仓间锁，不留持锁残留。

## 4. 五页同源

仓间页、批次页、封批页、探头页、对账页对"在批数/剩余名额/是否满额"的展示必须同源：

- 计数 SQL 以 `quota.py` 的 `ACTIVE_BATCH_FILTER` / `count_active_batches()` 为唯一来源；
- 后端 `/api/chambers`、`/api/chambers/{id}`、`/api/reconcile*` 均经同一函数取值，禁止各页各算；
- 前端只消费后端字段（`active_batch_count`、`quota_batches`、`quota_left`、`quota_full`），禁止在浏览器端另造计数；
- 禁止只改仓间页进度环/剩余名额而不动写路径——显示与正式表行数必须一致。

## 5. 红测盯防（tests/test_quota_full.py，先红后绿）

| 用例 | 盯防点 |
| --- | --- |
| `test_a_full_chamber_rejects_new_batch` | 满额仍建新正式批（409 拒写 + 正式表零增 + 计数零改 + 对账平衡） |
| `test_b_no_shadow_pending_batch_on_reject` | 影子批未晋升却计入在批清单 |
| `test_c_concurrent_create_single_writer` | 双人同仓间抢建（双请求同锁键，一成一 409，只增 1 行，争用留痕，锁释放） |

种子夹具在 pytest 临时库（`tmp_path`）内本地可跑，不污染 `data/app.db`。无红不测后端，禁止跳过失败测，禁止无复现改按钮。
