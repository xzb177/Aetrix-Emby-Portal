"""外部服务能力中心

**只提供能力，不带任何内置凭据**：端点、密钥、账号一律由管理员在后台自己填，填完即可用
（每个能力都带「测试连接」）。项目本身不内置任何 AI key / SMTP 密码 / 地图 key。

架构：一个能力一个模块，模块暴露三样东西（后两样可选）：

- ``SPEC``：能力元数据与字段表（``fields`` 决定后台表单长什么样，前端不写死任何字段）；
- ``apply(db)``：保存后需要落地的副作用（例如代理要写进进程环境变量）；
- ``test(db, payload)``：真实连通性测试，成功与否与失败原因原样回给管理员。

配置统一落在 ``system_configs``（沿用既有配置表，多机部署共享同一套配置）。密钥类字段
（``secret``）读回来只报「已配置」，前端提交 ``******`` 表示不修改、**清空表示删除**——
与经济设置的约定一致。

读配置走 ``store.read_values``：一次 IN 查询取回一批键。原先每个能力各自按 key 查一次，
人机验证挂件一次要 5 次往返、AI 一次问答要 4 次；现在总览整页只要 1 次。
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from backend.integrations import store

logger = logging.getLogger(__name__)

MASK = "******"
FIELD_TYPES = ("str", "secret", "int", "bool", "select")

# 能力清单：顺序即后台展示顺序；模块里再声明 title / desc / group
CAPABILITY_MODULES: tuple[str, ...] = (
    "proxy", "captcha", "mail", "telegram", "ai", "geoip", "branding",
    "world_board", "world_fortune",
)


def _module(slug: str):
    if slug not in CAPABILITY_MODULES:
        raise KeyError(slug)
    return importlib.import_module(f"backend.integrations.{slug}")


def _spec(slug: str) -> dict:
    return dict(_module(slug).SPEC)


def _coerce(field: dict, raw: Any) -> str:
    """按字段声明把前端传来的值规整成配置表里的字符串"""
    kind = field.get("type", "str")
    if kind == "bool":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        return "true" if str(raw).strip().lower() in ("true", "1", "on", "yes") else "false"
    if kind == "int":
        try:
            return str(int(str(raw).strip() or 0))
        except (TypeError, ValueError):
            return str(int(field.get("default") or 0))
    text = "" if raw is None else str(raw).strip()
    if kind == "select":
        options = [str(o) for o in field.get("options") or []]
        return text if text in options else str(field.get("default") or "")
    return text


def _defaults_for(fields: Iterable[dict]) -> dict[str, str]:
    return {f["key"]: ("" if f.get("default") is None else str(f["default"])) for f in fields}


def raw_values_many(db: Session, slugs: Iterable[str]) -> dict[str, dict]:
    """一次查询取回多个能力的原始值（含密钥明文，仅供后端内部使用）"""
    specs = {slug: _spec(slug) for slug in slugs}
    defaults: dict[str, str] = {}
    for spec in specs.values():
        defaults.update(_defaults_for(spec["fields"]))
    values = store.read_values(db, list(defaults), defaults)
    return {
        slug: {f["key"]: values[f["key"]] for f in spec["fields"]}
        for slug, spec in specs.items()
    }


def raw_values(db: Session, slug: str) -> dict:
    """读单个能力的原始值（含密钥明文，仅供后端内部使用）"""
    return raw_values_many(db, [slug])[slug]


def mask_values(slug: str, values: dict) -> dict:
    """对外序列化：密钥只报「是否已配置」"""
    out: dict = {}
    for field in _spec(slug)["fields"]:
        value = values.get(field["key"], "")
        if field.get("type") == "secret":
            out[field["key"]] = MASK if value else ""
        else:
            out[field["key"]] = value
    return out


def is_enabled(values: dict, slug: str) -> bool:
    """能力当前是否生效

    模块可自定义判定（例如人机验证要看「选没选提供方」，而不是某个 ``*_enabled`` 布尔），
    没自定义时按字段表里第一个 ``*_enabled`` 布尔判。
    """
    module = _module(slug)
    checker = getattr(module, "is_enabled", None)
    if callable(checker):
        return bool(checker(values))
    for field in _spec(slug)["fields"]:
        if field["key"].endswith("_enabled"):
            return str(values.get(field["key"], "")).lower() == "true"
    return True


def is_configured(slug: str, values: dict) -> bool:
    """是否「配齐了」：模块可自定义判定（如 GeoIP 两种提供方任选其一）

    只看字段是否齐全，不看开关：后台用「已配置 + 是否启用」两个维度给卡片打标，
    所以「关了但配过」与「压根没填」必须能区分开。
    """
    module = _module(slug)
    checker = getattr(module, "is_configured", None)
    if callable(checker):
        return bool(checker(values))
    # 默认：非密钥字段里标了 required 的都要有值
    for field in _spec(slug)["fields"]:
        if field.get("required") and not str(values.get(field["key"]) or "").strip():
            return False
    return True


def _card(db: Session, slug: str, spec: dict, values: dict) -> dict:
    module = _module(slug)
    return {
        "slug": slug,
        "title": spec.get("title", slug),
        "desc": spec.get("desc", ""),
        "group": spec.get("group", "外部服务"),
        "docs_hint": spec.get("docs_hint", ""),
        "enabled": is_enabled(values, slug),
        "configured": is_configured(slug, values),
        "fields": mask_values(slug, values),
        "test_label": spec.get("test_label", "测试连接"),
        # 没有 test() 的能力（例如站点与品牌：只是一组设置，没有可测的外部依赖）
        # 后台不显示「测试」按钮——按钮点了只会回一句「不支持测试」，那是界面在骗人
        "testable": callable(getattr(module, "test", None)),
    }


def list_capabilities(db: Session) -> list[dict]:
    """能力总览（后台「系统设置」中心页用）"""
    specs = {slug: _spec(slug) for slug in CAPABILITY_MODULES}
    all_values = raw_values_many(db, CAPABILITY_MODULES)
    return [_card(db, slug, specs[slug], all_values[slug]) for slug in CAPABILITY_MODULES]


def get_capability(db: Session, slug: str) -> dict:
    spec = _spec(slug)
    values = raw_values(db, slug)
    return {"spec": spec, "item": _card(db, slug, spec, values),
            "values": mask_values(slug, values)}


def save_capability(db: Session, slug: str, values: dict) -> dict:
    """保存能力配置；保存后调用 apply 落地副作用

    密钥字段的三条规矩（前端把已配好的密钥预填成 ``******``，所以这三种情况不会撞车）：

    - 字段**没提交**（键不存在）或值为 ``None`` → 不修改；
    - 值是掩码 ``******`` → 不修改；
    - 值是空串 → **清空已保存的密钥**（用户手动删掉了它）。

    第三条是必要的：否则密钥只换不撤——代理密码改了、Bot Token 不想再用了，都没办法删掉。
    """
    spec = _spec(slug)
    fields = {f["key"]: f for f in spec["fields"]}
    pending: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    for key, raw in (values or {}).items():
        field = fields.get(key)
        if field is None:
            continue
        if field.get("type") == "secret" and (raw is None or str(raw) == MASK):
            continue
        pending[key] = _coerce(field, raw)
        descriptions[key] = f"{spec.get('title', slug)} · {field.get('label') or ''}"
    if pending:
        store.write_values(db, pending, descriptions)
    db.commit()
    apply_capability(db, slug)
    return get_capability(db, slug)


def apply_capability(db: Session, slug: str) -> None:
    """落地副作用；失败只记日志，不能让保存动作整体失败"""
    module = _module(slug)
    hook = getattr(module, "apply", None)
    if not callable(hook):
        return
    try:
        hook(db)
    except Exception as exc:  # noqa: BLE001 — 例如代理不可达、通知渠道刷新失败
        logger.warning("能力 %s 的 apply 失败（可忽略）: %s", slug, exc)


def apply_all(db: Session) -> None:
    """启动时把所有能力落地一遍（目前只有代理与通知渠道需要：代理要写回环境变量）"""
    for slug in CAPABILITY_MODULES:
        apply_capability(db, slug)


def test_capability(db: Session, slug: str, payload: Optional[dict] = None) -> dict:
    """真实连通性测试（走真实网络/真实投递，不写配置）"""
    module = _module(slug)
    hook = getattr(module, "test", None)
    if not callable(hook):
        return {"ok": False, "message": "该能力不支持测试"}
    try:
        result = hook(db, payload or {})
    except Exception as exc:  # noqa: BLE001 — 测试就是把真实原因报出来
        logger.info("能力 %s 测试失败: %s", slug, exc)
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {"ok": bool(result), "message": ""}
    result.setdefault("ok", False)
    result.setdefault("message", "")
    return result
