#!/usr/bin/env python3
"""移动云盘 139 sidecar 进程（FMBY 挂载-4 照抄的部署形态）。

UDS 服务薄壳：传输/握手/会话一律走共享契约
``backend.emby_server.provider_runtime.ProviderRuntimeServer``（115 那一路
在 PR #1251 立定），139 自己的逻辑只在
``backend.emby_server.mount_yun139.Yun139SidecarHandler`` 里。

用法（部署者起进程，不由后端自动拉起；默认关与提供者开关同一口径）：
    YUN139_ENABLED=1 python scripts/yun139_sidecar.py [--sock PATH]

默认 socket = PROVIDER_RUNTIME_SOCK_DIR/yun139/runtime.sock
（PROVIDER_RUNTIME_SOCK_DIR 默认 /run/aetrix/providers，与 115 共用目录
口径、不与 FMBY 学习实例撞路径）。

- 不接真实 139 上游时：health / session / 转存注册表可用，
  list/stat/open_range/resolve_* 报契约的 ``not_implemented``
  （响亮，不回空数据）。真实上游须持实测证据另接。
- 本壳不读任何凭据、不写任何管理页；管理后台 UI 由专线统一做。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.emby_server.mount_yun139 import (  # noqa: E402
    Yun139SidecarHandler,
    yun139_enabled,
)
from backend.emby_server.provider_runtime import (  # noqa: E402
    ProviderRuntimeServer,
    default_socket_path,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="139 sidecar（UDS，共享运行时契约）")
    parser.add_argument("--sock", default=None,
                        help="UDS 地址（默认 PROVIDER_RUNTIME_SOCK_DIR/yun139/runtime.sock）")
    args = parser.parse_args(argv)
    if not yun139_enabled():
        print("YUN139_ENABLED 未开启，sidecar 拒绝启动（默认关）", file=sys.stderr)
        return 2
    sock_path = args.sock or default_socket_path("yun139")
    server = ProviderRuntimeServer(Yun139SidecarHandler(), sock_path)
    print(f"yun139 sidecar listening: {sock_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
