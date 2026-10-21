"""EA · Emby API 启动器（与 EM 面板分开部署）

一体化说明见 docs/deploy-ea.md。

用法:
    python serve_emby.py                      # 默认 0.0.0.0:8001
    EMBY_API_PORT=9001 python serve_emby.py   # 指定端口

前置条件（缺任一项启动即被拒绝）：
    - 与 EM 相同的 SECRET_KEY（客户端 token 由 EM 签发、由 EA 校验）
    - 与 EM 相同的 DATABASE_URL（用户、媒体库与策略都在 EM 那边写入）
"""
import logging
import os

import uvicorn

if __name__ == "__main__":
    try:
        from backend.domain_guard import uvicorn_proxy_kwargs

        proxy_kwargs = uvicorn_proxy_kwargs()
    except Exception:
        logging.getLogger(__name__).warning(
            "构造代理头信任名单失败，uvicorn 只信回环", exc_info=True
        )
        proxy_kwargs = {"proxy_headers": True, "forwarded_allow_ips": "127.0.0.1,::1"}
    uvicorn.run(
        "emby_api.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("EMBY_API_PORT", "8001")),
        log_level=os.getenv("LOG_LEVEL", "info"),
        # 单进程：转码子进程管理与内存态会话在同一进程内闭环
        workers=1,
        # 显式传入，避免 uvicorn 自己读到 FORWARDED_ALLOW_IPS=*
        **proxy_kwargs,
    )
