"""想看商店求片 R1：登记 + 榜单 + 入库联动（契约层）。

只钉形状和文案，不访问数据库、不入队转存、不自动寻源。

R1 定义（302 Next 设计文档 B4，Yunhai 待确认项 Q3 仍以本文为准）：
- 登记：会员为库外条目（tmdb 对不上库内）登记求片。登记≠能播，
  入库前文案必须明说还没有、不能播；不自动下片、不自动寻源。
- 榜单：按登记人数（附议数）排榜，会员可见；管理端可见全部登记。
- 入库联动：条目入库后，对得上的登记自动标为已入库、可走正常
  想看流程。联动只做到“可想看”状态翻转：不静默转存、不自动建
  转存任务、不发通知（通知通道待确认）。

与既有求片系统（MediaSeek）的关系：
- 不另建表：复用 ``movie_requests``（一行 = 一部片子的求片）与
  ``movie_request_votes``（一人一票）。R1 登记 = 创建或附议：
  同一 tmdb 已有处理中行时不重复建行，只附议计人数。
- 状态口径复用 MediaSeek：pending=已登记、approved=寻源中、
  completed=已入库、rejected=找不到、withdrawn=已撤回。
  管理端改状态（批准/拒绝/标记已入库）走既有 MediaSeek 入口，
  本模块不另开写入口；管理端榜单只读。
- 门槛只取商店 B2 同款（会员），不套 MediaSeek 的每日/月度额度
  与 seek_enabled/vote_enabled 开关：商店 R1 只受 STORE_ENABLED
  总闸 + 会员闸管（见 store_requests.py），差异已写明，免得
  两边开关打架时查无对证。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: R1 展示态 → MediaSeek 原始 status。
R1_STATE_TO_STATUS = {
    "registered": "pending",
    "seeking": "approved",
    "stored": "completed",
    "not_found": "rejected",
    "withdrawn": "withdrawn",
}

#: MediaSeek 原始 status → R1 展示态。未知 status 原样回，不编造。
STATUS_TO_R1_STATE = {v: k for k, v in R1_STATE_TO_STATUS.items()}

#: R1 展示态中文文案。
R1_STATE_LABELS = {
    "registered": "已登记",
    "seeking": "寻源中",
    "stored": "已入库",
    "not_found": "找不到",
    "withdrawn": "已撤回",
    "in_library": "已在库",
}

#: 求片登记成功（新行）：入库前不能播，入库后只翻状态、不自动转存。
REQUEST_REGISTERED_MESSAGE = (
    "已登记求片。片子还没入库，现在不能播；入库后这里会标为已入库，"
    "到时再点想看（不会自动转存）。"
)

#: 已有处理中行，登记计入人数。
REQUEST_VOTED_MESSAGE = "这部片子已经有人登记了，你的登记已计入人数。"

#: 同一人重复登记：人数不重复算。
REQUEST_ALREADY_REGISTERED_MESSAGE = "你已经登记过这部片子了，人数没有重复算。"

#: tmdb 已在库：不登记，直接去想看。
REQUEST_IN_LIBRARY_MESSAGE = "这部片子已经在库里了，直接去想看，不用登记求片。"

#: tmdb_id 校验失败（normalize_tmdb_id 只收纯数字）。
REQUEST_INVALID_TMDB_MESSAGE = "tmdb_id 只要纯数字。"

#: 片名过长。
REQUEST_TITLE_TOO_LONG_MESSAGE = "片名太长（最多 255 字）。"


class RequestIn(BaseModel):
    """POST /requests 的请求体。title 可选：搜索页带过来就存，

    没带就以 tmdb_id 占位（movie_name 列非空），管理端后续可补。
    """

    tmdb_id: str = Field(min_length=1, max_length=32)
    title: str | None = Field(default=None, max_length=255)


class RequestRecord(BaseModel):
    """一条求片记录（我的列表 / 榜单 / 管理端榜单同形）。

    request_id 为 None 只出现在“已在库”这一支：没建登记行，
    客户端应引导去想看而不是展示一条求片。
    """

    request_id: int | None
    tmdb_id: str
    movie_name: str | None = None
    vote_count: int = 0
    #: MediaSeek 原始 status（pending/approved/completed/rejected/withdrawn）。
    status: str
    #: R1 展示态（registered/seeking/stored/not_found/withdrawn/in_library）。
    state: str
    in_library: bool = False
    library_item_id: int | None = None
    #: 当前用户是否已登记/附议过（榜单打标用；管理端恒 False）。
    voted: bool = False
    message: str = ""


class RequestListResponse(BaseModel):
    """GET /requests（我的）/ GET /requests/board（榜单）/ 管理端榜单。"""

    items: list[RequestRecord] = Field(default_factory=list)
