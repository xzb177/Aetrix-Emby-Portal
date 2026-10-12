# -*- coding: utf-8 -*-
"""错误码大白话映射：100 条常见错误 → 中文人话 → 检查步骤。

## 为什么要做

日志和健康页里的原始错误（``[quota] 远端配额耗尽``、``database is locked``、
``Temporary failure in name resolution``）对服主是天书：看得懂字，连不成
「该干什么」。这个模块把 100 条高频错误翻译成「是什么 + 怎么查」的大白话，
供健康报告、探测错误排行、管理后台日志旁展示。

## 用法

>>> from backend.error_lexicon import translate
>>> hint = translate("[quota] 远端配额耗尽（约24h后重试）")
>>> hint["title"]
'Google 云盘配额用完了'

## 设计

- 纯函数、无 DB、无网络、无副作用：任何地方（health_report、probe_worker、
  admin API）都可以安全调用，翻译失败永远不影响主流程。
- ``LEXICON`` 按「具体 → 通用」排序：``translate()`` 返回第一个命中的条目，
  所以 ``远端配额耗尽（HTTP 403）`` 必须排在通用 ``HTTP 403`` 之前。
- 每条 ``pattern`` 都是预编译正则（``re.IGNORECASE``），同时覆盖英文原始
  错误和中文健康消息两种形态。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

__all__ = ["LEXICON", "LEXICON_COUNT", "CATEGORIES", "categories", "translate"]


def _e(
    id: str,
    category: str,
    pattern: str,
    title: str,
    what: str,
    steps: List[str],
) -> Dict[str, Any]:
    return {
        "id": id,
        "category": category,
        "pattern": pattern,
        "rx": re.compile(pattern, re.IGNORECASE),
        "title": title,
        "what": what,
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# 探测（probe）：按需探测 / ffprobe / probe_last_error
# ---------------------------------------------------------------------------
_PROBE = [
    # 解析阶段（resolve）条目排在通用条目之前：阶段信息更精准
    _e(
        "probe_resolve_quota", "probe",
        r"解析探测地址.{0,8}配额|resolve.*quota",
        "解析播放地址时配额用完了",
        "还没开始探测、在准备阶段就发现 Google 配额已空。系统会等配额恢复再试。",
        ["不用管：约 24 小时后自动重试",
         "检查 SA 账号配额分布，看是否需要加账号"],
    ),
    _e(
        "probe_resolve_ratelimit", "probe",
        r"解析探测地址.{0,8}限流|解析.*429|resolve.*ratelimit",
        "解析播放地址时被限流了",
        "准备阶段请求太密被 Google 限流，系统已退避等待。",
        ["不用管：约 24 小时后自动重试", "不要手动频繁重试"],
    ),
    _e(
        "probe_resolve_server", "probe",
        r"解析探测地址.{0,8}服务异常|解析.*5\d\d|resolve.*server",
        "解析播放地址时 Google 抖了",
        "准备阶段遇到 Google 服务端异常，临时性的。",
        ["不用管：约 24 小时后自动重试"],
    ),
    _e(
        "probe_resolve_auth", "probe",
        r"解析探测地址.{0,8}认证失败|解析.*401|resolve.*auth",
        "解析播放地址时钥匙失效了",
        "准备阶段令牌失效，SA 轮换间隙常见，系统会给 2 次重试机会。",
        ["先观察：通常轮换完成后恢复",
         "持续出现：检查 SA 账号状态"],
    ),
    _e(
        "probe_quota", "probe",
        r"\[quota\]|配额耗尽|超出.*配额",
        "Google 云盘配额用完了",
        "今天这个账号从 Google 下载的流量已到上限，Google 暂时不让下了。这是 Google 的限制，不是文件坏了。",
        ["不用管：系统已自动安排约 24 小时后重试，配额恢复后自己会好",
         "如果每天都这样：说明某个账号被用得太狠，去「SA 账号」页看配额分布",
         "急着看：换一集试试，可能走别的账号就通了"],
    ),
    _e(
        "probe_ratelimit", "probe",
        r"\[ratelimit\]|http\s*429|限流|too many requests",
        "请求太频繁，被 Google 限流了",
        "短时间内请求太多，Google 让慢一点。和配额用完不同：限流是「慢下来」，配额是「今天没了」。",
        ["不用管：系统已自动退避约 24 小时后重试",
         "如果频繁出现：检查是不是同一时间有大量探测/扫描在跑，错开高峰",
         "不要手动狂点重试，那样只会限得更久"],
    ),
    _e(
        "probe_server", "probe",
        r"\[server\]|远端服务异常|server.?error|internal.?error",
        "Google 那边服务抖了一下",
        "Google 服务器临时出小差（5xx），通常几分钟到几小时自己恢复，和你的文件无关。",
        ["不用管：系统已自动安排约 24 小时后重试",
         "如果大面积同时出现：去 Google Workspace 状态页确认是不是区域性故障"],
    ),
    _e(
        "probe_auth", "probe",
        r"http\s*401|认证失败|unauthorized|invalid.?credentials",
        "账号钥匙临时失效了",
        "用来读云盘的访问令牌过期或正在轮换，SA 账号切换瞬间偶尔会这样，一般自己会好。",
        ["先观察：系统会自动重试，轮换完成后通常恢复",
         "如果一直 401：去「SA 账号」页检查账号状态，看有没有被 Google 停用",
         "检查服务器时间是否准确，时间差太大会导致令牌校验失败"],
    ),
    _e(
        "probe_no_duration", "probe",
        r"取不到完整时长|完整时长|no.?duration|duration.*unknown|时长.*未知",
        "文件能播，但读不到总时长",
        "这个文件本身没问题、可以正常播放，只是 ffprobe 拿不到它的总时长信息（常见于某些压制格式）。按你的设置，这类文件 3 次尝试后不再折腾，直接标记完成。",
        ["这是预期行为，不是故障：文件照常可播，只是进度条可能不显示总时长",
         "如果想重新试：去探测管理里对该条目点「重试」",
         "如果大量出现：可能是某一批片源格式特殊，不影响播放"],
    ),
    _e(
        "probe_no_media_info", "probe",
        r"有效媒体信息|探测未返回|no.?useful.?data",
        "读不到这个文件的媒体信息",
        "ffprobe 跑完了，但没拿到可用的视频/音频信息。可能是文件头损坏、格式太偏门，或者下载到一半的文件。",
        ["先在网盘里确认这个文件能不能正常打开播放",
         "如果源文件坏了：重新上传一份好的，然后点「重试」",
         "如果文件是好的：记下文件名反馈，可能是 ffprobe 不支持的编码"],
    ),
    _e(
        "probe_resolve_fail", "probe",
        r"无法解析探测地址|resolve.*fail|cannot.?resolve",
        "找不到这个文件的真实地址",
        "系统知道有这个条目，但解析不出它在云盘上的实际下载地址。可能是文件被移动/删除了，或者挂载配置变了。",
        ["去媒体库确认源文件还在不在",
         "检查挂载配置：路径、盘符有没有改过",
         "如果是 .strm：确认 .strm 文件内容没被改坏"],
    ),
    _e(
        "probe_ffprobe_error", "probe",
        r"ffprobe\s*异常|ffprobe.*error|ffprobe.*failed",
        "媒体分析工具出错了",
        "ffprobe（负责读取视频信息的工具）在处理这个文件时抛了异常，可能是文件头损坏或格式不支持。",
        ["确认源文件能正常播放",
         "文件坏了就重新上传，然后点「重试」",
         "偶发一次不用管，3 次后系统会自动标记"],
    ),
    _e(
        "probe_ffprobe_timeout", "probe",
        r"\[timeout\]|ffprobe.*超时|probe.*timeout|timed.?out.*probe",
        "读取媒体信息超时了",
        "ffprobe 读这个文件太慢没能在时限内完成，多半是网络/挂载临时卡住。超时不会直接判死，会走 24 小时长退避再试。",
        ["不用管：约 24 小时后自动重试",
         "如果集中出现：检查挂载/网络是不是在抖"],
    ),
    _e(
        "probe_404", "probe",
        r"http\s*404|地址无效|not.?found.*http|404.*地址",
        "这个地址已经无效了（404）",
        "文件在云盘上找不到了：可能被删了、被移走了，或者分享链接失效。系统不再重试，直接标记失败。",
        ["去网盘确认文件还在不在",
         "文件被删了：从媒体库移除该条目，或重新上传后点「重试」",
         "批量出现：检查挂载根目录是不是被改了"],
    ),
    _e(
        "probe_403_perm", "probe",
        r"http\s*403(?!.*配额)|forbidden(?!.*quota)|权限不足",
        "没有权限读这个文件（403）",
        "Google 拒绝访问：不是配额问题，是账号没权限（比如文件没分享给这个 SA 账号）。",
        ["确认文件已正确分享给 SA 账号",
         "检查 SA 账号有没有被移出共享",
         "团队盘文件：确认 SA 有团队盘访问权"],
    ),
    _e(
        "probe_410", "probe",
        r"http\s*410|gone",
        "文件已被永久删除（410）",
        "Google 明确说这个文件没了，和 404 类似但更确定：删干净了。",
        ["从媒体库移除该条目，或重新上传"],
    ),
    _e(
        "probe_moov", "probe",
        r"moov|无法快进|快进.*失败|seek.*fail",
        "视频索引在文件尾，拖不动进度条",
        "这个视频的 moov（索引数据）在文件末尾，在线播时拖进度条会卡。能播，但体验差。",
        ["不影响正常从头播放",
         "想修：用工具把 moov 移到文件头（faststart），重新上传"],
    ),
    _e(
        "probe_mount_breaker", "probe",
        r"熔断|breaker|连续超时|挂载.*挂死",
        "这个挂载被熔断保护了",
        "某个云盘挂载连续超时，系统把它暂时熔断（暂停使用），防止越卡越死。熔断是保护动作，不是故障本身。",
        ["等它自动恢复：挂载恢复后熔断会自动解除",
         "去「挂载」页看这个挂载的健康状态",
         "如果一直熔断：检查 rclone 挂载进程是不是死了"],
    ),
    _e(
        "probe_stale", "probe",
        r"stale|抢单.*过期|claim.*expir|租约.*过期",
        "探测任务超时没交卷，被回收了",
        "worker 领了探测任务但长时间没完成（卡死或崩溃），系统把任务收回重新排队。这是自愈动作。",
        ["不用管：任务已自动重新排队",
         "如果频繁出现：检查 worker 日志，看是不是 ffprobe 卡住了",
         "也可在后台手动「重置」卡住的条目"],
    ),
    _e(
        "probe_worker_stopped", "probe",
        r"worker.*没有在运行|worker.*停|探测.*暂停|probe.*paused|待探测.*没有",
        "有活要干，但探测 worker 没在跑",
        "队列里有几千条待探测，但负责干活的 worker 线程没起来或被暂停了。",
        ["检查探测开关是不是被关了（设置 → 探测）",
         "去「后台任务」页看 worker 状态，手动启动",
         "刚部署完可能正在启动，等 1-2 分钟再看"],
    ),
    _e(
        "probe_ratio_high", "probe",
        r"探测失败率|probe.*ratio|fail.*ratio",
        "探测失败率偏高",
        "最近探测失败的比例超过了阈值。注意：按你的设置，「取不到时长」这类策略性失败也算在内，不一定是真故障。",
        ["点开「探测错误排行」看失败都集中在哪些错误上",
         "如果集中在「取不到时长」：这是预期策略，不用管",
         "如果集中在配额/限流：等配额恢复，或加 SA 账号"],
    ),
    _e(
        "probe_conn_reset", "probe",
        r"connection.?reset|连接.*重置|reset by peer",
        "连接被对方重置了",
        "下载媒体信息时连接突然断了，通常是网络抖动或 Google 侧断开长连接。",
        ["偶发不用管：会自动重试",
         "频繁出现：检查服务器到 Google 的网络质量"],
    ),
    _e(
        "probe_ssl", "probe",
        r"ssl.*error|certificate|证书.*失败|wrong version number",
        "HTTPS 握手失败了",
        "和 Google 建立加密连接时证书校验没过，可能是中间有代理、或服务器时间不对。",
        ["检查服务器时间是否准确（差几分钟就会失败）",
         "如果走了代理：确认代理配置正确",
         "直连环境出现这个：检查 DNS 是不是被污染"],
    ),
    _e(
        "probe_redirect", "probe",
        r"too many redirects|重定向.*过多|redirect.*loop",
        "下载地址跳转太多次了",
        "文件的下载链接一跳再跳，系统跟丢了。通常是分享链接配置有问题。",
        ["检查这个文件的分享/直链配置",
         ".strm 文件：确认里面的 URL 是最终直链"],
    ),
    _e(
        "probe_size_mismatch", "probe",
        r"size.*mismatch|大小.*不一致|content.?length",
        "文件大小对不上",
        "声明的文件大小和实际下载到的不一致，文件可能没传完或被改过。",
        ["重新上传完整文件后点「重试」",
         "检查上传时是不是中断过"],
    ),
    _e(
        "probe_24h_retry", "probe",
        r"约\s*24h\s*后重试|24 ?小时后重试|长退避",
        "已安排 24 小时后自动重试",
        "这是个临时性故障（配额/限流/超时/服务端抖动），系统不会判死，等 24 小时左右自动再试。这是正常的自愈流程。",
        ["不用管：时间到了自动重试",
         "想提前试：去探测管理里手动「重试」该条目"],
    ),
]


# ---------------------------------------------------------------------------
# 补全（enrich）：元数据刮削 / TMDB / 豆瓣
# ---------------------------------------------------------------------------
_ENRICH = [
    _e(
        "enrich_douban_cast", "enrich",
        r"豆瓣演职员表失败|演员落库失败|cast.*fail",
        "演员表没抓下来",
        "正片资料有了，但演员/演职员表这一步失败了。不影响播放，只影响详情页的演员展示。",
        ["不着急：下次补全会再试",
         "手动点该条目的「重新补全」"],
    ),
    _e(
        "enrich_tmdb_fail", "enrich",
        r"补全.?TMDB.?失败|tmdb.*fail|tmdb.*error",
        "TMDB 刮削失败了",
        "去 TMDB 查这部片的资料时出错了。TMDB 对中文剧集收录本来就差，搜不到是常态，不代表系统坏了。",
        ["偶发失败不用管：会自动重试",
         "长期失败率高：检查 TMDB Key 是否有效（设置 → 刮削）",
         "中文剧：系统会自动走豆瓣，TMDB 失败不影响"],
    ),
    _e(
        "enrich_tmdb_key", "enrich",
        r"tmdb.*key.*无效|tmdb.*401|invalid.*api.?key|api.?key.*invalid",
        "TMDB 的钥匙无效了",
        "TMDB API Key 被拒绝（401）。可能是 Key 填错了、过期了，或者被 TMDB 封了。",
        ["去 TMDB 官网确认 Key 有效，重新填到「刮削设置」",
         "检查 Key 有没有多复制了空格",
         "换个 Key 后点「重试失败」"],
    ),
    _e(
        "enrich_tmdb_429", "enrich",
        r"tmdb.*429|tmdb.*限流|tmdb.*rate",
        "TMDB 请求太频繁被限流了",
        "TMDB 免费 Key 每秒只能查几次，超了就被限。系统有本地缓存和退避，不会烧 Key。",
        ["不用管：退避后自动重试",
         "如果持续被限：降低补全并发，或等凌晨窗口再跑"],
    ),
    _e(
        "enrich_douban_fail", "enrich",
        r"豆瓣.*搜索失败|豆瓣.*失败|douban.*fail",
        "豆瓣没搜到这部片",
        "豆瓣优先刮削没搜中，可能是片名太偏、年份不对，或者豆瓣反爬临时拦截。",
        ["不用管：搜不中会自动回退 TMDB",
         "检查片名里有没有特殊符号，手动改名后重试",
         "频繁失败：可能是 IP 被豆瓣限了，等一会儿再跑"],
    ),
    _e(
        "enrich_incomplete", "enrich",
        r"资料不全|incomplete",
        "资料没刮全",
        "跑完了但关键字段（标题/海报/简介）还是空的，系统按失败计次、指数退避，超限后标记 failed。",
        ["等自动重试：退避时间会越拉越长",
         "手动去 TMDB/豆瓣确认这部片确实有资料",
         "实在没有：NFO 里手写也行"],
    ),
    _e(
        "enrich_retry_exhausted", "enrich",
        r"补全重试超限|enrich.*超限|attempts.*exhausted",
        "补全重试次数用完了",
        "这个条目试了太多次还是不行，系统不再自动试，标记为 failed 等人工处理。",
        ["去「补全失败」列表看具体是哪一步挂的",
         "修好原因后（比如补了 TMDB Key）点「重试失败」",
         "批量重试前先修好根因，不然又是白烧配额"],
    ),
    _e(
        "enrich_backlog", "enrich",
        r"enrich_backlog|补全队列堆积|backlog",
        "补全队列堆了几千条，处理不过来",
        "待补全的条目太多，worker 一时半会跑不完。注意：白天零产出可能是夜间模式（只在凌晨跑），那是正常的。",
        ["先看是不是夜间模式：设置 → 刮削，看时间窗口",
         "夜间模式：等凌晨窗口看它跑不跑",
         "一直堆着：加补全线程数，或先暂停不重要的库"],
    ),
    _e(
        "enrich_stalled", "enrich",
        r"enrich_stalled|有动作但.*成功|卡住",
        "补全卡住了：有动作但没成功",
        "worker 在跑，但最近一个成功的都没有——大概率是死锁、崩溃或下游（TMDB/豆瓣）全挂了。和「跑得慢」完全不同，要查。",
        ["去「后台任务」页看 enrich worker 是不是还活着",
         "看日志里最近的异常，定位是 TMDB 挂了还是自己崩了",
         "重启 enrich worker 试试"],
    ),
    _e(
        "enrich_no_progress", "enrich",
        r"enrich_no_progress|尚未完成任何条目|先观察再排查",
        "补全 worker 可能刚启动，还没产出",
        "本进程里一条成功的都还没有。刚部署/刚启动时这是正常的，先观察。",
        ["等 5-10 分钟再看",
         "一直没产出：检查 worker 到底起没起来",
         "确认不是夜间模式的白天空转"],
    ),
    _e(
        "enrich_flip", "enrich",
        r"enrich_flip|翻转|flip",
        "补全状态大面积翻转了",
        "短时间内大量条目从「已完成」变回「待补全」。可能是手动点了「重试未命中」，也可能是未知触发源，要确认。",
        ["查 EnrichFlipEvent 记录，看触发源是谁",
         "如果是误操作：等它慢慢跑完就行",
         "触发源不明：先别动，保留现场排查"],
    ),
    _e(
        "enrich_img_fail", "enrich",
        r"海报.*失败|图片.*失败|image.*fail|poster.*fail|缩略图.*失败",
        "海报/图片没下下来",
        "文字资料有了，但海报图下载失败。不影响播放，只影响展示。",
        ["会自动重试：图片是单独队列",
         "检查磁盘空间：图片下不下来有时是盘满了",
         "手动点「重新补全」只刷图"],
    ),
    _e(
        "enrich_nfo_fail", "enrich",
        r"nfo.*失败|NFO.*失败",
        "NFO 文件写失败了",
        "元数据写回 NFO 文件时出错，可能是目录没写权限。",
        ["检查媒体目录的写权限",
         "磁盘满了也会写不进去，先看磁盘"],
    ),
    _e(
        "enrich_night_window", "enrich",
        r"夜间窗口|night.?only|凌晨.*才",
        "现在是夜间模式，白天不跑补全",
        "你的设置是「白天只补顶层、凌晨才补单集」，白天看到零产出是正常的，不是坏了。",
        ["不用管：等凌晨 2-6 点窗口看",
         "想白天也跑：去「刮削设置」关掉夜间模式（会烧更多 TMDB 配额）"],
    ),
    _e(
        "enrich_worker_disabled", "enrich",
        r"补全.*关闭|enrich.*disabled|worker.*关掉",
        "补全 worker 被关掉了",
        "有人（或你）把补全总开关关了，所以一条都不跑。这是设置问题。",
        ["去「刮削设置」确认开关状态",
         "如果是你关的：记得为什么关的，开之前确认原因已解决"],
    ),
    _e(
        "enrich_tmdb_timeout", "enrich",
        r"tmdb.*超时|tmdb.*timeout",
        "连 TMDB 超时了",
        "请求 TMDB 时对方半天没回，网络或 TMDB 侧慢。",
        ["偶发不用管：自动重试",
         "长期超时：检查服务器到 api.themoviedb.org 的网络"],
    ),
    _e(
        "enrich_no_source", "enrich",
        r"metadata_source.*none|搜不到|无.*片源|查无此片",
        "这部片在所有源都搜不到",
        "TMDB、豆瓣都搜不中，可能是片名太偏、太新还没收录，或者年份/季号写错了。",
        ["检查文件名里的片名、年份、季号对不对",
         "手动去 TMDB/豆瓣搜一下确认",
         "太新的片：过几天再试"],
    ),
    _e(
        "enrich_actor_avatar", "enrich",
        r"演员.*失败|头像.*失败|avatar.*fail",
        "演员头像没抓到",
        "演员名字有了，但头像图没下下来。小问题，不影响使用。",
        ["会自动重试", "不着急，详情页照常看"],
    ),
    _e(
        "enrich_proxy", "enrich",
        r"代理.*失败|proxy.*fail|连.*TMDB.*失败|dns.*tmdb",
        "连 TMDB 的网络不通",
        "服务器连不上 TMDB：可能是代理挂了、DNS 挂了，或者 TMDB 被墙了。",
        ["检查代理配置和代理服务是不是活着",
         "服务器上 curl 一下 api.themoviedb.org 看通不通",
         "走直连：确认 DNS 能解析"],
    ),
    _e(
        "enrich_fail_ratio", "enrich",
        r"补全失败率|enrich.*fail.*ratio|刮削失败率",
        "补全失败率偏高",
        "最近补全失败的比例超阈值了。注意：TMDB 对中文剧收录差，几百条失败可能是正常的，先看失败都集中在哪。",
        ["看失败列表：是 TMDB Key 问题还是搜不到",
         "搜不到的多：正常，回退豆瓣会补上",
         "Key/网络问题：修好再批量重试"],
    ),
]


# ---------------------------------------------------------------------------
# 扫描（scan）：媒体库扫描 / scan_error
# ---------------------------------------------------------------------------
_SCAN = [
    _e(
        "scan_restart", "scan",
        r"进程重启.*未完成|重启.*中断|scan.*interrupt",
        "扫描时进程重启了，这轮没跑完",
        "部署/重启时正好有库在扫描，新旧进程交接有几毫秒竞态，状态牌子被挂成失败。数据一般没丢，只是牌子没更新。",
        ["数据没丢：scan_runs 里那轮一般是 success",
         "手动对这个库点一次「增量扫描」，牌子就恢复了",
         "以后部署前先看有没有库在扫描，尽量错开"],
    ),
    _e(
        "scan_path_missing", "scan",
        r"路径不存在|path.*not.*found|挂载未就绪|mount.*not.?ready|no such.*director",
        "扫描路径不存在或挂载没就绪",
        "库配置的扫描路径在系统里找不到：挂载掉了，或者路径被改了。",
        ["检查挂载状态：rclone 挂载还在不在",
         "检查库配置里的 paths 对不对",
         "挂载掉了：重挂后再扫"],
    ),
    _e(
        "scan_failed_generic", "scan",
        r"扫描失败|scan.*fail",
        "这个库最近一次扫描失败了",
        "笼统的扫描失败，具体原因要看 scan_error 的原文。",
        ["点开这个库的扫描记录，看失败原因原文",
         "把原文贴到错误翻译里看中文解释"],
    ),
    _e(
        "scan_timeout", "scan",
        r"扫描超时|scan.*timeout",
        "扫描超时了",
        "一轮扫描跑得太久没收尾，可能是文件太多、挂载太慢，或者卡在某个大文件上。",
        ["看扫描记录：卡在哪一步",
         "挂载慢：换个时间再扫，或拆小库",
         "手动点「增量扫描」只扫新增"],
    ),
    _e(
        "scan_lock", "scan",
        r"已有扫描|扫描.*进行中|already.*scanning|锁",
        "这个库已经在扫了",
        "同时只能跑一轮扫描，新的请求被拦下了。这是保护动作，不是故障。",
        ["等当前这轮跑完",
         "如果卡住不动超过很久：检查是不是僵尸扫描，手动重置"],
    ),
    _e(
        "scan_empty_guard", "scan",
        r"空清单|拒绝删库|empty.*guard|禁止.*删除",
        "空清单保护拦下了删库操作",
        "扫描拿到的文件清单是空的，系统拒绝执行删除——防止「扫了个空就把库清空」的灾难。这是保命开关在干活。",
        ["先别慌：库里的数据没动",
         "检查挂载是不是掉了（空清单九成是挂载问题）",
         "挂载恢复后重新扫一轮"],
    ),
    _e(
        "scan_changes_api", "scan",
        r"changes.?api|增量.*失败",
        "增量扫描的 Changes 接口失败了",
        "Google 的 Changes API（查文件变动）没调通，增量扫描退化成全量或跳过。",
        ["检查 SA 账号配额和权限",
         "偶发失败会自动退回全量扫描，慢一点而已"],
    ),
    _e(
        "scan_rclone_down", "scan",
        r"rclone.*断|rclone.*死|挂载断开|mount.*lost|transport.*endpoint",
        "rclone 挂载断开了",
        "云盘挂载进程死了或断了，扫描读不到文件。",
        ["重启 rclone 挂载",
         "检查 rclone 日志看为什么断",
         "挂载恢复后重新扫"],
    ),
    _e(
        "scan_db_write", "scan",
        r"扫描.*写回失败|scan.*commit|写入扫描结果失败",
        "扫描结果写回数据库失败",
        "扫完了，但结果没存进库。数据在内存里丢了，要重扫。",
        ["检查数据库连接正不正常",
         "磁盘满了也会写不进去",
         "重扫一轮就行"],
    ),
    _e(
        "scan_never", "scan",
        r"从未扫描|never.?scanned|没扫过",
        "这个库还没扫过",
        "新建的库，还没跑过第一轮扫描，里面是空的。",
        ["点「扫描」跑第一轮", "第一轮会比较慢，耐心等"],
    ),
    _e(
        "scan_slow", "scan",
        r"扫描.*慢|scan.*slow",
        "扫描跑得很慢",
        "文件多、挂载慢、API 配额紧都会拖慢扫描。",
        ["错开高峰：半夜扫最快",
         "用增量扫描代替全量",
         "检查是不是配额/限流拖的"],
    ),
    _e(
        "scan_quota", "scan",
        r"扫描.*配额|scan.*quota",
        "扫描时配额用完了",
        "扫描中调 Google API 太多，配额见底，剩下的文件扫不动了。",
        ["等配额恢复（每天重置）",
         "加 SA 账号分摊",
         "改增量扫描减少调用"],
    ),
    _e(
        "scan_auth", "scan",
        r"扫描.*认证|scan.*auth|scan.*401",
        "扫描时账号认证失败",
        "扫描用的 SA 账号令牌失效了。",
        ["检查 SA 账号状态", "等轮换恢复，或手动刷新令牌"],
    ),
    _e(
        "scan_interrupted", "scan",
        r"扫描.*中断|scan.*cancel|cancelled",
        "扫描被中断了",
        "有人手动取消了，或者进程被杀了。",
        ["手动再扫一轮就行", "增量扫描会接着上次的进度"],
    ),
]

# ---------------------------------------------------------------------------
# 云盘 / SA 账号（drive_sa）：Google Drive API / 子账号轮换
# ---------------------------------------------------------------------------
_DRIVE_SA = [
    _e(
        "drive_quota_exceeded", "drive_sa",
        r"quotaExceeded|quota.?exceeded|每日.*配额|下载配额",
        "Google 下载配额用完了",
        "这个账号今天从 Google 下载的流量到顶了。Google 按账号限每天下载量，用完就得等明天。",
        ["等明天配额重置（北京时间下午 4 点左右）",
         "加 SA 子账号：100 个账号轮着用，单个不容易用完",
         "热门文件：走 CDN 缓存，别每次都回源"],
    ),
    _e(
        "drive_ratelimit", "drive_sa",
        r"rateLimitExceeded|userRateLimitExceeded|速率.*限制",
        "Google API 请求太频繁",
        "API 调用次数超了 Google 的速率限制，和下载配额是两回事：这是「调接口太快」，不是「下得太多」。",
        ["降并发：把扫描/探测的并发调小",
         "等一会儿自动恢复", "检查是不是多个任务同时在刷 API"],
    ),
    _e(
        "drive_401", "drive_sa",
        r"invalid_grant|token.*expir|令牌过期|refresh.*token",
        "Google 授权过期了",
        "这个 SA 账号的刷新令牌失效，需要重新授权。长期不用的账号容易这样。",
        ["去「SA 账号」页重新授权这个账号",
         "检查账号有没有被 Google 停用",
         "轮换机制会自动切到别的账号先顶着"],
    ),
    _e(
        "drive_invalid_client", "drive_sa",
        r"invalid_client|客户端.*无效",
        "Google 应用凭证无效",
        "OAuth 客户端 ID/Secret 配错了或被 Google 删了。",
        ["去 Google Cloud Console 检查 OAuth 应用还在不在",
         "重新填正确的客户端 ID 和 Secret"],
    ),
    _e(
        "drive_404", "drive_sa",
        r"file.?not.?found|文件.*未找到|404.*drive",
        "云盘上找不到这个文件",
        "文件 ID 在 Google 那边查无此文件：被删了、被移出共享，或者 ID 写错了。",
        ["确认文件还在网盘里", "被删了就从媒体库移除或重传"],
    ),
    _e(
        "drive_403_perm", "drive_sa",
        r"insufficientPermissions|权限不足|forbidden.*drive",
        "SA 账号没权限读这个文件",
        "文件没分享给这个子账号。100 个 SA 里只要有一个没被分享，轮到它就会 403。",
        ["把文件/文件夹分享给所有 SA 账号",
         "团队盘：确认 SA 有成员权限"],
    ),
    _e(
        "drive_500", "drive_sa",
        r"drive.*5\d\d|backendError|内部错误",
        "Google 云盘服务端出错了",
        "Google 那边临时故障，通常很快自己好。",
        ["等几分钟自动重试", "大面积出现就去看 Google 状态页"],
    ),
    _e(
        "sa_all_breakers", "drive_sa",
        r"SA.*熔断|全部.*熔断|all.*breaker|无可用.*账号",
        "所有 SA 账号都被熔断了",
        "100 个子账号全被临时熔断（连续失败太多），系统暂时没有可用的下载账号。这是保护动作。",
        ["等熔断自动解除（带随机抖动恢复）",
         "查熔断原因：是配额、限流还是 401",
         "根因不修，熔断解除后还会再熔"],
    ),
    _e(
        "sa_exhausted", "drive_sa",
        r"子账号.*耗尽|sa.*exhaust|配额.*烧完",
        "子账号配额被烧完了",
        "轮询的一批 SA 账号配额都用光了，之前 10-11 出过 100 个被逐个烧穿的事故。",
        ["等配额重置", "检查是不是某个任务在狂刷下载",
         "加账号或降并发"],
    ),
    _e(
        "sa_rotation", "drive_sa",
        r"SA.*轮换|轮换|rotation",
        "SA 账号正在轮换",
        "系统在多个子账号之间切换使用，这是正常的反风控动作，不是错误。",
        ["不用管：轮换是设计如此"],
    ),
    _e(
        "drive_share_expired", "drive_sa",
        r"分享.*失效|share.*expir|link.*expir",
        "分享链接失效了",
        "文件的分享链接过期或被取消，解析不出下载地址。",
        ["重新生成分享链接并更新",
         ".strm 文件：更新里面的 URL"],
    ),
    _e(
        "drive_trash", "drive_sa",
        r"回收站|trash",
        "文件在回收站里",
        "文件被删到回收站了，API 读不到。",
        ["去网盘回收站还原", "确认删除就从媒体库移除"],
    ),
    _e(
        "drive_scope", "drive_sa",
        r"scope|权限范围|insufficient.*scope",
        "授权范围不够",
        "OAuth 授权时没勾够权限，调某些接口被拒。",
        ["重新授权，勾选 drive.readonly（或 drive）权限",
         "检查 OAuth 同意屏幕的范围配置"],
    ),
    _e(
        "drive_teamdrive", "drive_sa",
        r"team.?drive|团队盘|shared.?drive|共享盘",
        "团队盘相关问题",
        "团队盘（共享盘）的文件需要 SA 账号有成员身份，且 API 调用要带团队盘参数。",
        ["确认 SA 是团队盘成员",
         "检查代码/配置里团队盘参数对不对"],
    ),
    _e(
        "drive_app_not_authorized", "drive_sa",
        r"not.?authorized|应用未授权|access.?denied.*app",
        "应用没被授权访问",
        "Google 没允许这个应用访问该账号/文件。",
        ["重新走 OAuth 授权流程",
         "检查应用有没有被管理员拉黑"],
    ),
    _e(
        "drive_timeout", "drive_sa",
        r"drive.*超时|drive.*timeout",
        "Google 云盘请求超时",
        "调 Google API 半天没回，网络或 Google 侧慢。",
        ["偶发自动重试", "持续超时检查服务器到 Google 的网络"],
    ),
]


# ---------------------------------------------------------------------------
# 系统 / 数据库（system_db）：PG / SQLite / Redis / 磁盘 / worker
# ---------------------------------------------------------------------------
_SYSTEM_DB = [
    _e(
        "db_locked", "system_db",
        r"database is locked|数据库.*锁|db.*lock",
        "数据库被锁住了（SQLite）",
        "SQLite 同一时间只让一个写，写操作排队堵住了。读不受影响，但写会超时。",
        ["等几秒自动重试（系统有写锁重试）",
         "频繁出现：考虑切 PostgreSQL",
         "检查是不是有长事务没提交"],
    ),
    _e(
        "db_conn_refused", "system_db",
        r"数据库不可用|数据库.*连接|connection.?refused.*(db|postgres|5432)|could not connect.*(db|postgres)",
        "连不上数据库",
        "数据库服务没起来、地址配错了，或者网络不通。这是 P0 级故障，所有写操作都会挂。",
        ["检查数据库容器/服务在不在跑",
         "检查连接地址、端口、密码对不对",
         "看数据库日志找原因"],
    ),
    _e(
        "db_operational", "system_db",
        r"operationalerror|sqlalchemy.*error|db.*error",
        "数据库报了个通用错误",
        "SQLAlchemy 抛了 OperationalError，可能是连接断了、SQL 写错了或库有问题。",
        ["看完整错误原文定位",
         "连接断了：重启应用一般能恢复",
         "一直出现：把原文贴出来排查"],
    ),
    _e(
        "db_disk_full", "system_db",
        r"no space left|磁盘满|disk.*full|磁盘.*不足",
        "磁盘满了",
        "服务器硬盘写满了：数据库写不进去、图片下不下来、日志也写不了，会连锁雪崩。",
        ["紧急：清日志、清缓存、删旧备份腾空间",
         "看看到底是哪个目录占满了（du -sh）",
         "长期：加盘或把图片/转码缓存迁走"],
    ),
    _e(
        "db_corrupt", "system_db",
        r"corrupt|database disk image|库损坏|malformed",
        "数据库文件损坏了",
        "SQLite 文件坏了，读都读不出来。这是最坏的情况。",
        ["立刻停服，从最近的备份恢复",
         "检查是不是磁盘坏道/断电导致的",
         "恢复后跑一遍完整性检查"],
    ),
    _e(
        "redis_down", "system_db",
        r"redis.*不可用|redis.*error|redis.*fail",
        "Redis 缓存不可用了",
        "Redis 挂了：队列、熔断器、分布式锁都受影响。系统有本地回退，不会直接崩，但会降级。",
        ["检查 Redis 容器/服务在不在",
         "内存满了：Redis 会拒绝写入，清一下",
         "只是降级：修好 Redis 后自动恢复"],
    ),
    _e(
        "redis_conn", "system_db",
        r"redis.*connection.?refused|redis.*timeout|连不上.*redis",
        "连不上 Redis",
        "应用连 Redis 被拒绝：地址/端口/密码错了，或者 Redis 没起来。",
        ["检查 Redis 地址端口密码配置",
         "telnet 一下看通不通"],
    ),
    _e(
        "disk_low", "system_db",
        r"磁盘余量|disk.*free|磁盘.*偏低|磁盘.*告警",
        "磁盘快满了",
        "磁盘剩余空间低于 15%，再涨就写不进去了。",
        ["清转码缓存、旧日志、旧备份",
         "图片库大了：考虑迁到对象存储",
         "设个磁盘告警，别等真满"],
    ),
    _e(
        "worker_dead", "system_db",
        r"worker.*死亡|线程.*死亡|线程.*死|thread.*dead",
        "后台线程死掉了",
        "某个后台 worker 线程静默死亡，它负责的活（探测/补全/扫描）全停了，但主服务还活着，所以页面看着正常。",
        ["去「后台任务」页看是哪个 worker 死了",
         "重启应用或手动拉起该 worker",
         "查日志看死因（OOM？异常没 catch？）"],
    ),
    _e(
        "oom", "system_db",
        r"out of memory|oom|内存不足|memory.*exhausted|killed.*memory",
        "内存不够用了",
        "进程因为内存不足被杀了（OOM Killer）或自己崩了。2 核 8G 的机器上大并发转码最容易爆。",
        ["看看到底是谁吃的内存（转码？扫描？）",
         "降并发：转码并发、扫描 worker 数调小",
         "长期：加内存或把转码 offload 出去"],
    ),
    _e(
        "cpu_high", "system_db",
        r"cpu.*高|cpu.*100|cpu.*满",
        "CPU 跑满了",
        "CPU 占用长期 100%，接口会变慢、转码会卡。",
        ["看是哪个进程吃的（top）",
         "转码吃满：降转码并发",
         "扫描/补全吃满：错开高峰"],
    ),
    _e(
        "db_slow", "system_db",
        r"慢查询|slow.?query|query.*slow",
        "数据库查询太慢了",
        "某条 SQL 跑了好几秒，接口跟着卡。数据量大了以后常见。",
        ["看慢查询日志定位是哪条 SQL",
         "缺索引：加上",
         "列表页：确认走了分页，没全表扫"],
    ),
    _e(
        "db_migration", "system_db",
        r"migration.*fail|迁移失败|alembic.*error",
        "数据库迁移失败了",
        "升级时表结构变更没执行完，卡在中间状态。",
        ["别重复跑：先看 migration 版本表卡在哪",
         "备份后手动执行缺的那步",
         "找开发看日志"],
    ),
    _e(
        "pg_conn_limit", "system_db",
        r"too many (clients|connections)|连接数.*满|connection.*limit",
        "数据库连接数打满了",
        "PostgreSQL 的连接数到顶了，新的请求连不进去。",
        ["检查有没有连接泄漏（没 close 的）",
         "调大 max_connections，或上连接池",
         "紧急：重启应用释放连接"],
    ),
]

# ---------------------------------------------------------------------------
# 网络 / HTTP 通用（network）
# ---------------------------------------------------------------------------
_NETWORK = [
    _e(
        "net_conn_refused", "network",
        r"connection.?refused(?!.*redis|.*db|.*postgres)|连接.*拒绝",
        "目标地址拒绝连接",
        "对方地址能通，但端口没服务在听：服务没起、端口填错了，或者防火墙拦了。",
        ["确认目标服务在运行", "检查地址和端口对不对",
         "防火墙/安全组放行该端口"],
    ),
    _e(
        "net_timeout", "network",
        r"connect.?timeout|connection.?timed.?out|连接超时|read.?timeout",
        "连接超时了",
        "对方长时间没响应：网络慢、对方挂了，或者地址根本不通。",
        ["ping 一下看网络通不通", "确认目标服务还活着", "偶发等一会儿再试"],
    ),
    _e(
        "net_dns", "network",
        r"name or service not known|temporary failure in name resolution|dns.*fail|域名.*解析|getaddrinfo.*fail",
        "域名解析失败了",
        "域名转不成 IP：DNS 挂了、域名写错了，或者没网。",
        ["检查域名拼写", "换个 DNS 试试（8.8.8.8）",
         "服务器上 nslookup 看能不能解析"],
    ),
    _e(
        "net_ssl", "network",
        r"ssl|certificate|证书",
        "SSL/证书出问题了",
        "HTTPS 握手失败：证书过期、自签证书不被信任，或中间有劫持。",
        ["检查证书有没有过期", "自签证书：确认客户端信任了",
         "服务器时间对不对（差太多会校验失败）"],
    ),
    _e(
        "net_502", "network",
        r"http\s*502|502.*gateway|bad.?gateway",
        "网关 502 了",
        "nginx 收到了请求，但后端没回：后端挂了、地址配错了，或者刚重启还没好。",
        ["看后端容器/进程在不在", "nginx 配置里的后端地址对不对",
         "刚部署完：等 1 分钟再试"],
    ),
    _e(
        "net_503", "network",
        r"http\s*503|503.*service|no_healthy_account",
        "服务暂时不可用（503）",
        "后端说「我现在服务不了」：过载、没健康实例，或者网关的账号池空了。",
        ["等一会儿再试", "检查后端负载是不是爆了",
         "no_healthy_account：网关账号池占满，等 2-4 分钟"],
    ),
    _e(
        "net_504", "network",
        r"http\s*504|504.*gateway.?timeout",
        "网关超时（504）",
        "nginx 等后端等到超时：后端处理太慢或卡死了。",
        ["看后端是不是在 full GC / 死锁",
         "接口太慢：加超时、做分页", "重启后端试试"],
    ),
    _e(
        "net_proxy", "network",
        r"proxy.*error|代理.*失败|代理.*错误",
        "代理服务器出问题了",
        "走的代理连不上或报错，请求根本没发出去。",
        ["检查代理地址、端口、账号密码",
         "代理服务本身在不在跑", "直连试试，定位是不是代理的问题"],
    ),
    _e(
        "net_reset", "network",
        r"connection.?reset|连接.*重置",
        "连接被重置了",
        "传到一半连接断了：网络抖动或对方主动掐了。",
        ["偶发重试就行", "频繁出现查网络质量"],
    ),
    _e(
        "net_unreachable", "network",
        r"network.?unreachable|网络不可达|no route to host",
        "网络不可达",
        "到目标根本没有路由：网断了、IP 段不对，或者容器网络配错了。",
        ["ping 网关看本地网通不通", "检查目标 IP 对不对",
         "容器环境：检查 docker 网络"],
    ),
]


# ---------------------------------------------------------------------------
# 组装与查询
# ---------------------------------------------------------------------------
LEXICON: List[Dict[str, Any]] = (
    _PROBE + _ENRICH + _SCAN + _DRIVE_SA + _SYSTEM_DB + _NETWORK
)
LEXICON_COUNT = len(LEXICON)
CATEGORIES = ["probe", "enrich", "scan", "drive_sa", "system_db", "network"]

# 构建时自检：保证条目数达标、id 唯一
assert LEXICON_COUNT == 100, f"词典条目数 {LEXICON_COUNT} != 100"
_ids = [e["id"] for e in LEXICON]
assert len(set(_ids)) == len(_ids), "词典 id 有重复"


def translate(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """把一条原始错误翻译成大白话。

    命中返回 ``{"id", "category", "title", "what", "steps"}``；
    空文本或无命中返回 ``None``（调用方直接跳过，不影响主流程）。
    """
    if not text:
        return None
    t = str(text)
    if not t.strip():
        return None
    for entry in LEXICON:
        if entry["rx"].search(t):
            return {
                "id": entry["id"],
                "category": entry["category"],
                "title": entry["title"],
                "what": entry["what"],
                "steps": list(entry["steps"]),
            }
    return None


def categories() -> List[str]:
    """六大分类 key 列表。"""
    return list(CATEGORIES)
