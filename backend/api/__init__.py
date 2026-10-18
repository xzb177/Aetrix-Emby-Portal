"""
API 路由包
包含用户端和管理后台的所有路由
"""
from backend.api.user import user_router
# 管理后台按域拆成多个模块，共用 admin_core 里的同一个 admin_router：
# 导入顺序 = 拆分前的端点定义顺序（认证 → 业务 → 经济/统计），保持一致以免路由顺序变化。
from backend.api import admin_auth  # noqa: F401 — 导入即注册
from backend.api.admin import admin_router
from backend.api import admin_economy  # noqa: F401 — 导入即注册
from backend.api import admin_scrape  # noqa: F401 — 导入即注册（元数据与刮削）
from backend.api import admin_user_grants  # noqa: F401 — 导入即注册（用户授权资源卡片）
from backend.api import welfare_admin  # noqa: F401 — 导入即注册（公益服管理后台）
from backend.api import admin_dashboard  # noqa: F401 — 导入即注册（仪表盘聚合）

__all__ = ["user_router", "admin_router"]
