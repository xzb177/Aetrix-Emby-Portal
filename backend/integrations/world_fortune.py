"""能力：探险世界 · 每日抽一签签文池

签文池后台可配：一行一条，格式「等级 | 签文 | 宜 | 忌」。
等级取值：上上签 / 上签 / 中签 / 下签。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from backend.integrations import store

ALLOWED_LEVELS = ("上上签", "上签", "中签", "下签")

DEFAULT_POOL = """上上签 | 今晚的片单注定封神 | 连刷三集 | 剧荒焦虑
上上签 | 下饭神剧正在向你招手 | 边吃边看 | 空腹观影
上上签 | 今晚刷到的都是宝藏 | 大胆开新剧 | 犹豫不决
上上签 | 压箱底的好片该见天日了 | 翻出老片 | 只看预告
上上签 | 熬夜也值得的好结局 | 一口气看完 | 中途弃剧
上上签 | 这个周末注定不无聊 | 囤好零食 | 出门社交
上签 | 适合补一部口碑老剧 | 听朋友推荐 | 跟风差评
上签 | 今晚宜看轻松喜剧 | 笑出声来 | 较真剧情
上签 | 悬疑剧今晚有新线索 | 关灯观看 | 快进跳过
上签 | 适合重温经典片段 | 二刷名场面 | 考据细节
上签 | 纪录片能治好你的浮躁 | 静心观看 | 倍速刷完
上签 | 今晚的电影不会踩雷 | 相信直觉 | 看评分选片
上签 | 适合和朋友一起看 | 拉人入坑 | 独自emo
上签 | 老电影越看越有味道 | 细品台词 | 玩手机
中签 | 片荒是暂时的 | 先看短片 | 盲目开坑
中签 | 今晚宜整理收藏夹 | 清理待看 | 越囤越多
中签 | 适合看一集就睡 | 定时关闭 | 再看亿集
中签 | 恐怖片留到白天 | 开灯观看 | 半夜挑战
中签 | 长剧可以慢慢追 | 每天两集 | 一夜刷穿
中签 | 适合补番外和小剧场 | 放松心情 | 补设定集
中签 | 今晚看什么都行 | 随缘点开 | 选择困难
中签 | 适合看治愈系动画 | 早点休息 | 熬夜补番
中签 | 经典台词值得回味 | 记下金句 | 走马观花
中签 | 今晚宜早睡 | 明日再战 | 熬夜三集
下签 | 今晚诸事不宜追新剧 | 先看简介 | 闭眼入坑
下签 | 忌深夜看悲剧 | 备好纸巾 | 硬扛到底
下签 | 忌边看边刷手机 | 专心看完 | 一心二用
下签 | 忌熬夜刷悬疑 | 明天再看 | 通宵破案
下签 | 忌跟风看烂片 | 先看口碑 | 为骂而看
下签 | 忌空腹看美食片 | 先吃饱 | 深夜放毒"""

SPEC = {
    "title": "每日抽一签 · 签文池",
    "desc": "签文池后台可配：一行一条，格式「等级 | 签文 | 宜 | 忌」。等级取值为：上上签 / 上签 / 中签 / 下签。",
    "group": "探险世界",
    "docs_hint": "每行格式：等级 | 签文 | 宜 | 忌，例如：上上签 | 今晚必有好剧相伴 | 连刷三集 | 熬夜",
    "fields": [
        {"key": "fortune_pool", "label": "签文池", "type": "str", "default": DEFAULT_POOL,
         "hint": "一行一条，格式：等级 | 签文 | 宜 | 忌；等级只能是 上上签/上签/中签/下签"},
    ],
}

_FIELD_KEYS = [f["key"] for f in SPEC["fields"]]
_FIELD_DEFAULTS = {f["key"]: (f.get("default") or "") for f in SPEC["fields"]}


def read(db: Session) -> dict:
    """一次查询取回签文池配置"""
    return store.read_values(db, _FIELD_KEYS, _FIELD_DEFAULTS)


def parse_pool(raw: str) -> list[dict]:
    """解析签文池文本：逐行按 | 切成 等级/签文/宜/忌；非法行跳过。"""
    out: list[dict] = []
    for line in str(raw or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 4:
            continue
        level, text, yi, ji = parts
        if level not in ALLOWED_LEVELS or not text:
            continue
        out.append({"level": level, "text": text, "yi": yi, "ji": ji})
    return out
