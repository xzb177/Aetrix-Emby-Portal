"""302 Next E3：BYOS 选路优先（纯函数，只表达门禁，不改播放行为）。

这是 #1405 门禁的选路表达，不是第二套选路逻辑：
- 绑定用户有本人 ready 副本 → 播用户副本（第一优先级，永远在前）。
- 绑定用户无副本 → 绝不播共享原件：按状态走转存中 / 失败 /
  额度引导 / 转存引导，落点都是 409/503 语义，与 #1405 的
  ``quota_playback_decision``（绑定用户 failover 恒 False）一致。
- 未绑定用户 → 本函数不裁决播放（已下发签名仍走旧回落是旧链的事），
  只返回 ``bind_guide``（去绑定引导）。

本模块不碰数据库、不发请求、不改任何线上播放路径；线上播放仍由
``api.py`` 的既有门禁裁决。它的结果只通过已设开关的 E4 状态口
对外展示（解释“会怎么播”），以及被测试锁定——任何一次重构只要
让绑定用户无副本时返回了共享原件，就是回归，测试会红。
"""
from __future__ import annotations

ROUTE_USER_COPY = "user_copy"
ROUTE_PREPARING = "preparing"
ROUTE_FAILED = "failed"
ROUTE_QUOTA_GUIDE = "quota_guide"
ROUTE_NO_COPY_GUIDE = "no_copy_guide"
ROUTE_STALE_COPY = "stale_copy"
ROUTE_BIND_GUIDE = "bind_guide"

# 绑定用户永远不许走的路由：这里根本没有 "shared" 这一档——
# 选路函数连表达“播共享原件”这个选项都不提供，绕路无从写起。
_ROUTES = (
    ROUTE_USER_COPY, ROUTE_PREPARING, ROUTE_FAILED, ROUTE_QUOTA_GUIDE,
    ROUTE_NO_COPY_GUIDE, ROUTE_STALE_COPY, ROUTE_BIND_GUIDE,
)


def decide_route(*, bound: bool, has_user_ready_copy: bool,
                 user_copy_stale: bool = False,
                 user_job_status: str | None = None,
                 quota_hit: bool = False) -> dict:
    """给定门禁事实，返回选路裁决（纯函数）。

    ``user_job_status``：用户归属桶最新转存任务的 status
    （pending / running / done / failed / None）。
    返回 ``{"route": ..., "shared": False}``：``shared`` 恒为 False，
    表示本裁决永远不授权播放共享原件——绑定用户无副本时是引导，
    不是回落。
    """
    if not bound:
        return {"route": ROUTE_BIND_GUIDE, "shared": False,
                "detail": "未绑定：只能浏览，去绑定自己的盘后才能点播"}
    if has_user_ready_copy:
        # 有一张可播的副本就播它：同条目下残留的失效行不挡播放。
        return {"route": ROUTE_USER_COPY, "shared": False,
                "detail": "播用户自己盘的 ready 副本"}
    if user_job_status in ("pending", "running"):
        # 新转存已在路上：失效旧副本不重要，等这单转完。
        return {"route": ROUTE_PREPARING, "shared": False,
                "detail": "转存进行中：转完才可播"}
    if user_copy_stale:
        # 没有可播副本、也没有在途任务，但有一张死副本：说清失效，
        # 引导重新转存，而不是笼统报任务失败。
        return {"route": ROUTE_STALE_COPY, "shared": False,
                "detail": "用户副本已失效：重新转存后再播，不播共享原件"}
    if user_job_status == "failed":
        return {"route": ROUTE_FAILED, "shared": False,
                "detail": "转存失败：不播共享原件，请重试或联系管理员"}
    if quota_hit:
        return {"route": ROUTE_QUOTA_GUIDE, "shared": False,
                "detail": "共享原件额度爆了：先转存到自己的盘再播"}
    return {"route": ROUTE_NO_COPY_GUIDE, "shared": False,
            "detail": "还没有自己的副本：先转存到自己的盘再播"}
