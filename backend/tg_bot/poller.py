"""TG Bot 长轮询：getUpdates 接收消息，offset 持久化。"""
from __future__ import annotations

import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)

# update 分发队列（poller 线程只做 IO，业务在工作线程执行）
_UPDATE_QUEUE: queue.Queue = queue.Queue(maxsize=1000)

# getUpdates offset 持久化键（SystemConfig）
_OFFSET_KEY = "tg_bot_update_offset"
# poller 心跳（SystemConfig，ISO 时间）：API 进程据此判断 poller 是否在跑，
# 在跑时网页端绑定校验不得自己调 getUpdates（会与长轮询 409 冲突并抢走 update）
HEARTBEAT_KEY = "tg_bot_poller_heartbeat"
# 最后一次轮询错误（SystemConfig，ISO 时间 + 错误摘要）：管理端据此看到
# poller 是“跑着但报错”还是“跑着且正常”。成功后清掉，不留过期报错吓人。
LAST_ERROR_KEY = "tg_bot_poller_last_error"
# 心跳新鲜度阈值：长轮询 30s + 请求超时 10s + 最大退避 300s，留余量
HEARTBEAT_STALE_SECONDS = 360


# 启动时注册的 Bot 命令菜单（客户端左下角 / 菜单可见）。
# 与 router 命令表对照：路由新增用户命令时须同步补齐；
# /me、/renew 分别为 /account、/login 的别名，描述与行为一致。
# 描述统一带场景标签前缀（学 embyboss 命令表）：[群/私聊] = 群里与
# 私聊都能用（与 command_scopes.GROUP_COMMANDS 逐条一致）；[私聊] =
# 群里不执行（router private_only / P1 个人查询 / redeem 群内拒执行）。
# 标签只改说明文案、不改任何命令行为；改 router 拦截口径时必须同步
# 改这里的标签与 GROUP_COMMANDS，有双向断言在
# tests/test_tg_bot_slots_command.py 防漂移。
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "[群/私聊] 欢迎与快捷入口"),
    ("help", "[群/私聊] 帮助"),
    ("bind", "[群/私聊] 绑定 Telegram"),
    ("checkin", "[群/私聊] 每日签到"),
    ("points", "[群/私聊] 查能量"),
    ("account", "[群/私聊] 账号中心"),
    ("me", "[群/私聊] 账号中心"),
    ("request", "[私聊] 求片"),
    ("myrequests", "[私聊] 我的求片"),
    ("rank", "[群/私聊] 能量排行榜"),
    ("govlog", "[私聊] 治理审计日志"),
    ("level", "[群/私聊] 会员等级"),
    ("report", "[私聊] 举报求片"),
    ("propose", "[私聊] 活动提案"),
    ("launch", "[私聊] 直接发起活动"),
    ("requestboard", "[群/私聊] 求片榜"),
    ("rules", "[群/私聊] 群规"),
    ("login", "[私聊] 快捷登录"),
    ("renew", "[私聊] 快捷登录"),
    ("redpacket", "[群/私聊] 发红包"),
    ("lottery", "[群/私聊] 抽奖"),
    ("slots", "[群/私聊] 老虎机"),
    ("redeem", "[私聊] 兑换码兑换"),
    ("chatpoints", "[群/私聊] 群发言能量查询"),
    ("bindings", "[私聊] 查网盘绑定"),
    ("transfers", "[私聊] 查转存进度"),
    ("whyplay", "[私聊] 为什么播不了"),
    ("wants", "[私聊] 查想看"),
    ("reconcile", "[私聊] 群号一致性对账（管理员）"),
    ("rankpush", "[私聊] 推送观影榜（管理员）"),
    ("whois", "[私聊] 账号反查（管理员）"),
]


def _beat(db) -> None:
    """写心跳（失败忽略，不影响轮询）。"""
    from datetime import datetime
    from backend.integrations import store
    try:
        store.write_values(db, {HEARTBEAT_KEY: datetime.now().isoformat()})
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.debug("tg poller heartbeat failed: %s", exc)


def _record_poll_error(db, exc: Exception) -> None:
    """记最后一次轮询错误（失败忽略，不影响轮询）。"""
    from datetime import datetime
    from backend.integrations import store
    try:
        summary = f"{datetime.now().isoformat()} {exc}"[:300]
        store.write_values(db, {LAST_ERROR_KEY: summary})
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.debug("tg poller last-error record failed", exc_info=True)


def _clear_poll_error(db) -> None:
    """清掉已恢复的报错（失败忽略，不影响轮询）。"""
    from backend.integrations import store
    try:
        store.write_values(db, {LAST_ERROR_KEY: ""})
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.debug("tg poller last-error clear failed", exc_info=True)


def is_active(db) -> bool:
    """poller 是否在跑（心跳在阈值内）。可在任意进程调用。"""
    from datetime import datetime
    from backend.integrations import store
    raw = store.read_value(db, HEARTBEAT_KEY, "") or ""
    try:
        last = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    return (datetime.now() - last).total_seconds() < HEARTBEAT_STALE_SECONDS


def _get_offset(db) -> int:
    """从 SystemConfig 读取 offset。"""
    from backend.integrations import store
    try:
        # 直读 DB（不走 get_value 的 TTL 热缓存），避免读到旧 offset 重放 update
        return int(store.read_value(db, _OFFSET_KEY, "0") or "0")
    except (ValueError, TypeError):
        return 0


def _save_offset(db, offset: int) -> None:
    """持久化 offset 到 SystemConfig。"""
    from backend.integrations import store
    # 注意：store 只有 write_values，没有 set_value
    store.write_values(db, {_OFFSET_KEY: str(offset)})
    # write_values 不提交：这里必须自己 commit，否则 close 时回滚，offset 永不推进
    db.commit()


def _poll_once(token: str, offset: int, timeout: int = 30) -> list:
    """单次 getUpdates 长轮询，返回 updates 列表。"""
    import httpx

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    # allowed_updates 必须显式列全：chat_member/my_chat_member 不列就永远
    # 收不到。chat_member 是退群处置的事件源——注意 Telegram 只在 bot 为
    # 群管理员时才稳定推送他人的成员变动，非管理员场景可能收不到
    # （2026-10-30 接入时真机未验）；my_chat_member（bot 自身入群/被踢）
    # 一并订阅，router 显式忽略不处理。inline_query 为内联搜片所用。
    # 键必须是字符串字面量：2026-10-30 实证 #1527 变基时引号被传
    # 输层吃掉、写成 {offset: offset, ...}（键是变量本身），请求只带
    # "758207xxx=758207xxx&30=30"，offset/timeout 从未到达 Telegram、
    # update 永不被服务器确认，重启即整批重投递刷屏。回归锁在
    # tests/test_tg_bot_poller_redelivery.py（断言键为 str 且值对上）。
    params = {"offset": offset, "timeout": timeout,
              "allowed_updates": ["message", "callback_query", "inline_query",
                                  "chat_member", "my_chat_member"]}
    with httpx.Client(timeout=timeout + 10) as client:
        resp = client.get(url, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"getUpdates HTTP {resp.status_code}")
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"getUpdates API error: {body.get('description')}")
    return body.get("result") or []


def _worker_loop() -> None:
    """工作线程：从队列取 update 并分发。"""
    from backend.database import SessionLocal
    from backend.tg_bot import router

    while True:
        update = _UPDATE_QUEUE.get()
        if update is None:  # 退出信号
            _UPDATE_QUEUE.task_done()
            break
        db = None
        try:
            db = SessionLocal()
            router.dispatch(db, update)
        except Exception as exc:  # noqa: BLE001
            logger.error("tg worker dispatch failed: %s", exc, exc_info=True)
        finally:
            if db is not None:
                db.close()
            # 必须 task_done：poller 靠 join() 等这一批处理完才推进 offset
            _UPDATE_QUEUE.task_done()


def _process_updates(updates: list, offset: int, save) -> int:
    """入队一批 update，等它们全部处理完再保存 offset（at-least-once）。

    以前入队即推进 offset：进程崩溃或队列满（put_nowait 直接丢弃）都会永久丢 update。
    现在队列满就阻塞等待；全部 handler 跑完才 save。崩溃会重放这一批：
    抢红包（uq_rpc_packet_user）、兑换码（uq_code_redemption_user）、群发言能量
    （uq_chat_points_msg）、绑定码（used_at）有库级幂等；发红包 /redpacket 重放可能重复（待补）。
    """
    max_id = offset
    skipped = 0
    for u in updates:
        uid = u.get("update_id")
        if isinstance(uid, int):
            if uid < offset:
                # 已提交水位之下的 update：Telegram 重投递的旧批，
                # 绝不二次分发 handler（2026-10-30 刷屏教训：参数
                # 失效时整批反复重投，靠本地水位兜底也绝不刷屏）。
                # 本批提交后水位推进，重投的同批从此全部命中过滤。
                skipped += 1
                continue
            if uid >= max_id:
                max_id = uid + 1
        _UPDATE_QUEUE.put(u)  # 阻塞：宁可慢，不丢
    if skipped:
        logger.warning(
            "tg poller: skipped %s already-committed updates (offset=%s)",
            skipped, offset)
    _UPDATE_QUEUE.join()
    save(max_id)
    return max_id


def poll_forever() -> None:
    """主循环：长轮询 getUpdates。token 未配置则直接返回。"""
    from backend.database import SessionLocal
    from backend.integrations.telegram import token as get_token
    from backend.integrations import store

    # 分发由下面 4 个手写 _worker_loop 线程承担（r30 N6：删掉建而未用的线程池）
    for _ in range(4):
        t = threading.Thread(target=_worker_loop, daemon=True, name="tg-bot-worker")
        t.start()

    backoff = 5
    # P0：是否已记过报错；恢复后才清，避免每轮都写一次 SystemConfig
    error_recorded = False

    # 启动时注册一次 Bot 命令菜单（客户端「菜单」按钮可见）；幂等，失败不影响轮询
    # 三级下发（command_scopes）：default 只含普通命令、管理员私聊全量、
    # 名单群只含群内可执行命令；单级失败只记日志，不拖垮轮询启动
    try:
        from backend.tg_bot import command_scopes

        _db = SessionLocal()
        try:
            command_scopes.register_command_scopes(_db, BOT_COMMANDS)
        finally:
            _db.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("tg setMyCommands skipped: %s", exc)

    while True:
        db = SessionLocal()
        try:
            bot_token = get_token(db)
            if not bot_token:
                logger.warning("tg_bot_poller: token 未配置，线程退出")
                return
            # r40：删除 ``tg_bot_commands_enabled`` 死开关检查。该键在全产品无任何
            # 写入通道（无 schema、无管理端点、无后台 UI），恒为默认值 "1"。
            # Bot 总开关只许一套：``bot_enabled``（welfare 家族，有后台 UI，
            # 关闭后 router 层静默丢弃 update，见 tg_bot/router.py）。
            offset = _get_offset(db)
            _beat(db)
            # P0：上一轮报错、本轮心跳已恢复 → 清掉旧报错
            if error_recorded:
                _clear_poll_error(db)
                error_recorded = False
        finally:
            db.close()

        try:
            updates = _poll_once(bot_token, offset, timeout=30)
            backoff = 5  # 成功后重置退避
            if updates:
                def _persist(max_id: int) -> None:
                    db2 = SessionLocal()
                    try:
                        _save_offset(db2, max_id)
                    finally:
                        db2.close()

                # 处理完才持久化 offset（at-least-once）
                _process_updates(updates, offset, _persist)
        except Exception as exc:  # noqa: BLE001
            logger.error("tg poll failed: %s, retry in %ss", exc, backoff)
            # P0：报错写进 SystemConfig，管理端不用翻日志也能看到
            try:
                _db = SessionLocal()
                try:
                    _record_poll_error(_db, exc)
                finally:
                    _db.close()
            except Exception:  # noqa: BLE001
                logger.debug("tg poll error record skipped", exc_info=True)
            error_recorded = True
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
