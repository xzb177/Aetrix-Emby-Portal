"""
统一数据模型
整合用户端、管理后台和主项目的所有数据模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, BigInteger, DateTime, Date, Text, Numeric, Index, ForeignKey, JSON, Float, UniqueConstraint
from sqlalchemy.orm import relationship
from backend.database import Base


# ==================== 系统管理相关 ====================

class AdminUser(Base):
    """管理员用户表"""
    __tablename__ = 'admin_users'

    __table_args__ = (
        Index('idx_admin_username', 'username'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    email = Column(String(255))
    role = Column(String(50), default='admin')  # super_admin, admin, operator
    permissions = Column(JSON)  # 权限列表
    is_active = Column(Boolean, default=True)
    failed_login_count = Column(Integer, default=0)
    locked_until = Column(DateTime)
    last_login_at = Column(DateTime)
    last_login_ip = Column(String(50))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AdminRole(Base):
    """管理员角色表"""
    __tablename__ = 'admin_roles'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    description = Column(String(255))
    permissions = Column(JSON)  # 权限列表
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class AdminLog(Base):
    """管理员操作日志表"""
    __tablename__ = 'admin_logs'

    __table_args__ = (
        Index('idx_admin_log_user', 'admin_user_id'),
        Index('idx_admin_log_action', 'action'),
        Index('idx_admin_log_time', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_user_id = Column(Integer, ForeignKey('admin_users.id'))
    action = Column(String(100), nullable=False)  # 操作类型
    target_type = Column(String(50))  # 操作目标类型
    target_id = Column(Integer)  # 操作目标ID
    details = Column(JSON)  # 操作详情
    ip_address = Column(String(50))
    user_agent = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)

    admin_user = relationship("AdminUser")


class SystemConfig(Base):
    """系统配置表"""
    __tablename__ = 'system_configs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(50), unique=True, nullable=False, index=True)
    value = Column(Text)
    description = Column(String(255))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ServerRealm(Base):
    """服（一个可独立运营的服务单元）—— 面板可以同时运营多个服

    一个服 = 一套独立对外提供播放的服务：

    - **内容**：该服的媒体库（``emby_libraries.realm_id``）与存储挂载（``storage_mounts.realm_id``）；
    - **卖什么**：该服的套餐（``subscription_plans.realm_id``）；
    - **卖给谁**：该服的订阅（``user_subscriptions.realm_id``）——同一个用户可以在多个服各有一份订阅；
    - **谁来放**：该服的服务器记录（``remote_servers.realm_id``，kind=ea）。
      同一个服可以部署到多台机器上（每台一个 EA，各有自己的 ``node_key``）同时出流。

    面板顶部的「当前服」决定后台各页默认在看哪个服（``SystemConfig["active_realm_id"]``）。
    升级上来的老部署会自动回填出一个默认服（``slug='main'``），行为与单服时完全一致。

    **接入方式（``access_mode``）**：一个面板可以同时运营两种服——

    - ``paid``（付费服，缺省）：需要生效中的订阅才能播放，卖套餐、走付费墙；
    - ``free``（公益服）：免费开放，**不需要订阅**即可播放全库内容。公益服一般靠
      注册/邀请引流，用 ``access_note`` 写清规则（限设备、禁止下载、禁止转卖…），
      并用 ``allow_download`` 关掉下载以保护资源与带宽。

    两种服的判定统一收口在 ``backend/subscriptions.py``（``can_play`` /
    ``ensure_download_allowed``）：升级上来的老服没有这个字段值时按 ``paid`` 处理，
    行为与升级前完全一致。
    """

    __tablename__ = 'server_realms'

    __table_args__ = (
        Index('idx_realm_slug', 'slug'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), unique=True, nullable=False)
    # 稳定标识：写进 EA 的 REALM 环境变量、也用于排查（改名不影响它）
    slug = Column(String(40), unique=True, nullable=False)
    # 该服对外的 Emby 地址（用户端账号卡用；留空则回退全局 EMBY_PUBLIC_URL）
    url = Column(String(500), default='')
    description = Column(String(300), default='')
    # v2.7.0 公益服：paid（需要订阅）/ free（免费开放）；空值按 paid 处理
    access_mode = Column(String(10), default='paid')
    # 公益服规则/说明（用户端展示，也作为付费墙与下载拦截的提示文案）
    access_note = Column(String(500), default='')
    # 该服下载策略：None=跟随全局（公益服默认禁止）/ True=允许 / False=禁止
    allow_download = Column(Boolean, nullable=True, default=None)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class RemoteServer(Base):
    """已添加的服务器（面板可以加多台，每类里挑一台作为「当前使用」）

    四类：

    - ``ea``：本项目自带的 Emby API（EA，分离部署的后端服）。激活后同步写回
      ``emby_managed_*`` 配置，现有的网关闸门 / 挂载体检 / 客户端指引全部照旧生效。
    - ``emby``：已有的第三方 Emby / Jellyfin。激活后同步写回 ``emby_external_*``。
    - ``moviepilot``：MoviePilot。求片批准后可一键提交为它的订阅（它自己去搜索与下载）。
    - ``qbittorrent``：qBittorrent。有磁力/种子链接时可直接交给它下载。

    ``config`` 存类型相关字段（JSON 文本），密钥类字段永不出接口（见 ``servers.mask_config``）。
    同一类型只能有一行 ``is_active``：由激活接口保证，避免出现「谁是当前入口」的歧义。
    **同一类型 + 同一个服** 只能有一行 ``is_active``（见 ``servers.activate``）。
    """

    __tablename__ = 'remote_servers'

    __table_args__ = (
        Index('idx_remote_server_kind', 'kind'),
        Index('idx_remote_server_realm', 'realm_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), unique=True, nullable=False)
    kind = Column(String(20), nullable=False)  # ea / emby / moviepilot / qbittorrent
    # 属于哪个服：每台 EA 只服务它所属服的内容与订阅（见 backend/realms.py）
    realm_id = Column(Integer, ForeignKey('server_realms.id'), nullable=True)
    # 节点标识：EA 启动时用环境变量 NODE_KEY 认领这条记录（见 emby_server/nodes.py）。
    # 只有 EA 需要它——它决定「这台机器负责哪些媒体库、只提供哪些内容」。
    node_key = Column(String(60), unique=True, nullable=True)
    url = Column(String(500), nullable=False)
    # 类型相关配置（JSON 文本）：api_key / username / password / savepath 等
    config = Column(Text, default='{}')
    is_enabled = Column(Boolean, default=True)
    # 同一类型里的「当前使用」：EA / Emby 会同步到 emby_active_mode 等旧配置键
    is_active = Column(Boolean, default=False)
    remark = Column(String(300), default='')
    # 最近一次连接测试结果（仅展示，不参与鉴权判断）
    last_checked_at = Column(DateTime)
    last_check_ok = Column(Boolean)
    last_check_message = Column(String(300))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class NotificationHistory(Base):
    """通知历史表"""
    __tablename__ = 'notification_history'

    __table_args__ = (
        Index('idx_notif_type', 'notification_type'),
        Index('idx_notif_time', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    notification_type = Column(String(50), nullable=False)  # telegram, email, system
    target = Column(String(255))  # 接收者
    title = Column(String(255))
    content = Column(Text)
    status = Column(String(20), default='pending')  # pending, sent, failed
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    sent_at = Column(DateTime)


class StationMessage(Base):
    """站内消息表 - 前后台联动核心"""
    __tablename__ = 'station_messages'

    __table_args__ = (
        Index('idx_station_from', 'from_user_id'),
        Index('idx_station_to', 'to_user_id'),
        Index('idx_station_read', 'is_read'),
        Index('idx_station_type', 'message_type'),
        Index('idx_station_time', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_user_id = Column(Integer, ForeignKey('web_users.id'))  # 发送者（管理员 WebUser.id），系统消息为空
    to_user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)  # 接收者
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    message_type = Column(String(20), default='system')  # system, ticket, announcement, subscription, media_seek
    related_id = Column(Integer)  # 关联ID（如工单ID、公告ID）
    is_read = Column(Boolean, default=False)
    read_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)

    from_user = relationship("WebUser", foreign_keys=[from_user_id])
    to_user = relationship("WebUser", backref="station_messages", foreign_keys=[to_user_id])


# ==================== 用户相关 ====================

class WebUser(Base):
    """Web 用户表（网站登录用户）"""
    __tablename__ = 'web_users'

    __table_args__ = (
        Index('idx_web_username', 'username'),
        Index('idx_web_telegram_id', 'telegram_id'),
        Index('idx_web_email', 'email'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    email = Column(String(255))
    telegram_id = Column(BigInteger, unique=True)
    is_active = Column(Boolean, default=True)
    is_staff = Column(Boolean, default=False)
    # 管理员角色（v2.26.0，见 backend/admin_roles.py）：super / operator / viewer
    # 空值 = 升级前的老管理员 → 按 super 处理（不改权、也不会「没人进得去后台」）
    admin_role = Column(String(20), nullable=True)
    points = Column(Integer, default=0)  # 积分余额（签到/邀请返利/兑换/充值）
    # 会员等级（P1 统一货币体系）：经验值只来自真实充值/有效邀请，等级由阈值推导并缓存
    member_xp = Column(Integer, default=0, nullable=False, server_default="0")  # 会员经验值
    member_level = Column(Integer, default=1, nullable=False, server_default="1")  # 缓存的当前等级（1-6）
    # 佩戴中的成就徽章 key（panel_achievement_defs.key）；空 = 没戴。
    # 佩戴状态属于账号不属于浏览器：换设备跟着账号走，换账号不串号。
    worn_badge_key = Column(String(64), nullable=True)
    # 活力值（C1 竞品借鉴）：公益服用户每日 00:00 扣 1，上限/阈值/兑换率全部可配；
    # 低于阈值限制观影，耗尽（0）即休眠，签到/续活力后自动恢复。老库补列默认满值。
    vitality = Column(Integer, default=14, nullable=False, server_default="14")

    # 注册渠道（v2.44.0 归因）：admin=管理员创建 / code=卡密注册 /
    # invitation=邀请码注册 / open=开放注册。
    # 空值 = 升级前的存量用户（当时没记，不硬猜），后台显示为「未记录」。
    register_channel = Column(String(20), index=True)
    # P2/P3 修复（审查）：token 版本号。改密码 / 管理员踢下线时 +1，
    # JWT payload 里带 tv，鉴权时比对——旧版本 token 全部失效（改密后旧 token 立即作废）。
    token_version = Column(Integer, default=0, nullable=False, server_default="0")

    # 自建 Emby 凭据（完全自建模式下，Emby 客户端用此账号密码登录）
    emby_username = Column(String(64), unique=True, nullable=True)
    emby_password = Column(String(128), nullable=True)
    # 用户级转码开关（Linger 借鉴，Emby 标准字段 EnableVideoPlaybackTranscoding）
    # 管理员可对特定用户禁用转码（只给直传，省服务器资源），默认允许
    enable_video_transcoding = Column(Boolean, default=True, nullable=False, server_default="1")
    # 公益服身份与资格（模块1）
    is_welfare = Column(Boolean, default=False, nullable=False, server_default="0")
    welfare_expires_at = Column(DateTime, nullable=True)
    welfare_grant_channel = Column(String(20), nullable=True)
    welfare_granted_at = Column(DateTime, nullable=True)
    # 观影碑时长榜隐私：True=允许上榜（默认），False=不上榜
    show_on_leaderboard = Column(Boolean, default=True, nullable=False, server_default="1")
    last_login_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class UserPlayLine(Base):
    """用户播放线路偏好（线路选择功能）

    line 取值：direct（直连线路，默认：video_stream 先试 Google 直链 302，
              客户端直连 Google，VPS 不中转）
              relay（中转线路：跳过一切 302，直接走服务器代理转发，
              流量过 VPS，适合直连 Google 不通的用户）
    无记录 = direct（老用户默认直连，保持现有行为不变）。
    """
    __tablename__ = 'user_play_lines'

    user_id = Column(Integer, ForeignKey('web_users.id'), primary_key=True)
    line = Column(String(16), default='direct', nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("WebUser", backref="play_line_pref")


class TelegramUser(Base):
    """Telegram 用户表（从主项目迁移）"""
    __tablename__ = 'telegram_users'

    __table_args__ = (
        Index('idx_tg_id', 'id'),
        Index('idx_tg_username', 'username'),
    )

    id = Column(BigInteger, primary_key=True)  # Telegram ID
    username = Column(String(100))
    first_name = Column(String(100))
    last_name = Column(String(100))
    language_code = Column(String(10))
    is_bot = Column(Boolean, default=False)
    is_premium = Column(Boolean, default=False)

    # Emby 相关
    emby_account = Column(String)  # Emby 账号
    emby_password = Column(String)  # Emby 密码
    emby_url = Column(String)  # Emby 服务器地址
    is_vip = Column(Boolean, default=False)
    vip_expiry = Column(DateTime)

    # 积分系统
    points = Column(Integer, default=0)
    bank_points = Column(Integer, default=0)
    total_earned = Column(Integer, default=0)
    total_spent = Column(Integer, default=0)

    # 签到相关
    last_checkin = Column(DateTime)
    last_checkin_date = Column(DateTime)
    total_checkin_days = Column(Integer, default=0)
    consecutive_checkin = Column(Integer, default=0)

    # 观影相关
    daily_watch_minutes = Column(Integer, default=0)
    total_watch_minutes = Column(Integer, default=0)
    last_watch_claimed = Column(DateTime)
    watch_streak = Column(Integer, default=0)
    last_watch_checkin_date = Column(DateTime)
    total_watch_checkin_days = Column(Integer, default=0)
    watch_checkin_today = Column(Boolean, default=False)

    # 游戏数据（兼容原有功能）
    win = Column(Integer, default=0)
    lost = Column(Integer, default=0)
    weapon = Column(String)
    attack = Column(Integer, default=0)
    intimacy = Column(Integer, default=0)
    resonance_count = Column(Integer, default=0)

    # 时间相关
    last_active_time = Column(DateTime)
    last_chat_time = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 关联
    web_user_id = Column(Integer, ForeignKey('web_users.id'))
    web_user = relationship("WebUser", backref="telegram_accounts")


# ==================== 订阅和支付相关 ====================

class SubscriptionPlan(Base):
    """订阅套餐表"""
    __tablename__ = 'subscription_plans'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    price = Column(Numeric(10, 2), nullable=False)
    # 积分价（P2 货币体系双轨）：NULL=不支持积分购买；用户端可二选一（人民币 price 或积分 points_price）
    points_price = Column(Numeric(10, 2), nullable=True)
    duration_days = Column(Integer, nullable=False)
    features = Column(JSON)  # 特性列表
    # 属于哪个服：套餐一个服一个（见 ServerRealm）。同一套餐只卖给该服的用户。
    realm_id = Column(Integer, ForeignKey('server_realms.id'), nullable=True)
    is_active = Column(Boolean, default=True)
    is_popular = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    realm = relationship("ServerRealm")


class UserSubscription(Base):
    """用户订阅表"""
    __tablename__ = 'user_subscriptions'

    __table_args__ = (
        Index('idx_sub_user', 'user_id'),
        Index('idx_sub_status', 'status'),
        Index('idx_sub_expiry', 'end_date'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=False)
    # 订阅属于哪个服：决定它能在哪个服的 EA 上播放（见 backend/subscriptions.py）。
    # 同一个用户可以在多个服各有一份互不影响的订阅。
    realm_id = Column(Integer, ForeignKey('server_realms.id'), nullable=True)
    start_date = Column(DateTime, default=datetime.now)
    end_date = Column(DateTime, nullable=False)
    status = Column(String(20), default='active')  # active, expired, cancelled
    auto_renew = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    plan = relationship("SubscriptionPlan")
    user = relationship("WebUser")
    realm = relationship("ServerRealm")


class SubscriptionReminder(Base):
    """订阅到期提醒的发送记录（去重表）

    「到期前 7/3/1 天提醒续费」这个任务会周期性重复执行，**没有这张表就会反复提醒同一个人**。
    每条（订阅 × 档位）只允许一行：``kind`` 是 ``7d`` / ``3d`` / ``1d``，
    以及到期那一刻的一次 ``expired``。写入成功即代表已发过，不再重发。
    """
    __tablename__ = 'subscription_reminders'

    __table_args__ = (
        UniqueConstraint('subscription_id', 'kind', name='uq_sub_reminder_kind'),
        Index('idx_sub_reminder_sub', 'subscription_id'),
        Index('idx_sub_reminder_time', 'sent_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscription_id = Column(Integer, ForeignKey('user_subscriptions.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    # 冗余存一份归属服：结算/排查时不必再回查订阅行
    realm_id = Column(Integer, ForeignKey('server_realms.id'), nullable=True)
    kind = Column(String(20), nullable=False)  # 7d / 3d / 1d / expired
    sent_at = Column(DateTime, default=datetime.now)


class RechargePackage(Base):
    """充值套餐表"""
    __tablename__ = 'recharge_packages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    amount = Column(Integer, nullable=False)  # 积分数量
    price = Column(Numeric(10, 2), nullable=False)
    bonus = Column(Integer, default=0)  # 赠送积分
    is_active = Column(Boolean, default=True)
    is_popular = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    icon = Column(String(10), nullable=True)  # 套餐图标 emoji，由管理端配置，前端直接渲染
    created_at = Column(DateTime, default=datetime.now)


class RechargeOrder(Base):
    """充值订单表"""
    __tablename__ = 'recharge_orders'

    __table_args__ = (
        Index('idx_recharge_user', 'user_id'),
        Index('idx_recharge_status', 'status'),
        Index('idx_recharge_order_id', 'order_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    package_id = Column(Integer, ForeignKey('recharge_packages.id'), nullable=False)
    amount = Column(Integer, nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    payment_method = Column(String(50))
    status = Column(String(20), default='pending')  # pending / paid / refunded / closed
    payment_url = Column(String(500))
    paid_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    # 关单（未支付订单作废）与退款（已支付订单冲正）的留痕。
    # 退款必须留下原因与冲正额度：客服对账、争议时这是唯一的依据。
    closed_at = Column(DateTime)
    refunded_at = Column(DateTime)
    refund_reason = Column(String(255))
    refunded_points = Column(Integer, default=0)  # 实际冲正给该用户的积分数（正数=扣回）
    # 用优惠券时的快照：列表价、优惠金额、核销记录（退款时要把额度还回去）
    list_price = Column(Numeric(10, 2), default=0)
    discount_amount = Column(Numeric(10, 2), default=0)
    coupon_usage_id = Column(Integer, ForeignKey('coupon_usages.id'), nullable=True)

    package = relationship("RechargePackage")
    user = relationship("WebUser")


class SubscriptionOrder(Base):
    """订阅订单表"""
    __tablename__ = 'subscription_orders'

    __table_args__ = (
        Index('idx_suborder_user', 'user_id'),
        Index('idx_suborder_status', 'status'),
        Index('idx_suborder_order_id', 'order_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=False)
    item_name = Column(String(255))
    amount = Column(Numeric(10, 2), nullable=False)
    payment_method = Column(String(50))
    status = Column(String(20), default='pending')  # pending / paid / refunded / closed
    payment_url = Column(String(500))
    paid_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    # 履约时记下「这条订单开出了哪份订阅、发了多少天」：退款要按这笔回滚天数。
    # 不记就只能猜（用户可能同时有别的来源的天数），退款就会多扣或少扣。
    subscription_id = Column(Integer, ForeignKey('user_subscriptions.id'), nullable=True)
    days_granted = Column(Integer, default=0)
    closed_at = Column(DateTime)
    refunded_at = Column(DateTime)
    refund_reason = Column(String(255))
    list_price = Column(Numeric(10, 2), default=0)
    discount_amount = Column(Numeric(10, 2), default=0)
    coupon_usage_id = Column(Integer, ForeignKey('coupon_usages.id'), nullable=True)

    plan = relationship("SubscriptionPlan")
    user = relationship("WebUser")


# ==================== Emby 相关 ====================

class EmbyServer(Base):
    """Emby 服务器表"""
    __tablename__ = 'emby_servers'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    url = Column(String(255), nullable=False)
    api_key = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    max_users = Column(Integer, default=0)
    current_users = Column(Integer, default=0)
    priority = Column(Integer, default=0)  # 优先级
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 监控数据
    last_check_at = Column(DateTime)
    status = Column(String(20), default='unknown')  # online, offline, unknown
    response_time = Column(Integer)  # 响应时间（毫秒）


class PlanServerRelation(Base):
    """套餐服务器关联表（负载均衡）"""
    __tablename__ = 'plan_server_relations'

    __table_args__ = (
        Index('idx_psr_plan', 'plan_id'),
        Index('idx_psr_server', 'server_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=False)
    server_id = Column(Integer, ForeignKey('emby_servers.id'), nullable=False)
    weight = Column(Integer, default=1)  # 权重
    created_at = Column(DateTime, default=datetime.now)

    plan = relationship("SubscriptionPlan")
    server = relationship("EmbyServer")


class UserEmbyAccount(Base):
    """用户 Emby 账号表"""
    __tablename__ = 'user_emby_accounts'

    __table_args__ = (
        Index('idx_emby_user', 'user_id'),
        Index('idx_emby_server', 'server_id'),
        Index('idx_emby_sub', 'subscription_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    server_id = Column(Integer, ForeignKey('emby_servers.id'), nullable=False)
    subscription_id = Column(Integer, ForeignKey('user_subscriptions.id'), nullable=False)
    emby_user_id = Column(String(100))
    username = Column(String(100))
    password = Column(String(255))
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime)
    is_active = Column(Boolean, default=True)

    server = relationship("EmbyServer")
    user = relationship("WebUser")
    subscription = relationship("UserSubscription")


class EmbySession(Base):
    """Emby 会话监控表"""
    __tablename__ = 'emby_sessions'

    __table_args__ = (
        Index('idx_session_user', 'user_id'),
        Index('idx_session_server', 'server_id'),
        Index('idx_session_time', 'start_time'),
    )

    id = Column(String(100), primary_key=True)  # Emby Session ID
    server_id = Column(Integer, ForeignKey('emby_servers.id'))
    user_id = Column(Integer, ForeignKey('web_users.id'))
    username = Column(String(100))
    item_name = Column(String(255))
    item_type = Column(String(50))  # movie, series, episode
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    play_duration = Column(Integer, default=0)  # 播放时长（秒）
    device_name = Column(String(100))
    client_name = Column(String(100))

    server = relationship("EmbyServer")
    user = relationship("WebUser")


class MovieBookmark(Base):
    """电影收藏表"""
    __tablename__ = 'movie_bookmarks'

    __table_args__ = (
        Index('idx_bookmark_user', 'user_id'),
        Index('idx_bookmark_item', 'item_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    item_id = Column(String, nullable=False)
    item_name = Column(String)
    item_type = Column(String)
    bookmark_type = Column(String, default='favorite')  # favorite, watchlist
    rating = Column(Integer)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


# ==================== 工单系统 ====================

class Ticket(Base):
    """工单表"""
    __tablename__ = 'tickets'

    __table_args__ = (
        Index('idx_ticket_user', 'user_id'),
        Index('idx_ticket_status', 'status'),
        Index('idx_ticket_priority', 'priority'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    title = Column(String(200), nullable=False)
    category = Column(String(50), default='other')
    priority = Column(String(20), default='medium')
    status = Column(String(20), default='open')
    admin_id = Column(Integer, ForeignKey('admin_users.id'))  # 处理管理员
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("WebUser")
    admin = relationship("AdminUser")


class TicketMessage(Base):
    """工单消息表"""
    __tablename__ = 'ticket_messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(Integer, ForeignKey('tickets.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=True)
    admin_id = Column(Integer, ForeignKey('admin_users.id'), nullable=True)
    message = Column(Text, nullable=False)
    attachments = Column(JSON)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)

    ticket = relationship("Ticket")
    user = relationship("WebUser")
    admin = relationship("AdminUser")


# ==================== 公告和活动 ====================

class Announcement(Base):
    """公告表"""
    __tablename__ = 'announcements'

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    type = Column(String(20), default='system')
    is_active = Column(Boolean, default=True)
    is_pinned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ThemeActivity(Base):
    """主题观影活动表"""
    __tablename__ = 'theme_activities'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    activity_type = Column(String(50), nullable=False)
    filter_genre = Column(String)
    filter_director = Column(String)
    filter_series = Column(String)
    target_count = Column(Integer)
    reward_mp = Column(Integer)
    reward_title = Column(String)
    is_active = Column(Boolean, default=True)
    start_date = Column(DateTime)
    end_date = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)


class ThemeActivityProgress(Base):
    """活动进度表"""
    __tablename__ = 'theme_activity_progress'

    __table_args__ = (
        Index('idx_actprog_user', 'user_id'),
        Index('idx_actprog_activity', 'activity_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(Integer, ForeignKey('theme_activities.id'), nullable=False)
    user_id = Column(BigInteger, nullable=False)
    progress = Column(Integer, default=0)
    watched_items = Column(Text, default='')
    completed = Column(Boolean, default=False)
    reward_claimed = Column(Boolean, default=False)
    claimed_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ==================== 经济系统（签到/积分/兑换码） ====================

class CheckinRecord(Base):
    """每日签到记录表"""
    __tablename__ = 'checkin_records'

    __table_args__ = (
        Index('idx_checkin_user_date', 'user_id', 'checkin_date', unique=True),
        Index('idx_checkin_date', 'checkin_date'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False, index=True)
    checkin_date = Column(DateTime, nullable=False)  # 签到日期（零点）
    points_awarded = Column(Integer, default=0)
    streak = Column(Integer, default=1)  # 连续签到天数
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("WebUser")


class PointsLog(Base):
    """积分流水表（收入/支出台账）"""
    __tablename__ = 'points_logs'

    __table_args__ = (
        Index('idx_points_user', 'user_id'),
        Index('idx_points_type', 'type'),
        Index('idx_points_time', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False, index=True)
    amount = Column(Integer, nullable=False)  # 正数收入 / 负数支出
    balance_after = Column(Integer, default=0)  # 变动后余额
    type = Column(String(30), default='system')  # checkin, invite, invitee, rebate, exchange, recharge, admin_grant, admin_deduct
    description = Column(String(255))
    ref_id = Column(String(64))  # 关联对象（订单号/兑换码等）
    prev_hash = Column(String(64), nullable=True)  # C3 流水审计：上一条该用户流水的 record_hash（链头为空字符串）
    record_hash = Column(String(64), nullable=True)  # C3 流水审计：本条记录的 hash；NULL=审计开启前的历史记录
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("WebUser")


class VitalityLog(Base):
    """活力值变动流水（C1 竞品借鉴）：每日扣减/签到恢复/积分续活"""
    __tablename__ = 'vitality_logs'

    __table_args__ = (
        Index('idx_vitality_user', 'user_id'),
        Index('idx_vitality_time', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False, index=True)
    delta = Column(Integer, nullable=False)  # 正数恢复 / 负数扣减
    balance_after = Column(Integer, default=0)  # 变动后活力值
    reason = Column(String(30), default='system')  # daily_deduct, checkin, watch_reward, recharge, admin_grant, admin_deduct, archive_clamp
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("WebUser")


class EmbyViewUnlock(Base):
    """Emby 账号/线路查看权限（公益服花积分解锁）

    付费服的查看权限来自有效订阅，不走这张表；公益服没有订阅概念，
    用户花积分购买后在这里留一条记录。同一用户同一服只保留一条
    （续费/重购直接更新该行），靠唯一约束防并发重复扣费。
    """
    __tablename__ = 'emby_view_unlocks'

    __table_args__ = (
        UniqueConstraint('user_id', 'realm_id', name='uq_emby_view_unlock_user_realm'),
        Index('idx_emby_view_unlock_user', 'user_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    realm_id = Column(Integer, ForeignKey('server_realms.id'), nullable=False)
    unlocked_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=True)  # NULL = 永久有效
    points_spent = Column(Integer, default=0)  # 本次解锁花费的积分

    user = relationship("WebUser")


class ExchangeCode(Base):
    """兑换码表（积分兑换 / 订阅兑换 / 折扣兑换）"""
    __tablename__ = 'exchange_codes'

    __table_args__ = (
        Index('idx_exchange_code', 'code'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), unique=True, nullable=False, index=True)
    type = Column(String(20), default='points')  # points 积分 / subscription 订阅 / discount 折扣
    points_value = Column(Integer, default=0)  # points 型：兑换积分数
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=True)  # subscription 型：套餐
    duration_days = Column(Integer, default=0)  # subscription 型：时长
    discount_pct = Column(Integer, default=0)  # discount 型：实付百分比，85=八五折
    max_uses = Column(Integer, default=1)
    use_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    note = Column(String(255))
    used_by = Column(String(500))  # 逗号分隔的 WebUser.id 审计
    expires_at = Column(DateTime)
    created_by = Column(Integer, ForeignKey('web_users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    plan = relationship("SubscriptionPlan")

class ExchangeDiscountCredit(Base):
    """兑换码折扣权益：用户核销 discount 型兑换码后获得一张，下次购买订阅时自动抵扣。

    一张兑换码每人限核销一次（走 CodeRedemption 唯一约束的老逻辑），一张权益只用一次。
    不与优惠券叠加：下单时填了 coupon_code 就走优惠券，否则用这里最优的一张。
    """
    __tablename__ = 'exchange_discount_credits'

    __table_args__ = (
        Index('idx_exdisc_user', 'user_id'),
        Index('idx_exdisc_status', 'status'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    # R32-F1：来源可能是统一码系统的 registration_codes（此时记 0，不是
    # exchange_codes 的行），去掉外键与 NOT NULL。SQLite 本就不强制 FK，
    # PG 老库由 backend/database.py 的 _migrate_exchange_discount_credit_fk 迁移。
    # 该列全仓只写不读（消费端只按 user_id/status 查），无语义影响。
    exchange_code_id = Column(Integer, nullable=True)
    discount_pct = Column(Integer, default=0)  # 实付百分比，85=八五折
    status = Column(String(20), default='unused')  # unused 未用 / used 已用
    used_order_id = Column(String(64))
    expires_at = Column(DateTime)  # 继承兑换码的 expires_at，可空
    created_at = Column(DateTime, default=datetime.now)


# ==================== 邀请系统 ====================

class CodeRedemption(Base):
    """每人每码一次的核销记录（安全修复 H5）

    兑换码 / 卡码的 ``max_uses`` 是**总**次数：此前「100 人份」的公共码同一个账号能一直兑换。
    这里用 ``(code_kind, code_id, user_id)`` 唯一约束兜底：核销时在**同一事务**里先 INSERT，
    并发的第二个请求在唯一索引上冲突（PostgreSQL 等第一个提交后报 IntegrityError），
    直接拒绝，不会发两份奖励。``code_kind``：``exchange`` = ExchangeCode，``reg`` = RegistrationCode。
    """
    __tablename__ = 'code_redemptions'
    __table_args__ = (
        UniqueConstraint('code_kind', 'code_id', 'user_id', name='uq_code_redemption_user'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code_kind = Column(String(20), nullable=False)
    code_id = Column(Integer, nullable=False)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now)


class RegistrationCode(Base):
    """卡码表（注册码 / 续期码 / 白名单码 / 诱饵码 / 指名码）

    借鉴 twilight-kotomi 的 RegCode：一份数据结构靠 code_type 区分用途，
    days 表示授予或叠加的会员天数，target_username 为指名码。
    诱饵码（蜜罐）不建列：靠 HONEY- 码前缀识别（见 backend/codes.is_honeypot），
    零数据库表结构改动。
    本项目的会员口径以 UserSubscription(end_date) 为单一事实来源，
    因此卡码的「天数」最终落到订阅上（无生效订阅则新建，有则叠加）。
    """
    __tablename__ = 'registration_codes'

    __table_args__ = (
        Index('idx_reg_code', 'code'),
        Index('idx_reg_code_type', 'code_type'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    max_uses = Column(Integer, default=1)
    use_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    note = Column(String(255))
    used_by = Column(String(500))  # 逗号分隔的 WebUser.id 审计
    expires_at = Column(DateTime)
    created_by = Column(Integer, ForeignKey('web_users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    # 属于哪个服：这张卡码开出来的是那个服的会员（见 economy.redeem_registration_code）
    realm_id = Column(Integer, ForeignKey('server_realms.id'), nullable=True)
    # ===== 卡码体系（借鉴 twilight-kotomi 的 RegCode）=====
    code_type = Column(Integer, default=1)  # 1 注册码 / 2 续期码 / 3 白名单码
    days = Column(Integer, default=30)  # 授予或叠加的会员天数；-1 表示永久
    target_username = Column(String(50))  # 指名码：非空时仅限该用户名使用
    source = Column(String(20), default='admin')  # admin 管理员发放 / invite 邀请体系生成
    # ===== M1 统一码系统：并入兑换码的奖励类型 =====
    # reward_type: subscription 会员天数（默认，原卡码行为）/ points 积分 / discount 折扣权益
    reward_type = Column(String(20), default='subscription')
    points_value = Column(Integer, default=0)  # points 型：兑换积分数
    discount_pct = Column(Integer, default=0)  # discount 型：实付百分比，85=八五折


class UserDevice(Base):
    """用户设备（第三方播放器登录设备，用于设备上限与设备审查）

    借鉴 twilight-kotomi 的设备限制：按 user_id + device_id 唯一，
    记录客户端名称/版本/IP 与首末次出现时间，超限时可拒绝新设备或踢最久未使用者。
    """
    __tablename__ = 'user_devices'

    __table_args__ = (
        UniqueConstraint('user_id', 'device_id', name='uq_device_user_device'),
        Index('idx_device_user', 'user_id'),
        Index('idx_device_last_seen', 'last_seen_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    device_id = Column(String(128), nullable=False)
    name = Column(String(100))  # 设备名（X-Emby-Authorization Device）
    client = Column(String(100))  # 客户端（Infuse / Forward / SenPlayer ...）
    app_version = Column(String(50))
    ip = Column(String(64))
    first_seen_at = Column(DateTime, default=datetime.now)
    last_seen_at = Column(DateTime, default=datetime.now)
    is_blocked = Column(Boolean, default=False)

    user = relationship("WebUser")


class LoginLog(Base):
    """登录与账号安全日志

    记录登录成功/失败、设备超限被拒、诱饵码触发封禁等事件，
    供管理后台风控审查（reason 区分事件类型）。
    """
    __tablename__ = 'login_logs'

    __table_args__ = (
        Index('idx_loginlog_user', 'user_id'),
        Index('idx_loginlog_time', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=True)
    username = Column(String(64))
    ip = Column(String(64))
    user_agent = Column(String(300))
    success = Column(Boolean, default=True)
    reason = Column(String(100))  # portal_login / emby_login / device_limit / decoy_code ...
    detail = Column(String(255))
    # IP 归属地（能力：IP 与地理位置）；未配置提供方时为空——由 authlog 在写入时填
    region = Column(String(100))
    created_at = Column(DateTime, default=datetime.now)


class ShareGuardEvent(Base):
    """防共享事件（v2.43.0）：跨城市轨迹 + 同播检测的判定结果

    为什么单独一张表而不是复用 ``login_logs``：两者的**查询口径**不一样。
    ``login_logs`` 是「登录与安全事件的流水」，按 ``reason`` 筛选；这里是
    「一个账号在时间轴上的位置与并发情况」，要按 ``(user_id, kind, created_at)``
    往前翻最近一次在哪、上一次并发了几路。两张表混在一起会让登录日志页
    既慢又难读。

    **两种 kind 共用一张表**：它们是同一件事的两面（账号被多人共用），
    后台要在一个地方按时间看完整轨迹，而不是翻两个页签。

    行是**有界**的：见 ``backend/share_guard.py`` 的写入节流（同一区域每隔
    ``travel_window_minutes`` 最多一行 / 城市一变立刻一行），再加按保留天数清理，
    所以开着也不会无限长。
    """

    __tablename__ = 'share_guard_events'

    __table_args__ = (
        Index('idx_shareguard_user_kind', 'user_id', 'kind', 'created_at'),
        Index('idx_shareguard_time', 'created_at'),
        Index('idx_shareguard_kind', 'kind'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=True)
    username = Column(String(64))
    # travel = 跨城市轨迹；concurrent = 同播检测
    kind = Column(String(20), nullable=False, index=True)
    # 基线行（该账号当前所在的域市）不是一次判定。它要落库——下一次的比对就是拿它当
    # 「上一次在哪」——但混在事件流水里会让「判出来过什么」失真，所以单独标出来。
    is_baseline = Column(Boolean, default=False, nullable=False, index=True)
    # 实际生效的档位：record（只记录）/ alert（记录 + 通知管理员）/ enforce（记录 + 处置）
    # off 不会产生任何行——「关闭」的意思就是不写库，而不是「写一行表示关闭」
    action = Column(String(20), default='record', nullable=False)
    ip = Column(String(64))
    # 归属地串（能力：IP 与地理位置）。查不到就是空串，空串**不参与**城市比对
    region = Column(String(100))
    # travel：上一次的城市（空 = 首次或上次查不到）；concurrent：本次判定时的并发数
    prev_region = Column(String(100))
    sessions = Column(Integer, default=0)
    # 人看的说明（已含城市、窗口、并发数等具体数字）
    detail = Column(String(255))
    created_at = Column(DateTime, default=datetime.now, index=True)


class AiUsage(Base):
    """AI 助手每日用量（能力：AI 模型设置）

    原先是进程内字典计数：多进程部署下每个 worker 各记一份，配额会被放大到「上限 × 进程数」，
    重启还会归零。改成落库 + 唯一约束（user_id, day），配额在多进程、多台机器上都是同一份；
    占用额度用「条件 UPDATE + 唯一约束兼底」实现，并发下不会两个请求都读到 limit-1。
    """
    __tablename__ = 'ai_usage'

    __table_args__ = (
        UniqueConstraint('user_id', 'day', name='uq_ai_usage_user_day'),
        Index('idx_ai_usage_day', 'day'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    day = Column(String(10), nullable=False)   # 本地日期 YYYY-MM-DD
    count = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class InvitationCode(Base):
    """邀请码表"""
    __tablename__ = 'invitation_codes'

    __table_args__ = (
        Index('idx_inv_code', 'code'),
        Index('idx_inv_user', 'user_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    max_uses = Column(Integer, default=100)
    use_count = Column(Integer, default=0)
    reward_points = Column(Integer, default=0)
    expires_at = Column(DateTime)
    is_active = Column(Boolean, default=True)
    # 白名单（可选）：逗号分隔的用户名。非空时**只有这些人**能拿这个码注册，
    # 用完即无效——与卡码的 target_username 同一个思路（内测码 / 渠道码）。
    # 空 = 不限（与升级前行为一致）。
    whitelist = Column(Text, default="")
    welfare_days = Column(Integer, default=30, nullable=False, server_default="30")
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("WebUser")




class WelfareGrantLog(Base):
    """公益资格开通/续期记录表"""
    __tablename__ = "welfare_grant_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("web_users.id"), nullable=False, index=True)
    channel = Column(String(20), nullable=False)
    days = Column(Integer, nullable=False, default=0)
    granted_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
class InvitationRecord(Base):
    """邀请记录表"""
    __tablename__ = 'invitation_records'

    __table_args__ = (
        Index('idx_inv_inviter', 'inviter_id'),
        # 一个被邀请人只能有一条关系记录：并发重复发奖的最后一道门
        Index('idx_inv_invitee', 'invitee_id', unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    inviter_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    invitee_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    code_id = Column(Integer, ForeignKey('invitation_codes.id'), nullable=False)
    reward_points = Column(Integer, default=0)
    reward_claimed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)

    inviter = relationship("WebUser", foreign_keys=[inviter_id])
    invitee = relationship("WebUser", foreign_keys=[invitee_id])
    code = relationship("InvitationCode")


class PromotionReward(Base):
    """推广奖励记录（v2.44.0 第一阶段）

    邀请成功后**另发**的一笔奖励，与既有的双向积分奖励并列、互不干扰：
    那边是「邀请这件事本身的回报」，这边是「本期推广激励」，所以走独立的
    开关与阈值（``promotion_reward_*``），默认关闭。

    奖励类型两选一（值全部来自 SystemConfig，不在代码里写死）：

    - ``balance``：加积分余额（``reward_value`` = 积分数）；
    - ``days``：加会员有效期（``reward_value`` = 天数，``realm_id`` 记归属服）。

    一行一条，用户在「我的」里看到的明细就是这张表；``config_snapshot``
    记下发时的开关与阈值，否则日后改了配置就再也说不清「当时到底按多少发的」。
    """
    __tablename__ = 'promotion_rewards'

    __table_args__ = (
        # 一个被邀请人只能给邀请人发一笔（与 invitation_records 的唯一索引同一道门）
        Index('idx_promo_invitee', 'invitee_id', unique=True),
        Index('idx_promo_inviter', 'inviter_id'),
        Index('idx_promo_time', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    inviter_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    invitee_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    code_id = Column(Integer, ForeignKey('invitation_codes.id'))
    # balance / days（见 promotion.py 的 REWARD_TYPES）
    reward_type = Column(String(16), nullable=False)
    reward_value = Column(Integer, nullable=False, default=0)
    # reward_type=days 时的归属服（空 = 当前服，与 grant_membership_days 同口径）
    realm_id = Column(Integer)
    # 下发时的配置快照（JSON），事后能还原「按哪一档发的」
    config_snapshot = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)

    inviter = relationship("WebUser", foreign_keys=[inviter_id])
    invitee = relationship("WebUser", foreign_keys=[invitee_id])
    code = relationship("InvitationCode")


# ==================== 优惠券 ====================

class CouponCode(Base):
    """优惠券（购买时抵扣，v2.10.0）

    与兑换码（`ExchangeCode`）不是一回事：兑换码是「不花钱直接拿东西」，
    优惠券是「付费时打折 / 减钱」——钱还是要走支付网关，只是单价变了。

    **使用次数是预订制的**：下单时就占额度（`CouponUsage.status='reserved'`），
    支付成功转 `consumed`，订单被关掉/退款则 `released` 并把额度还回去。
    只在付款成功时计数的话，一串未支付订单会同时绕过限额，付完款全都拿到折扣。
    """
    __tablename__ = 'coupon_codes'

    __table_args__ = (
        UniqueConstraint('code', name='uq_coupon_code'),
        Index('idx_coupon_kind', 'kind'),
        Index('idx_coupon_active', 'is_active'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), unique=True, nullable=False, index=True)
    # 适用范围：subscription 只能买会员 / recharge 只能充值 / all 两者皆可
    kind = Column(String(20), default='all')
    # percent 百分比（value=85 即 85 折、实付 85%）；fixed 直接减免（value=单位元）
    discount_type = Column(String(20), default='percent')
    value = Column(Integer, default=0)
    # 满多少可用（0 = 不设门槛）；百分比券的封顶减免（0 = 不封顶）
    min_amount = Column(Numeric(10, 2), default=0)
    max_discount = Column(Numeric(10, 2), default=0)
    # 只对某个服的套餐可用（多服运营下避免 A 服的券在 B 服用）
    realm_id = Column(Integer, ForeignKey('server_realms.id'), nullable=True)
    max_uses = Column(Integer, default=0)      # 总次数上限，0 = 不限
    use_count = Column(Integer, default=0)     # 当前占用（reserved + consumed）
    per_user_limit = Column(Integer, default=1)  # 每人最多用几次，0 = 不限
    valid_from = Column(DateTime)
    valid_until = Column(DateTime)
    is_active = Column(Boolean, default=True)
    note = Column(String(255))
    created_by = Column(Integer, ForeignKey('web_users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    realm = relationship("ServerRealm")


class CouponUsage(Base):
    """优惠券核销记录：一笔优惠订单一行（预订 → 消费 / 释放）

    `paid_amount` / `list_price` 快照下来：套餐改价后，历史订单仍要能解释「当时到底按多少钱算的」。
    """
    __tablename__ = 'coupon_usages'

    __table_args__ = (
        UniqueConstraint('order_id', name='uq_coupon_usage_order'),
        Index('idx_coupon_usage_user', 'user_id'),
        Index('idx_coupon_usage_coupon', 'coupon_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    coupon_id = Column(Integer, ForeignKey('coupon_codes.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    order_id = Column(String(64), nullable=False)
    kind = Column(String(20))
    # reserved 占了额度但还没付 / consumed 已付款 / released 订单关掉或退款、额度已还
    status = Column(String(20), default='reserved')
    list_price = Column(Numeric(10, 2), default=0)
    discount_amount = Column(Numeric(10, 2), default=0)
    paid_amount = Column(Numeric(10, 2), default=0)
    created_at = Column(DateTime, default=datetime.now)
    closed_at = Column(DateTime)

    coupon = relationship("CouponCode")
    user = relationship("WebUser")


# ==================== 求片系统 ====================

class MovieRequest(Base):
    """求片请求表"""
    __tablename__ = 'movie_requests'

    __table_args__ = (
        Index('idx_req_user', 'user_id'),
        Index('idx_req_status', 'status'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    movie_name = Column(String(255), nullable=False)
    year = Column(String(10))
    type = Column(String(50))
    # TMDB 候选带过来的 id：管理端「标记已入库」按它精确匹配（手填片名时为空）
    tmdb_id = Column(String(20))
    # 剧集按整季申请：存 "1,2" 或 "all"（全季）；电影/其它类型为空
    season = Column(String(50))
    note = Column(Text)
    status = Column(String(20), default='pending')
    admin_note = Column(Text)
    emby_item_id = Column(String(100))
    # 求的是哪个服的片：入库后进的是该服的媒体库
    realm_id = Column(Integer, ForeignKey('server_realms.id'), nullable=True)
    # 转交外部服务的结果：moviepilot（已提交订阅）/ qbittorrent（已交给下载器加种）
    push_target = Column(String(20))
    push_status = Column(String(20))  # ok / failed
    push_message = Column(String(300))
    pushed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    # v2 求片附议数：用户可对 pending 的求片附议（+1），管理端按热度排序
    vote_count = Column(Integer, default=0, nullable=False)

    user = relationship("WebUser")


class MovieRequestVote(Base):
    """求片附议表（v2）：一个用户对一条求片只能附议一次"""
    __tablename__ = 'movie_request_votes'

    __table_args__ = (
        UniqueConstraint('request_id', 'user_id', name='uq_vote_request_user'),
        Index('idx_vote_request', 'request_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(Integer, ForeignKey('movie_requests.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.now)


# ==================== 系统监控 ====================

class SystemMetric(Base):
    """系统指标表"""
    __tablename__ = 'system_metrics'

    __table_args__ = (
        Index('idx_metric_name', 'metric_name'),
        Index('idx_metric_time', 'timestamp'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    metric_name = Column(String(100), nullable=False)
    metric_value = Column(Float)
    tags = Column(JSON)  # 标签，如 {server_id: 1}
    timestamp = Column(DateTime, default=datetime.now)


class AlertRule(Base):
    """告警规则表

    两种规则形态（二选一）：
    - 事件型（event_type 非空）：系统事件告警（probe_queue_high / enrich_stalled /
      disk_full / sa_quota_low / scan_failed），由 backend/system_alerts.py 的
      定时检查触发。阈值/渠道/接收人/冷却全部可配。
    - 指标型（event_type 为空，metric_name 非空）：遗留的通用指标告警形态。
    """
    __tablename__ = 'alert_rules'

    # R16 P2-2：event_type 唯一（PG/SQLite 的 UNIQUE 都允许多个 NULL，
    # legacy 指标型规则 event_type 为空不受影响）；防 ensure_default_rules
    # "先查后插"竞态下播种出重复规则
    __table_args__ = (
        UniqueConstraint("event_type", name="uq_alert_rules_event_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    metric_name = Column(String(100), nullable=False)
    condition = Column(String(20))  # gt, lt, eq
    threshold = Column(Float)
    severity = Column(String(20), default='warning')  # info, warning, critical
    notification_channels = Column(JSON)  # ['telegram', 'email']
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    # --- 系统告警（事件型）扩展列，见 backend/system_alerts.py ---
    # 事件类型：probe_queue_high / enrich_stalled / disk_full / sa_quota_low /
    # scan_failed / api_error_high；为空表示 legacy 指标型规则
    event_type = Column(String(50), nullable=True)
    # 接收人 JSON：{"staff": true}（默认，全部启用中的管理员）或
    # {"user_ids": [1, 2]}（指定用户）
    recipients = Column(Text, nullable=True)
    # 同一规则冷却秒数：冷却期内重复越线不重复打扰（防轰炸）
    cooldown_sec = Column(Integer, default=3600)


class Alert(Base):
    """告警记录表"""
    __tablename__ = 'alerts'

    __table_args__ = (
        Index('idx_alert_rule', 'rule_id'),
        Index('idx_alert_status', 'status'),
        Index('idx_alert_time', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, ForeignKey('alert_rules.id'))
    severity = Column(String(20), default='warning')
    title = Column(String(255))
    message = Column(Text)
    status = Column(String(20), default='open')  # open, acknowledged, resolved
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    # R16 P1-2：是否至少一个渠道送达。去重只认 notified_ok=True 的记录，
    # 发送失败的不进入冷却，下轮继续重试（一次投递失败不再导致整冷却期失声）
    notified_ok = Column(Boolean, default=False)
    # R16 P2-3：管理员确认的时间（ack_alert 写入，供审计"谁何时确认的"）
    acknowledged_at = Column(DateTime, nullable=True)

    rule = relationship("AlertRule")


# ==================== 导出所有模型 ====================
__all__ = [
    # 系统管理
    "AdminUser", "AdminRole", "AdminLog", "SystemConfig", "NotificationHistory", "StationMessage",
    # 多服运营
    "ServerRealm",
    # 用户
    "WebUser", "TelegramUser",
    # 订阅和支付
    "SubscriptionPlan", "UserSubscription", "SubscriptionReminder",
    "RechargePackage", "RechargeOrder", "SubscriptionOrder",
    # Emby
    "EmbyServer", "PlanServerRelation", "UserEmbyAccount", "EmbySession", "MovieBookmark",
    # 工单
    "Ticket", "TicketMessage",
    # 公告和活动
    "Announcement", "ThemeActivity", "ThemeActivityProgress",
    # 经济系统
    "CheckinRecord", "PointsLog", "ExchangeCode",
    # 邀请
    "InvitationCode", "InvitationRecord",
    # 优惠券
    "CouponCode", "CouponUsage",
    # 求片
    "MovieRequest",
    # 监控
    "SystemMetric", "AlertRule", "Alert",
]


# ==================== 娱乐板块（公益服模块3） ====================

class MediaRequest(Base):
    """求片请求表（用户端娱乐板块）

    .. deprecated::
        求片已统一走 movie_requests（MediaSeek 流程）。本表不再写入，
        保留模型仅为兼容历史数据。
    """
    __tablename__ = 'media_requests'

    __table_args__ = (
        Index('idx_mreq_user', 'user_id'),
        Index('idx_mreq_status', 'status'),
        Index('idx_mreq_tmdb', 'tmdb_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    tmdb_id = Column(String(20), nullable=False)
    media_type = Column(String(10), nullable=False, default='movie')  # movie / tv
    title = Column(String(255), nullable=False)
    status = Column(String(20), default='pending')  # pending/approved/rejected/done
    admin_note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("WebUser")


class LotteryRound(Base):
    """群抽奖活动轮次：一期抽奖"""
    __tablename__ = 'lottery_rounds'

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False)
    chat_id = Column(BigInteger, nullable=False)  # 群组 chat_id
    status = Column(String(20), nullable=False, default='open')  # open/drawing/done/cancelled
    start_at = Column(DateTime, default=datetime.now)
    draw_at = Column(DateTime, nullable=True)      # 计划开奖时间，空=手动开奖
    drawn_at = Column(DateTime, nullable=True)
    drawing_at = Column(DateTime, nullable=True)  # 认领开奖时间戳：drawing 僵尸回收用
    max_participants = Column(Integer, nullable=False, default=0)  # 0=不限
    # 抽奖类型：button=按钮抽奖 / password=口令抽奖
    lottery_type = Column(String(20), nullable=False, default='button', server_default='button')
    password_keyword = Column(String(100), nullable=True)  # 口令抽奖时的关键词，button 类型时为 NULL
    seed_hash = Column(String(64), nullable=True)  # 开奖前公布的承诺
    seed = Column(String(64), nullable=True)       # 开奖后揭示
    # 三重门·公信门：drand 公开随机信标（开奖时抓取，与 seed 混合成最终种子）
    drand_round = Column(BigInteger, nullable=True)      # drand 信标轮次
    drand_randomness = Column(String(128), nullable=True)  # drand 信标随机值（hex）
    created_by = Column(Integer, ForeignKey('web_users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class LotteryRoundPrize(Base):
    """群抽奖奖品：挂在某一轮下面"""
    __tablename__ = 'lottery_round_prizes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    round_id = Column(Integer, ForeignKey('lottery_rounds.id', ondelete='CASCADE'), nullable=False)
    name = Column(String(100), nullable=False)
    type = Column(String(20), nullable=False)  # days=公益天数 / points=积分 / whitelist=白名单(永久)
    value = Column(Integer, nullable=False, default=0)
    quantity = Column(Integer, nullable=False, default=1)
    sort = Column(Integer, nullable=False, default=0)


class LotteryRoundEntry(Base):
    """群抽奖参与记录：一人一期只能参加一次"""
    __tablename__ = 'lottery_round_entries'

    __table_args__ = (
        UniqueConstraint('round_id', 'user_id', name='uq_lottery_round_entry'),
        Index('idx_lottery_entry_round', 'round_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    round_id = Column(Integer, ForeignKey('lottery_rounds.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    telegram_id = Column(BigInteger, nullable=False)
    joined_at = Column(DateTime, default=datetime.now)


class LotteryRoundWinner(Base):
    """群抽奖中奖记录"""
    __tablename__ = 'lottery_round_winners'

    id = Column(Integer, primary_key=True, autoincrement=True)
    round_id = Column(Integer, ForeignKey('lottery_rounds.id', ondelete='CASCADE'), nullable=False)
    entry_id = Column(Integer, ForeignKey('lottery_round_entries.id'), nullable=False)
    prize_id = Column(Integer, ForeignKey('lottery_round_prizes.id'), nullable=False)
    distributed = Column(Boolean, nullable=False, default=False, server_default='0')
    distributed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class LotteryBlacklist(Base):
    """群抽奖黑名单：三重门·资格门。

    被拉黑的用户无法参加任何群抽奖。由管理后台维护。
    """
    __tablename__ = 'lottery_blacklist'

    __table_args__ = (
        UniqueConstraint('user_id', name='uq_lottery_blacklist_user'),
        Index('idx_lottery_blacklist_user', 'user_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id', ondelete='CASCADE'), nullable=False)
    reason = Column(String(200), nullable=True)  # 拉黑原因
    created_by = Column(Integer, ForeignKey('web_users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class RedPacket(Base):
    """红包表（积分红包）"""
    __tablename__ = 'red_packets'

    __table_args__ = (
        Index('idx_rp_sender', 'sender_id'),
        Index('idx_rp_time', 'created_at'),
        # P1 修复（审查）：TG poller at-least-once 重放 /redpacket 会重复建包扣款。
        # 用 TG update_id 做幂等键，唯一约束保证重放只返回已存在的包。
        UniqueConstraint('idempotency_key', name='uq_redpacket_idempotency'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sender_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    total_amount = Column(Integer, nullable=False)  # 总积分
    total_count = Column(Integer, nullable=False)  # 总个数
    remaining_amount = Column(Integer, nullable=False)
    remaining_count = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    # 幂等键（如 tg:<update_id>）；非 TG 渠道调用时可为空
    idempotency_key = Column(String(64), nullable=True)

    sender = relationship("WebUser")


class RedPacketClaim(Base):
    """红包领取记录表"""
    __tablename__ = 'red_packet_claims'

    __table_args__ = (
        Index('idx_rpc_packet', 'packet_id'),
        Index('idx_rpc_user', 'user_id'),
        # 并发重复领取的数据库级兜底：同一用户对同一红包只能有一条领取记录
        UniqueConstraint('packet_id', 'user_id', name='uq_rpc_packet_user'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    packet_id = Column(Integer, ForeignKey('red_packets.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    amount = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("WebUser")


class Review(Base):
    """影评表"""
    __tablename__ = 'reviews'

    __table_args__ = (
        Index('idx_review_guid', 'item_guid'),
        Index('idx_review_user', 'user_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    item_guid = Column(String(64), nullable=False)
    rating = Column(Integer, nullable=False, default=5)  # 1-5
    content = Column(Text, nullable=False)
    likes = Column(Integer, default=0, nullable=False, server_default="0")
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("WebUser")



class MemberLevel(Base):
    """会员等级配置表（P1 统一货币体系）

    等级 1-6，经验阈值默认 0/100/500/1500/5000/15000（后台可改）。
    等级折扣：订阅购买时按等级 discount_pct 打折，仅付费服（access_mode='paid'）生效，公益服不打折。
    """
    __tablename__ = 'member_levels'

    __table_args__ = (
        Index('idx_member_level', 'level'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(Integer, unique=True, nullable=False)  # 等级 1-6
    name = Column(String(30), nullable=False)  # 等级名称：初幕/影迷/鉴赏家/放映师/造梦者/传奇（暗房影院主题）
    xp_threshold = Column(Integer, nullable=False, default=0)  # 升级所需经验阈值
    discount_pct = Column(Integer, nullable=False, default=0, server_default="0")  # 等级订阅折扣百分比 0-100，仅付费服生效
    benefits_json = Column(Text, nullable=True)  # 权益描述 JSON 数组，如 ["权益1","权益2"]
    badge_icon = Column(String(30), nullable=True)  # 徽章图标名（lucide 图标名）
    badge_color = Column(String(20), nullable=True)  # 徽章主题色，如 "#c0c0c0"
    is_active = Column(Boolean, default=True, nullable=False, server_default="1")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MemberXpLog(Base):
    """会员经验流水表（P1 统一货币体系）

    经验只来自真实充值（订单回调成功）和有效邀请；兑换码/红包/签到不加经验。
    """
    __tablename__ = 'member_xp_log'

    __table_args__ = (
        Index('idx_xplog_user', 'user_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False, index=True)
    xp_delta = Column(Integer, nullable=False)  # 经验变动（正数=发放，负数=退款回滚）
    xp_after = Column(Integer, nullable=False)  # 变动后经验
    source = Column(String(20), nullable=False)  # recharge=充值 / invite=有效邀请 / subscription=订阅购买
    ref_id = Column(String(64), nullable=True)  # 关联订单号等
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("WebUser")


class TgBindCode(Base):
    """TG 绑定码表（B1：bot 绑定基础设施）

    支持两种绑定流程：
    - 网页发起：user_id 已填，telegram_id 为空 → 用户给 bot 发码验证
    - Bot 发起：telegram_id 已填，user_id 为空 → 用户在网页输入码验证
    """
    __tablename__ = 'tg_bind_codes'

    __table_args__ = (
        Index('idx_tgbind_code', 'code'),
        Index('idx_tgbind_user', 'user_id'),
        Index('idx_tgbind_tgid', 'telegram_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=True, index=True)
    telegram_id = Column(BigInteger, nullable=True, index=True)
    code = Column(String(6), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    # 本码生效期间全站累计的「猜错绑定码」次数；达到上限即作废（防多 TG 账号分布式枚举）
    fail_count = Column(Integer, default=0)


class ChatPointsLog(Base):
    """群发言积分明细表（M1）

    bot 统计 TG 群有效发言的计分明细，同时作为防刷依据：
    - 唯一约束 (telegram_id, chat_id, message_id) 保证幂等（offset 重放/重启不重复计分）
    - 索引 (web_user_id, points_date) 供每日上限查询
    - 索引 (telegram_id, created_at) 供防刷窗口查询
    积分本身进 WebUser.points（单一货币），流水见 PointsLog(type='chat')。
    """
    __tablename__ = 'chat_points_log'

    __table_args__ = (
        UniqueConstraint('telegram_id', 'chat_id', 'message_id', name='uq_chat_points_msg'),
        Index('idx_chat_points_user_date', 'web_user_id', 'points_date'),
        Index('idx_chat_points_tg_time', 'telegram_id', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    web_user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    points = Column(Integer, nullable=False, default=1)
    points_date = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)



class RevokedJwt(Base):
    """JWT 吊销表（P2 修复：jti 生成了但从未校验/存储，无吊销能力）。

    登出 / 改密码 / 管理员踢下线时把 jti 写进来；鉴权时查表，已吊销则 401。
    expires_at 过期后由定时任务清理（token 本来也过期了，留着无意义）。
    """
    __tablename__ = 'revoked_jwt'

    jti = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False, index=True)
    reason = Column(String(50), nullable=False, default="logout")
    revoked_at = Column(DateTime, default=datetime.now, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class ApiKey(Base):
    """API Key / Service Token 表（服务端对服务端认证）

    - 明文格式：``ak_`` + 48 位 URL 安全随机串，创建时返回一次，库里只存
      ``SHA256(明文 + pepper)``（见 backend/api_key.py）
    - ``key_id`` 是明文前 12 字符的公开标识，可安全展示、作查询前缀
    - scopes：['read', 'write', 'admin']，层级 admin > write > read
    """
    __tablename__ = 'api_keys'

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(32), unique=True, nullable=False, index=True)
    key_hash = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    scopes = Column(JSON, default=list)
    expires_at = Column(DateTime, nullable=True)  # None = 永不过期
    is_active = Column(Boolean, default=True, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey('web_users.id'), nullable=True)  # 创建人 admin id
    created_at = Column(DateTime, default=datetime.now, nullable=False)


# ==================== v4 探险家日志面板 ====================

class PanelUnseal(Base):
    """面板盲盒拆封记录：用户拆了哪件今日封存（幂等，UNIQUE 约束）"""
    __tablename__ = 'panel_unseals'

    __table_args__ = (
        UniqueConstraint('user_id', 'item_id', name='uq_panel_unseal_user_item'),
        Index('idx_panel_unseal_user', 'user_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    # emby_items.id（MediaItem 与本表同属 backend.database.Base 同一 metadata，FK 可解析）。
    # ondelete=CASCADE：条目被物理回收（purge_expired）时拆封记录一并消失——记录只在条目
    # 存在时有意义（标记今日 5 件封存是否已拆），且 unseal 只按 user_id 查，级联无副作用。
    item_id = Column(Integer, ForeignKey('emby_items.id', ondelete='CASCADE'), nullable=False)
    unsealed_at = Column(DateTime, default=datetime.now, nullable=False)


class PanelAchievementDef(Base):
    """成就定义（启动时 seed，规则文本不写死在代码里）"""
    __tablename__ = 'panel_achievement_defs'

    key = Column(String(50), primary_key=True)  # 如 'first_night'
    name = Column(String(50), nullable=False)
    desc = Column(String(200), nullable=False)
    icon = Column(String(20), nullable=False, default='🏅')
    sort = Column(Integer, default=0, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)  # 可配置开关


class PanelAchievementUnlock(Base):
    """用户成就解锁记录（幂等，UNIQUE 约束）"""
    __tablename__ = 'panel_achievement_unlocks'

    __table_args__ = (
        UniqueConstraint('user_id', 'ach_key', name='uq_panel_ach_user_key'),
        Index('idx_panel_ach_user', 'user_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('web_users.id'), nullable=False)
    ach_key = Column(String(50), nullable=False)
    unlocked_at = Column(DateTime, default=datetime.now, nullable=False)


class PanelActivity(Base):
    """后台活动 feed（供面板脉搏文案 + 发现页实时动态流）。

    保留策略见 backend/panel_feed.py：每服分区最新 500 条，写入时裁剪。
    """
    __tablename__ = 'panel_activity'

    __table_args__ = (
        Index('idx_panel_activity_time', 'created_at'),
        Index('idx_panel_activity_realm_time', 'realm_id', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(String(300), nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    # 事件类型：scan / enrich / play / system（GET /api/panel/feed 的 type 字段）
    event_type = Column(String(20), default='system', nullable=False)
    # 库名标签（系统事件可为 NULL）
    library = Column(String(200))
    # 归属服（NULL = 全局事件，所有服可见）
    realm_id = Column(Integer)
