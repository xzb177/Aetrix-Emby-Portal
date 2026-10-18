# 阻塞路由清零（v2.39.0）

这篇是 [`docs/performance.md`](performance.md) 的续篇，专门记「`async` 路由里做同步 DB」
这条欠账的**收尾**：`scripts/check_blocking_routes.py` 的基线从 24 条清到 **0 条**。

背景与为什么这件事不能一次机械替换，见性能文档的「二·七」；前面几轮的进度是
41 条（v2.21.0 时点）→ 39（v2.35.0 两条热路径）→ 37（v2.36.0 两条求片链路 + 通知层）
→ 24（v2.37.0 探测 / 体检层）。这一版把剩下的 24 条做掉。

## 一、为什么必须做

项目用的是**同步 SQLAlchemy**，而 FastAPI 对 `async def` 端点是在事件循环上直接跑的
（只有 `def` 端点才会被丢进线程池）。所以一个 `async def` 路由里只要出现
`db.query(...)` / `db.commit()`，**整个进程**就得等这次查询——单机 SQLite 亚毫秒还能忍，
跨机 PostgreSQL 每查询一个 RTT 就是全站卡顿，而这里面还夹着「提交前等网络」的时候
（见 [`docs/performance-transactions.md`](performance-transactions.md)）。

## 二、口径

两种修法，按「这个端点有没有必须 await 的东西」选：

1. 什么都不用 await（绝大多数）→ 把 `async def` 改成同步 `def`。FastAPI 自动丢线程池，
   业务代码一个字都不用动。**首选。**
2. 确实要 await（WebSocket 通知 / 读表单 / 磁盘与子进程 / 外部推送）→ 把同步数据库那段
   拆成一个同步函数，用 `await run_in_threadpool(那个函数, ...)` 调用
   （仓库既有先例：`backend/emby_server/portal_mount_routes.py`）。

**提交之后不要回读 ORM 属性**：`Session.commit()` 会过期所有实例，之后哪怕 `ticket.title`
这样一次属性读取也会在事件循环上发一条 SELECT。这类隐式回查 `check_blocking_routes.py`
抓不到（它只认函数体里的 `db.xxx(`），所以工作线程里读、把纯值带回循环，是这一版的一等要求。

## 三、逐域改动

| 位置 | 原先 | 现在 |
| --- | --- | --- |
| `admin.py` 订阅授予 / 延长 | 查用户与套餐、插订阅、写审计都在 `async` 路由体里 | `_grant()` / `_extend()` 下放线程池；套餐名、到期时间这类提交后要用的值先取成纯值 |
| `admin.py` 站内消息 / 广播 / 公告增改 / 工单回复与关闭 / 求片状态 | 同上一行 | 校验 + 落库 + 审计各拆一个嵌套同步函数下放；`notify_admin_event` / `notify_all_users` 仍 await |
| `admin.py::economy_mark_order_paid`（人工补单） | 查单 + 反幂等 + 履约 + 审计全在 `async` 体里 | 模块级 `_mark_order_paid()`（履约与审计同一段写库）下放线程池；通知在提交之后发 |
| `admin_economy.py::economy_adjust_points`、`reminders_admin.py::run_expiry_reminders` | 调账与审计、提醒之后的审计在 `async` 体里 | 各自拆出同步核心下放；提醒本体（要 await 推送）仍在循环上 |
| `coupons_admin.py::list_coupon_usages` / `list_coupon_usages_by_code` | 两个端点各自跑同一段查询，且都在 `async` 体里读库 | 查询抽成同步 `coupon_usages_payload`，两个端点都改同步 `def` |
| `economy.py::do_checkin` / `redeem_exchange_code` | 防重 / 连签 / 发积分 / 原子占位 / 发订阅 / 核销审计都在 `async` 体里 | 各拆一个同步核心下放；`notify_admin_event` 仍 await |
| `economy.py::payment_notify`（易支付回调） | 验签 / 查单 / 金额核对 / 履约都在 `async` 体里 | 拆成 `_notify()` 下放线程池；只有读表单与发通知留在循环上 |
| `economy._fulfill_order` | `async def`，但体内一个 await 都没有（回调与人工补单都被迫在循环上跑它） | 改同步 `def`，两个调用方都用 `run_in_threadpool` 调它 |
| `user.py` 消息全标已读 / 创建与回复工单 | 批量 UPDATE、工单落库在 `async` 体里 | 各自下放；工单通知仍 await（提交后回读的属性先取成纯值） |
| `emby_server/api.py::rate_item` | 收藏 / 已看标记（读条目 + 写 `UserMediaData` + 序列化）在 `async` 体里 | 只有 `await request.json()` 留在循环上，其余整段在工作线程 |
| `portal.py::stop_my_session` / `admin_stop_session` | 查会话 + 写结束时间在 `async` 体里 | 下放；转码已删除，不再有子进程可停 |
| `portal.py::scan_library_endpoint` | 查库 + 归属节点判定 + 入队都在 `async` 体里 | 整段下放，返回「要转发的节点身份」或「入队结果」；`push_scan` 的网络转发仍 await |

**业务语义未变**：扫描入队与转发口径、字幕与播放地址规则、签到 / 兑换 / 退款 / 履约的判定、
通知文案与顺序都保持原样——只是这些活儿不再压在事件循环上。

## 四、顺带收口的三个口径

- `economy._fulfill_order` 同步化之后，人工补单（`admin.py`）与回调（`economy.payment_notify`）
  都用 `run_in_threadpool` 调它；`scripts/smoke_test_economy.py` / `smoke_test_refunds.py`
  里那两个直调点也跟着改成同步调用（否则会拿到一个没 await 的协程）。
- 管理端审计门禁（`scripts/check_admin_audit_coverage.py`）现在**跟一层委托**：端点用
  `await run_in_threadpool(_helper, ...)` 把整段写库（**含审计**）交给本模块函数时不再误报；
  但被调用函数里也没写 `_audit(...)` 的照旧会被点名（自检里有正反两个样本）。
- 同一门禁的审计判定改成语边界匹配：名字以 `_audit` 结尾的普通函数
  （`_worker_without_audit(`）不再被当成「写了审计」。

## 五、验证

```bash
# 阻塞路由：真实代码树上 0 条，基线 0 条
python scripts/check_blocking_routes.py

# 护栏自检（进 CI）：真实树 0 条 + 合成树必须失败 + 「委托写法」正反样本
python scripts/smoke_test_static_guards.py

# await 契约（改同步 def 之后别处还在 await 它）
python scripts/check_await_consistency.py

# 与改动直接相关的冒烟（实测通过项数）
python scripts/smoke_test_economy.py          # 19
python scripts/smoke_test_refunds.py          # 76
python scripts/smoke_test_concurrency.py      # 39
python scripts/smoke_test_coupons.py          # 109
python scripts/smoke_test_reminders.py        # 50
python scripts/smoke_test_backend_hot_paths.py # 44
python scripts/smoke_test_admin_audit.py      # 59
python scripts/smoke_test_emby.py             # 主链路
python -m pytest tests/ -q                    # 112
```

阻塞路由基线：24 → **0**（`scripts/check_blocking_routes.py`，`BASELINE` 现在是空集）。
