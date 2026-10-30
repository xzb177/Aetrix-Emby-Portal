<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ facilityStamp('account') }}</span>
        <h1>账号设置</h1>
        <p>改登录密码、看我的会员、管我的设备、绑自己的网盘 —— 账号自己的事都在这儿。</p>
        <p><a class="v4-empty-link" href="#my-drive" @click.prevent="goToMyDrive">去我的网盘绑定 →</a></p>
      </header>

      <!-- 安全设置：修改登录密码（逻辑由 ProfileView 移植，authApi.changePassword） -->
      <section class="v4-sec" aria-label="安全设置">
        <div class="v4-sec-title">安全设置</div>
        <div class="v4-sec-sub">登录密码是进营地的钥匙，定期换一把更安心</div>
        <div class="v4-card acct-card">
          <button type="button" class="acct-row" @click="openChangePwd">
            <span class="acct-row-ic" aria-hidden="true"><Lock :size="16" /></span>
            <span class="acct-row-text">修改登录密码</span>
            <ChevronRight :size="15" class="acct-row-arrow" />
          </button>
        </div>
      </section>

      <!-- 我的收藏：收藏列表页入口（/favorites；标星动作在播放器里做，这里看清单与取消收藏） -->
      <section class=v4-sec aria-label=我的收藏>
        <div class=v4-sec-title>我的收藏</div>
        <div class=v4-sec-sub>收藏过的片子在这里看清单，也可以在这里取消收藏</div>
        <div class=v4-card acct-card>
          <RouterLink class=acct-row as-link to=/favorites>
            <span class=acct-row-ic aria-hidden=true><Star :size=16 /></span>
            <span class=acct-row-text>我的收藏</span>
            <ChevronRight :size=15 class=acct-row-arrow />
          </RouterLink>
        </div>
      </section>

      <!-- 我的会员：直接复用 ProfileSubscriptions.vue（查看到期 / 历史 / 取消订阅） -->
      <section class="v4-sec" aria-label="我的会员">
        <!-- R41：补上标题行，与其他两节结构对齐（标题+副标题） -->
        <div class="v4-sec-title">我的会员</div>
        <!-- 公益服下付费口径文案会误导（付费卡／续订已隐藏），改写为公益服实际内容 -->
        <div class="v4-sec-sub">{{ isFreeRealm ? '公益资格、查看凭证到期都在这里' : '会员到期、历史账单、取消续订都在这里' }}</div>
        <div v-if="subsError" class="v4-err">订阅加载失败，请稍后重试<br><button class="v4-retry" @click="loadSubs">重试</button></div>
        <!-- R41：在途显示骨架屏 —— 请求还没回来时别闪现"还没有会员"（ProfileSubscriptions 的空数据文案） -->
        <div v-else-if="subsLoading" class="v4-skeleton" style="height:180px"></div>
        <ProfileSubscriptions
          v-else
          :active-sub="activeSub"
          :subscriptions="subscriptions"
          :expiring-soon="expiringSoon"
          :is-free-realm="isFreeRealm"
          :realm-note="realmNote"
          :cancelling-sub="cancellingSub"
          :toggling-renew="togglingRenew"
          :welfare-is-welfare="!!userStore.user?.is_welfare"
          :welfare-expires-at="welfareExpiresAt"
          :welfare-status-loaded="welfareStatusLoaded"
          :points-balance="pointsBalance"
          :tg-bound="tgBound"
          :view-unlock="viewUnlock"
          :renewing-welfare="renewingWelfare"
          :renewing-view="renewingView"
          @cancel="cancelSubscription"
          @toggle-renew="toggleAutoRenew"
          @renew-welfare="renewWelfare"
          @renew-view="renewView"
        />
      </section>

      <!-- 发现页卡片展示：服务器卡片怎么摆，用户自选（模式 + 每服显隐，存 localStorage） -->
      <section class="v4-sec" aria-label="发现页卡片展示">
        <div class="v4-sec-title">发现页卡片展示</div>
        <div class="v4-sec-sub">发现页服务器卡片怎么摆，你说了算</div>
        <div class="v4-card">
          <div class="mode-seg" role="group" aria-label="发现页卡片展示模式">
            <button
              v-for="opt in modeOptions"
              :key="opt.v"
              type="button"
              :class="{ 'is-on': mode === opt.v }"
              :aria-pressed="mode === opt.v"
              @click="setMode(opt.v)"
            >
              {{ opt.label }}
            </button>
          </div>
          <p class="mode-hint">自动 = 一个服大卡、多个服总览</p>

          <div v-if="serversLoading && !servers.length" class="srv-empty">正在加载…</div>
          <div v-else-if="serversLoadError" class="srv-empty">服务器清单加载失败<button class="v4-retry" @click="() => loadServers()">重试</button></div>
          <div v-else-if="!servers.length" class="srv-empty">还没有可展示的服务器</div>
          <div v-else>
            <div v-for="s in servers" :key="s.id" class="srv-row">
              <div class="srv-info">
                <div class="srv-name">{{ s.name }}</div>
                <div class="srv-tag">{{ serverTagText(s) }}</div>
              </div>
              <button
                type="button"
                class="srv-switch"
                :class="{ 'is-on': cardPrefs.isVisible(s.id) }"
                role="switch"
                :aria-checked="cardPrefs.isVisible(s.id)"
                :aria-label="`${s.name}：在发现页显示`"
                @click="toggleServer(s.id)"
              ></button>
            </div>
          </div>
        </div>
      </section>

      <!-- 隐私设置：观影碑时长榜上榜开关（存账号） -->
      <section class="v4-sec" aria-label="隐私设置">
        <div class="v4-sec-title">隐私设置</div>
        <div class="v4-sec-sub">谁能在榜单上看到你，你说了算</div>
        <div class="v4-card">
          <div class="srv-row">
            <div class="srv-info">
              <div class="srv-name">观影碑时长榜上榜</div>
              <div class="srv-tag">打开后，你的观影时长会出现在世界观影碑的时长榜上（只显示昵称）；关掉则不上榜，但你自己仍能看到自己的排名</div>
            </div>
            <button
              type="button"
              class="srv-switch"
              :class="{ 'is-on': showOnLeaderboard }"
              :disabled="privacyLoading"
              role="switch"
              :aria-checked="showOnLeaderboard"
              aria-label="观影碑时长榜上榜"
              @click="toggleLeaderboard"
            ></button>
          </div>
          <p v-if="privacyError" class="srv-empty">{{ privacyError }}<button class="v4-retry" @click="loadPrivacy">重试</button></p>
        </div>
      </section>

      <!-- 我的设备：第三方播放器登录设备列表 / 移除（顶上限自救通道，deviceApi） -->
      <section class="v4-sec" aria-label="我的设备">
        <div class="v4-sec-title">我的设备</div>
        <div class="v4-sec-sub">用第三方播放器登录过的设备都在这里，顶到上限自己动手清</div>
        <div class="v4-card">
          <div class="dev-head">
            <span v-if="deviceInfo && deviceInfo.limit" class="dev-count">
              {{ deviceInfo.active_count ?? 0 }} / {{ deviceInfo.limit }} 台
            </span>
            <!-- R38 P3-2：忙碌时文案「刷新中」替代旋转图标（旋转+reduce 伪守卫已删） -->
            <button type="button" class="v4-mini-btn" title="刷新" :disabled="deviceLoading" :aria-busy="deviceLoading" @click="loadDevices">
              <RefreshCw :size="13" /> {{ deviceLoading ? '刷新中' : '刷新' }}
            </button>
          </div>
          <div v-if="deviceLoading && !devices.length" class="v4-skeleton" style="height:120px"></div>
          <div v-else-if="deviceError" class="v4-err">设备列表加载失败，请稍后重试<br><button class="v4-retry" @click="loadDevices">重试</button></div>
          <div v-else-if="!devices.length" class="v4-empty">
            还没有设备记录 —— 用 Emby 客户端（Infuse / Forward 等）登录后会出现在这里
          </div>
          <template v-else>
            <div v-for="d in devices" :key="d.device_id" class="dev-row">
              <div class="dev-main">
                <span class="dev-name">{{ d.name || '未命名设备' }}</span>
                <span class="dev-meta">
                  {{ d.client || '未知客户端' }}
                  <template v-if="d.app_version"> v{{ d.app_version }}</template>
                  · {{ deviceAgo(d.last_seen_at, d.is_blocked) }}
                  <template v-if="d.ip"> · {{ d.ip }}</template>
                </span>
              </div>
              <span v-if="d.is_blocked" class="dev-badge off">已禁用</span>
              <span v-else-if="!d.is_online_recent" class="dev-badge idle">已闲置</span>
              <span v-else class="dev-badge ok">活跃</span>
              <button
                type="button"
                class="dev-remove"
                :class="{ confirming: pendingRemove === d.device_id }"
                :disabled="removingDevice === d.device_id"
                @click="handleRemoveDevice(d)"
              >
                <!-- R37 P3-8：移除忙碌态给人话文案「移除中」，不再只有变暗 -->
                {{ removingDevice === d.device_id ? '移除中' : (pendingRemove === d.device_id ? '确认移除' : '移除') }}
              </button>
            </div>
          </template>
          <p class="dev-tip">
            <template v-if="deviceInfo && deviceInfo.limit">
              账号最多同时使用 {{ deviceInfo.limit }} 台设备（近 {{ deviceInfo.active_days }} 天内登录过）。
              {{ deviceInfo.auto_evict ? '超限时会自动停用最久未用的设备。' : '超限时请先移除不再使用的设备。' }}
            </template>
            <template v-else>移除后该设备登录状态立即失效，需重新输入账号密码。</template>
          </p>
        </div>
      </section>

      <!-- 我的网盘：绑定自己的谷歌盘与 115（BYOS，storageBindingApi）——只放用户端已有设置面，不新增导航入口。
           渲染按后台挂载驱动：先调 GET /storage-bindings/providers 拿 provider 名单，
           只有名单里的块才渲染（同一套 hasProvider 门控管两块，不写死分支）；
           名单里没有的 provider，整个块不渲染，连该 provider 已绑的行也不展示（以名单为准）。
           后端口径见 backend/emby_server/portal.py user_emby_router：列表只出自己的行；
           谷歌盘绑定走 Google 授权（新窗口打开授权地址），回调由 Google 直接调后端 callback 口返回绑定行 JSON，
           所以授权完成后回到本页会自动重拉一次绑定列表（没出现再点“刷新列表”）；服务器未配 OAuth 凭据时后端明文 400，前端原样展示。
           115 绑定走 POST /storage-bindings（provider='pan115'，credential 为 115 Cookie），建时后端同步验活。 -->
      <section v-if="providersError || (providersLoaded && (hasProvider('drive') || hasProvider('pan115')))" id="my-drive" class="v4-sec" aria-label="我的网盘">
        <div class="v4-sec-title">我的网盘</div>
        <div v-if="providersError" class="v4-err">{{ providersError }}<br><button class="v4-retry" @click="loadProviders">重试</button></div>
        <template v-else>
        <div v-if="hasProvider('drive')" class="v4-sec-sub">绑了自己的谷歌盘，播放就走你自己的额度，不走共享池；没绑定只能浏览、不能点播</div>
        <div v-if="hasProvider('drive')" class="v4-card">
          <div class="dev-head">
            <span class="dev-count">已绑 {{ driveBindings.length }} 个谷歌盘</span>
            <button type="button" class="v4-mini-btn" title="刷新" :disabled="driveLoading" :aria-busy="driveLoading" @click="loadDriveBindings">
              <RefreshCw :size="13" /> {{ driveLoading ? '刷新中' : '刷新列表' }}
            </button>
          </div>
          <div v-if="driveLoading && !driveBindings.length" class="v4-skeleton" style="height:120px"></div>
          <div v-else-if="driveError" class="v4-err">{{ driveError }}<br><button class="v4-retry" @click="loadDriveBindings">重试</button></div>
          <div v-else-if="!driveBindings.length" class="v4-empty">
            <p>① 这是什么：绑了自己的谷歌盘，播放就走你自己的额度，不走共享池；没绑定只能浏览、不能点播。</p>
            <p>② 怎么做：起个名字点“绑定谷歌盘”→新窗口去 Google 点同意→回来后列表会自动刷新，没出现再点“刷新列表”。</p>
            <p>③ 绑完怎么验：去播一集，看是不是走你自己的盘。</p>
          </div>
          <template v-else>
            <div v-for="b in driveBindings" :key="b.id" class="dev-row">
              <div class="dev-main">
                <span class="dev-name">{{ b.account_label }}</span>
                <span class="dev-meta">
                  谷歌盘 · {{ driveStatusText(b.status) }}
                  <template v-if="b.error_message"> · {{ b.error_message }}</template>
                  <template v-if="b.needs_rebind"> · 转存需要写权限，请重新授权</template>
                </span>
              </div>
              <span v-if="b.status === 'active'" class="dev-badge ok">可用</span>
              <span v-else class="dev-badge off">不可用</span>
              <button
                v-if="b.status !== 'active'"
                type="button"
                class="dev-remove"
                :disabled="verifyingBinding === b.id"
                @click="handleVerifyBinding(b)"
              >
                {{ verifyingBinding === b.id ? '验证中' : '重新验证' }}
              </button>
              <button
                v-if="b.needs_rebind"
                type="button"
                class="dev-remove"
                :disabled="rebindingId === b.id"
                @click="handleRebindDrive(b)"
              >
                {{ rebindingId === b.id ? '正在打开授权…' : '重新授权' }}
              </button>
              <button
                type="button"
                class="dev-remove"
                :class="{ confirming: pendingUnbind === b.id }"
                :disabled="unbindingId === b.id"
                @click="handleUnbindDrive(b)"
              >
                {{ unbindingId === b.id ? '解绑中' : (pendingUnbind === b.id ? '确认解绑' : '解绑') }}
              </button>
            </div>
            <p class="dev-tip">绑好了，下一步：去播一集试试，走的就是你自己的盘；播不动先点“重新验证”，还不行就解绑重绑。</p>
            <p v-if="driveBindings.some((b) => b.needs_rebind)" class="dev-tip">有谷歌盘还是旧的只读授权，转存到你盘里会失败：点那一行旁边的“重新授权”，去 Google 同意新增的写权限（只覆盖本站创建的文件），同意后转存失败的任务会自动重跑。</p>
          </template>
          <div class="acct-field">
            <label class="acct-label" for="drive-label">给这个盘起个名字</label>
            <input id="drive-label" v-model="driveLabel" type="text" placeholder="比如：我的主力盘" autocomplete="off" />
          </div>
          <p v-if="driveFormError" class="acct-error" role="alert">{{ driveFormError }}</p>
          <p v-if="driveAuthUrl" class="dev-tip">点这里打开 Google 授权页：<a :href="driveAuthUrl" target="_blank" rel="noopener">打开 Google 授权页</a>，同意后关掉那个窗口、回到本页，列表会自动刷新。</p>
          <button type="button" class="acct-btn primary" :disabled="driveStarting" @click="handleBindDrive">
            {{ driveStarting ? '正在打开授权…' : '绑定谷歌盘' }}
          </button>
          <p class="dev-tip">点绑定后会在新窗口打开 Google 授权页，点同意后关掉那个窗口、回到本页，列表会自动刷新，没出现再点“刷新列表”。授权页本身不是本站页面，账号密码只交给 Google。</p>
        </div>
        <div v-if="hasProvider('pan115')" class="v4-card">
          <div class="dev-head">
            <span class="dev-count">已绑 {{ pan115Bindings.length }} 个 115 账号</span>
            <button type="button" class="v4-mini-btn" title="刷新" :disabled="driveLoading" :aria-busy="driveLoading" @click="loadDriveBindings">
              <RefreshCw :size="13" /> {{ driveLoading ? '刷新中' : '刷新列表' }}
            </button>
          </div>
          <div class="v4-sec-sub">绑了自己的 115，播 115 的片子就走你自己的 115 账号现签直链，字节不过服务器；没绑定只能浏览、不能点播</div>
          <div v-if="driveLoading && !pan115Bindings.length" class="v4-skeleton" style="height:120px"></div>
          <div v-else-if="pan115Error" class="v4-err">{{ pan115Error }}<br><button class="v4-retry" @click="loadDriveBindings">重试</button></div>
          <div v-else-if="!pan115Bindings.length" class="v4-empty">
            <p>① 这是什么：绑了自己的 115，播 115 的片子就走你自己的 115 账号现签直链，字节不过服务器；没绑定只能浏览、不能点播。账号必须是 115 VIP，非 VIP 账号绑了也播不了。</p>
            <p>② 怎么做：去 115 网页版取登录 Cookie→粘到下面框里点“绑定 115”→本站只密封存、只用于你自己的播放；Cookie 过期了重绑。</p>
            <p>③ 绑完怎么验：去播一集 115 的片子，看走不走自己的账号。</p>
          </div>
          <template v-else>
            <div v-for="b in pan115Bindings" :key="b.id" class="dev-row">
              <div class="dev-main">
                <span class="dev-name">{{ b.account_label }}</span>
                <span class="dev-meta">
                  115 · {{ driveStatusText(b.status) }}
                  <template v-if="b.error_message"> · {{ b.error_message }}</template>
                </span>
              </div>
              <span v-if="b.status === 'active'" class="dev-badge ok">可用</span>
              <span v-else class="dev-badge off">不可用</span>
              <button
                v-if="b.status !== 'active'"
                type="button"
                class="dev-remove"
                :disabled="verifyingBinding === b.id"
                @click="handleVerifyPan115(b)"
              >
                {{ verifyingBinding === b.id ? '验证中' : '重新验证' }}
              </button>
              <button
                type="button"
                class="dev-remove"
                :class="{ confirming: pendingUnbind === b.id }"
                :disabled="unbindingId === b.id"
                @click="handleUnbindPan115(b)"
              >
                {{ unbindingId === b.id ? '解绑中' : (pendingUnbind === b.id ? '确认解绑' : '解绑') }}
              </button>
            </div>
            <p class="dev-tip">绑好了，下一步：去播一集 115 的片子试试，走的就是你自己的 115 账号；播不动先点“重新验证”，还不行就解绑重绑。</p>
          </template>
          <div class="acct-field">
            <label class="acct-label" for="pan115-label">给这个 115 账号起个名字</label>
            <input id="pan115-label" v-model="pan115Label" type="text" placeholder="比如：我的 115" autocomplete="off" />
          </div>
          <div class="acct-field">
            <label class="acct-label" for="pan115-cookie">115 Cookie</label>
            <input id="pan115-cookie" v-model="pan115Cookie" type="password" placeholder="从 115 网页版登录态里取，粘进来就行" autocomplete="off" />
          </div>
          <p class="dev-tip">取法：先在 115 网页版登录 → 打开浏览器开发者工具 → 应用/存储 → Cookie → 把 115 那一行的“键=值; 键=值”整段复制到上面；只粘 Cookie 值，不要粘请求头、不要带换行。Cookie 会过期，过期了这里会显示已过期，到时候重新粘一次就行。</p>
          <p v-if="pan115FormError" class="acct-error" role="alert">{{ pan115FormError }}</p>
          <button type="button" class="acct-btn primary" :disabled="pan115Creating" @click="handleBindPan115">
            {{ pan115Creating ? '正在验证…' : '绑定 115' }}
          </button>
          <p class="dev-tip">Cookie 只交给本站后端密封保存，只用来播你自己 115 里的东西，不会拿去干别的；它也不会显示在页面上，提交后输入框会立刻清空。115 的 Cookie 会过期，过期了这里会显示已过期，到时候重新粘一次就行。</p>
        </div>
        </template>
      </section>
    </div>

      <!-- 追剧订阅：账号页内订阅与状态（不放首页/发现页；订阅只建订阅行，转存由工人扫集建） -->
      <section v-if="followLoaded || followError" class="v4-sec" aria-label="追剧订阅">
        <div class="v4-sec-title">追剧订阅</div>
        <div class="v4-sec-sub">订阅后新集到货会自动转存到你自己绑定的盘里，转完状态在这里看；无绑定、盘满或转存失败都会明示原因，绝不静默走共享线路。115 与跨盘转存不在本单范围。</div>
        <div v-if="followError" class="v4-err">追剧订阅加载失败，请稍后重试<br><button class="v4-retry" @click="loadFollows">重试</button></div>
        <div v-else-if="!follows.length" class="srv-empty">还没有订阅的剧</div>
        <div v-else>
          <div v-for="f in follows" :key="f.follow_id" class="v4-card">
            <div class="srv-row">
              <div class="srv-info">
                <div class="srv-name">{{ f.series_name || `剧集 #${f.series_item_id}` }}</div>
                <div class="srv-tag">{{ f.status === 'active' ? '订阅中' : '已取消' }}</div>
              </div>
              <button v-if="f.status === 'active'" type="button" class="acct-btn" @click="cancelFollow(f.series_item_id)">取消订阅</button>
            </div>
            <div v-for="ep in f.episodes" :key="ep.episode_id" class="srv-row">
              <div class="srv-info">
                <div class="srv-name">第 {{ ep.season_number ?? '?' }} 季第 {{ ep.episode_number ?? '?' }} 集 · {{ ep.name }}</div>
                <div class="srv-tag">{{ followStateText(ep.state, ep.queue_reason) }}<span v-if="ep.error_detail"> · {{ ep.error_detail }}</span></div>
              </div>
            </div>
          </div>
        </div>
        <div class="acct-field">
          <label class="acct-label" for="follow-series-id">订阅剧集（填剧集条目 id）</label>
          <input id="follow-series-id" v-model="followSeriesId" type="number" placeholder="例如 12345" />
        </div>
        <p v-if="followFormError" class="acct-error" role="alert">{{ followFormError }}</p>
        <button type="button" class="acct-btn primary" :disabled="followSubmitting" @click="handleSubscribeFollow">
          {{ followSubmitting ? '正在订阅…' : '订阅这部剧' }}
        </button>
      </section>

    <!-- 修改登录密码弹窗（由 ProfileView 移植） -->
    <div v-if="showChangePwd" class="acct-mask" @click.self="closeChangePwd" @keydown="onPwdModalKeydown">
      <div class="acct-modal" role="dialog" aria-modal="true" aria-label="修改登录密码">
        <h3 class="acct-modal-title">修改登录密码</h3>
        <div class="acct-field">
          <label class="acct-label" for="pwd-old">当前密码</label>
          <input id="pwd-old" ref="oldPwdInput" v-model="pwdForm.oldPassword" type="password" autocomplete="current-password" />
        </div>
        <div class="acct-field">
          <label class="acct-label" for="pwd-new">新密码</label>
          <input id="pwd-new" v-model="pwdForm.newPassword" type="password" autocomplete="new-password" />
        </div>
        <div class="acct-field">
          <label class="acct-label" for="pwd-confirm">确认新密码</label>
          <input id="pwd-confirm" v-model="pwdForm.confirmPassword" type="password" autocomplete="new-password" />
        </div>
        <p v-if="pwdError" class="acct-error" role="alert">{{ pwdError }}</p>
        <div class="acct-modal-actions">
          <button type="button" class="acct-btn ghost" :disabled="pwdLoading" @click="closeChangePwd">取消</button>
          <button type="button" class="acct-btn primary" :disabled="pwdLoading" @click="handleChangePassword">
            {{ pwdLoading ? '提交中' : '确认修改' }}
          </button>
        </div>
      </div>
    </div>

    <!-- R36：终止订阅二次确认（纸色风格，替代原生 confirm；照抄 SignalView R27） -->
    <Modal v-model="cancelConfirmOpen" size="sm" content-class="paper-confirm">
      <template #header>
        <h3 class="modal-title">终止订阅</h3>
      </template>
      <p class="acct-confirm-text">{{ pendingCancel ? confirmCopy.cancelSubscription(pendingCancel.plan_name) : '' }}</p>
      <template #footer>
        <div class="acct-modal-actions">
          <button type="button" class="acct-btn ghost" @click="cancelConfirmOpen = false">再想想</button>
          <button type="button" class="acct-btn primary" :disabled="cancellingSub !== null" @click="doCancelSubscription">
            {{ cancellingSub !== null ? '终止中' : '确定终止' }}
          </button>
        </div>
      </template>
    </Modal>
  </div>
</template>

<script setup lang="ts">
/**
 * 账号设置设施（/game/account）—— 旧「个人中心」ProfileView 的 P0 功能承接页。
 *
 * 只放三件套（移植 ProfileView 逻辑，不重写）：
 * 1. 修改登录密码（authApi.changePassword，全仓唯一入口）
 * 2. 我的会员（复用 components/profile/ProfileSubscriptions.vue：到期 / 历史 / 取消）
 * 3. 我的设备（deviceApi：设备列表 / 移除，顶上限自救通道）
 * 4. 我的网盘（storageBindingApi：绑定自己的谷歌盘，BYOS；不新增导航入口）
 *
 * 后续已承接（R45）：Emby 多服列表、正在播放远程终止已由接线员页
 * （/game/operator）承接 —— realmCards 多服列表 + 正在播放一键远程结束。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { Lock, ChevronRight, RefreshCw, Star } from 'lucide-vue-next'
import Modal from '@/components/ui/Modal.vue'
import ProfileSubscriptions from '@/components/profile/ProfileSubscriptions.vue'
import { facilityStamp } from '@/config/game'
import { authApi, subscriptionApi, embyApi, isExpiringSoon, type MySubscription, type AccountCard } from '@/api'
import { deviceApi, storageBindingApi, seriesFollowApi, welfareApi, type MyDevice, type MyDevicesResponse, type StorageBinding, type SeriesFollow } from '@/api/economy'
import { tgApi } from '@/api/tg'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'
import { useServerCards, type ServerCardItem } from '@/composables/useServerCards'
import { useServerCardPrefs, type ServerCardMode } from '@/composables/useServerCardPrefs'
import { humanizeError } from '@/composables/usePanelApi'
import { panelApi } from '@/composables/usePanelApi'
import { toastCopy, confirmCopy } from '@/constants/toastMessages'
import { toUserMessage } from '@/utils/toUserMessage'

const userStore = useUserStore()
const toast = useToast()

// ===== 修改登录密码（ProfileView 移植） =====
const showChangePwd = ref(false)
const pwdForm = ref({ oldPassword: '', newPassword: '', confirmPassword: '' })
const pwdLoading = ref(false)
const pwdError = ref('')
const oldPwdInput = ref<HTMLInputElement | null>(null)

/** R36：改密码弹窗键盘闭环（照抄 WishlistView R27 标杆：focus trap + 自动聚焦 + Esc） */
function onPwdModalKeydown(e: KeyboardEvent) {
  if (e.key !== 'Tab' || !showChangePwd.value) return
  const card = (e.currentTarget as HTMLElement).querySelector('.acct-modal')
  if (!card) return
  const els = Array.from(
    card.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'),
  ).filter((el) => el.offsetParent !== null)
  if (els.length === 0) return
  const first = els[0]
  const last = els[els.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}
/** R38 P2-2：弹窗关闭后焦点回到触发按钮（改密码 / 终止订阅共用） */
const lastTrigger = ref<HTMLElement | null>(null)
function onPwdEsc(e: KeyboardEvent) {
  if (e.key === 'Escape' && showChangePwd.value) closeChangePwd()
}
function openChangePwd() {
  // R37 P3-9：重开弹窗清掉旧错误文案
  pwdError.value = ''
  // R38 P2-2：记住触发按钮，关闭后焦点回到它
  lastTrigger.value = document.activeElement as HTMLElement | null
  showChangePwd.value = true
  // R41：弹窗打开时锁住背后页面滚动（照抄 WishlistView R38 P3-11 模式）
  document.body.style.overflow = 'hidden'
  window.addEventListener('keydown', onPwdEsc)
  nextTick(() => oldPwdInput.value?.focus())
}
function closeChangePwd() {
  // R54-10：提交在途时不允许关闭，否则在途请求的失败原因会被清空后静默丢失
  if (pwdLoading.value) return
  showChangePwd.value = false
  // R41：弹窗关闭时释放 body 滚动锁（照抄 WishlistView R38 P3-11 模式）
  document.body.style.overflow = ''
  // R41：关闭时同步清空表单 —— 取消后重开，旧密码不再残留（pwdError 的清理保留在 openChangePwd）
  pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
  window.removeEventListener('keydown', onPwdEsc)
  lastTrigger.value?.focus?.()
}
onUnmounted(() => {
  window.removeEventListener('keydown', onPwdEsc)
  window.removeEventListener('focus', onOAuthReturn)
  document.removeEventListener('visibilitychange', onVisibilityReturn)
  // R41：组件卸载时兜底释放 body 滚动锁，避免锁死
  document.body.style.overflow = ''
})

async function handleChangePassword() {
  const { oldPassword, newPassword, confirmPassword } = pwdForm.value
  if (!oldPassword || !newPassword || !confirmPassword) {
    pwdError.value = '请填写完整'
    return
  }
  if (newPassword.length < 6) {
    pwdError.value = '新密码至少 6 位'
    return
  }
  if (newPassword !== confirmPassword) {
    pwdError.value = '两次输入的新密码不一致'
    return
  }
  pwdError.value = ''
  pwdLoading.value = true
  try {
    await authApi.changePassword({ old_password: oldPassword, new_password: newPassword })
    toast.success(toastCopy.passwordChanged())
    closeChangePwd()
    pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
  } catch (err: any) {
    pwdError.value = toUserMessage(err, 'settingFailed')
  } finally {
    pwdLoading.value = false
  }
}

// ===== 我的会员（ProfileView 移植；展示层全在 ProfileSubscriptions.vue） =====
const subscriptions = ref<MySubscription[]>([])
const subsError = ref('')
// R41：在途标记 —— loadSubs 全程置位，在途显示 .v4-skeleton，不闪现"还没有会员"
const subsLoading = ref(false)
const cancellingSub = ref<number | null>(null)

const activeSub = computed(
  () => subscriptions.value.find(s => s.status === 'active' && s.days_left > 0) || null,
)
const expiringSoon = computed(() => isExpiringSoon(activeSub.value))
const isFreeRealm = computed(() => userStore.isFreeRealm)
const realmNote = computed(() => userStore.realmNote)

async function loadSubs() {
  subsLoading.value = true
  subsError.value = ''
  try {
    subscriptions.value = await subscriptionApi.getMine()
  } catch (e) {
    // 加载失败别装成"没订阅"，留错误态给用户重试
    subsError.value = humanizeError(e)
  } finally {
    subsLoading.value = false
  }
}

/** R36：终止订阅二次确认 —— 原生 confirm() 改用纸色 Modal（照抄 SignalView R27 先例）。
 *  文案继续走 confirmCopy.cancelSubscription，不改文案。 */
/** r48-⑬：自动续订开关 —— 在途标记，防重复点击 */
const togglingRenew = ref<number | null>(null)
async function toggleAutoRenew(s: MySubscription) {
  if (togglingRenew.value !== null) return
  togglingRenew.value = s.id
  try {
    const res = await subscriptionApi.setAutoRenew(s.id, !s.auto_renew)
    toast.success(res.message || (res.auto_renew ? '已开启自动续订' : '已关闭自动续订'))
    await loadSubs()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'toggleAutoRenewFailed'))
  } finally {
    togglingRenew.value = null
  }
}

// ===== 自助续期（公益服）：后端口子现成（公益天数 /redeem、查看 /unlock-view），此前个人中心无入口 =====
// 公益资格到期日取福利状态口（GET /api/points/welfare-status）：失败时不显示日期，不阻断页面。
const accountCard = ref<AccountCard | null>(null)
const tgBound = ref<boolean | null>(null)
const welfareExpiresAt = ref<string | null>(null)
const welfareStatusLoaded = ref(false)
const renewingWelfare = ref<number | null>(null)
const renewingView = ref(false)

const viewUnlock = computed(() => {
  const vp = accountCard.value?.view_permission
  if (!vp) return null
  return {
    granted: vp.granted,
    expiresAt: vp.expires_at,
    unlockPoints: vp.unlock_points,
    unlockDays: vp.unlock_days,
  }
})
const pointsBalance = computed(() => accountCard.value?.view_permission?.points_balance ?? null)

async function loadRenewal() {
  // 两路各自容错：账号卡失败不挡 TG 状态，反之亦然；失败时对应区块降级（查看块隐藏、TG 未知时不拦按钮，以服务端为准）
  try {
    accountCard.value = await embyApi.getAccountCard()
  } catch {
    accountCard.value = null
  }
  try {
    const st = await tgApi.status()
    tgBound.value = st.telegram_id !== null && st.telegram_id !== undefined
  } catch {
    tgBound.value = null
  }
  try {
    const st = await welfareApi.status()
    welfareExpiresAt.value = st.welfare_expires_at
    if (userStore.user) userStore.user.is_welfare = st.is_welfare
    welfareStatusLoaded.value = true
  } catch {
    // 状态口失败时不显示日期，不阻断页面
  }
}

async function renewWelfare(days: 7 | 30) {
  if (renewingWelfare.value !== null) return
  renewingWelfare.value = days
  try {
    const res = await welfareApi.redeem(days)
    welfareExpiresAt.value = res.expires_at
    welfareStatusLoaded.value = true
    if (userStore.user) userStore.user.is_welfare = true
    toast.success(`续期成功，已延至 ${res.expires_at ? res.expires_at.slice(0, 10) : `${days} 天后`}（花费 ${res.cost} 能量，余额 ${res.balance}）`)
    await loadRenewal()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'renewFailed'))
  } finally {
    renewingWelfare.value = null
  }
}

async function renewView() {
  if (renewingView.value) return
  renewingView.value = true
  try {
    const realmId = accountCard.value?.realm_id ?? undefined
    const res = await embyApi.unlockView(realmId)
    toast.success(res.already ? '查看已是解锁状态' : `续费成功${res.expires_at ? `，已延至 ${res.expires_at.slice(0, 10)}` : ''}`)
    await loadRenewal()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'renewFailed'))
  } finally {
    renewingView.value = false
  }
}

const pendingCancel = ref<MySubscription | null>(null)
const cancelConfirmOpen = computed({
  get: () => pendingCancel.value !== null,
  // R38 P2-2：任何关闭路径（"再想想"、Modal 内部关闭按钮/Esc）都把焦点还给触发按钮
  set: (v: boolean) => { if (!v) { pendingCancel.value = null; lastTrigger.value?.focus?.() } },
})
function cancelSubscription(s: MySubscription) {
  lastTrigger.value = document.activeElement as HTMLElement | null
  pendingCancel.value = s
}
async function doCancelSubscription() {
  const s = pendingCancel.value
  if (!s || cancellingSub.value !== null) return
  cancellingSub.value = s.id
  try {
    const res = await subscriptionApi.cancel(s.id)
    toast.success(res.message || toastCopy.subscriptionCancelled())
    cancelConfirmOpen.value = false
    await loadSubs()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'cancelFailed'))
    // 失败时保持确认弹窗打开，用户可直接重试，不丢失上下文（照抄 SignalView R27）
  } finally {
    cancellingSub.value = null
  }
}

// ===== 我的设备（ProfileView 移植：列表 / 两步确认移除） =====
const devices = ref<MyDevice[]>([])
const deviceInfo = ref<MyDevicesResponse | null>(null)
const deviceLoading = ref(false)
const deviceError = ref('')
const removingDevice = ref('')
const pendingRemove = ref('')

async function loadDevices() {
  deviceLoading.value = true
  deviceError.value = ''
  try {
    const res = await deviceApi.mine()
    devices.value = res.devices
    deviceInfo.value = res
  } catch (e) {
    // 加载失败别装成"没设备"，留错误态给用户重试
    deviceError.value = humanizeError(e)
  } finally {
    deviceLoading.value = false
  }
}

async function handleRemoveDevice(device: MyDevice) {
  // R54-12：移除请求在途时直接短路，避免并发删除同一设备
  if (removingDevice.value) return
  if (pendingRemove.value !== device.device_id) {
    pendingRemove.value = device.device_id
    window.setTimeout(() => {
      if (pendingRemove.value === device.device_id) pendingRemove.value = ''
    }, 4000)
    return
  }
  pendingRemove.value = ''
  removingDevice.value = device.device_id
  try {
    const res = await deviceApi.remove(device.device_id)
    toast.success(res.message || toastCopy.deviceRemoved())
    await loadDevices()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'removeFailed'))
  } finally {
    removingDevice.value = ''
  }
}

function deviceAgo(iso?: string | null, isBlocked = false) {
  if (isBlocked) return '已禁用'
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  if (Number.isNaN(diff)) return '—'
  if (diff < 0) return '刚刚'
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

// ===== 我的网盘（用户自带谷歌盘与 115 绑定，BYOS） =====
// 列表口一次拉取，按 provider 分两组展示：drive 走 Google 授权，pan115 走 Cookie 粘贴
// （POST /storage-bindings，provider='pan115'，建时后端同步验活）。
// 绑定流程（谷歌盘）：start 口返回 authorization_url（字段名照抄 drive_oauth.start_drive_oauth），
// 新窗口打开 Google 授权；Google 回调直接调后端 callback 口，不经过本站页面，
// 所以本页不监听回调，改为窗口重新获得焦点/页面重新可见时自动重拉一次
// （只在刚走完 OAuth 的会话里做，避免无意义轮询；手动“刷新列表”仍保留作兜底）。
// 后台挂载驱动的 provider 名单（GET /storage-bindings/providers）：两块共用同一套
// 门控，不写死分支——名单里没有的块整个不渲染，连已绑行也不展示。
const availableProviders = ref<string[]>([])
const providersLoaded = ref(false)
const providersError = ref('')

function hasProvider(provider: string) {
  return availableProviders.value.includes(provider)
}

async function loadProviders() {
  providersError.value = ''
  try {
    const res = await storageBindingApi.providers()
    availableProviders.value = res.providers || []
    providersLoaded.value = true
  } catch (e) {
    // 名单拿不到就没法按规则渲染，留错误态给用户重试，不猜、不 fallback 到全显
    providersError.value = humanizeError(e)
  }
}

const driveBindings = ref<StorageBinding[]>([])
const pan115Bindings = ref<StorageBinding[]>([])
const driveLoading = ref(false)
const driveError = ref('')
const pan115Error = ref('')
const driveLabel = ref('')
const driveFormError = ref('')
const driveStarting = ref(false)
// 正在走重绑的绑定行 id（重绑不新增行，原地换凭据，按钮防重复点）。
const rebindingId = ref<number | null>(null)
// iOS 弹窗被拦时的手动 fallback：弹窗被拦下时把授权地址存这里，
// 页面上渲染一条可点链接，用户手动点开也能走完授权流程。
const driveAuthUrl = ref('')
const pan115Label = ref('')
const pan115Cookie = ref('')
const pan115FormError = ref('')
const pan115Creating = ref(false)
const verifyingBinding = ref<number | null>(null)
const unbindingId = ref<number | null>(null)
const pendingUnbind = ref<number | null>(null)
// 刚走完 Google OAuth 的会话标记：窗口重新获得焦点/页面重新可见时自动重拉一次。
const pendingOAuthReturn = ref(false)

function goToMyDrive() {
  try { window.history.replaceState(null, '', '#my-drive') } catch { /* 忽略 */ }
  const el = document.getElementById('my-drive')
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function onOAuthReturn() {
  if (!pendingOAuthReturn.value || driveLoading.value) return
  pendingOAuthReturn.value = false
  void loadDriveBindings()
}

function onVisibilityReturn() {
  if (document.visibilityState === 'visible') onOAuthReturn()
}

function driveStatusText(status: string) {
  if (status === 'active') return '可用'
  if (status === 'expired') return '已过期'
  if (status === 'invalid') return '不可用'
  return status
}

async function loadDriveBindings() {
  driveLoading.value = true
  driveError.value = ''
  pan115Error.value = ''
  try {
    const res = await storageBindingApi.list()
    driveBindings.value = (res.bindings || []).filter((b) => b.provider === 'drive')
    pan115Bindings.value = (res.bindings || []).filter((b) => b.provider === 'pan115')
  } catch (e) {
    // 加载失败别装成“没绑盘”，留错误态给用户重试
    driveError.value = humanizeError(e)
    pan115Error.value = humanizeError(e)
  } finally {
    driveLoading.value = false
  }
}

async function handleBindDrive() {
  const label = driveLabel.value.trim()
  if (!label) {
    driveFormError.value = '先给这个盘起个名字'
    return
  }
  driveFormError.value = ''
  driveAuthUrl.value = ''
  // iOS Safari 只认点按当下同步开的窗：await 之后再 window.open 会被当弹窗拦。
  // 所以先在手势里同步开一个空窗保住手势，拿到授权地址后再给它指地址。
  // 注意：不能带 noopener——实测 iPhone 上带 noopener 时后续 win.location.href 改地址不生效，
  // 空窗一直停在 about:blank（后端 start 已 200、授权地址已返回，窗就是不动）。
  const win = window.open('about:blank', '_blank')
  driveStarting.value = true
  try {
    const res = await storageBindingApi.startDriveOAuth(label)
    if (!res.authorization_url) {
      if (win && !win.closed) win.close()
      driveFormError.value = '服务器没有返回授权地址，请稍后重试'
      return
    }
    driveAuthUrl.value = res.authorization_url
    if (!win || win.closed) {
      // 弹窗被浏览器拦了：不许谎报成功，留错误态 + 页面上的手动链接
      driveFormError.value = '弹窗被浏览器拦了，点上面“打开 Google 授权页”手动打开，同意后回来，列表会自动刷新'
      return
    }
    win.location.href = res.authorization_url
    pendingOAuthReturn.value = true
    toast.success('已打开 Google 授权页，同意后回到本页，列表会自动刷新')
  } catch (e) {
    // 服务器未配 OAuth 凭据等明文 400，原样展示后端错误，不编造成功
    if (win && !win.closed) win.close()
    driveFormError.value = humanizeError(e)
  } finally {
    driveStarting.value = false
  }
}

async function handleRebindDrive(b: StorageBinding) {
  // 重绑：同名同行原地换凭据（后端 start 带 rebind_binding_id，回调
  // 成功不新增行）；流程与新绑一致——先在手势里同步开空窗保住
  // 手势，拿到授权地址后再指地址，绝不带 noopener（iOS 实测带了
  // 后续改地址不生效）。
  if (rebindingId.value !== null) return
  rebindingId.value = b.id
  driveFormError.value = ''
  driveAuthUrl.value = ''
  const win = window.open('about:blank', '_blank')
  try {
    const res = await storageBindingApi.startDriveOAuth(b.account_label, b.id)
    if (!res.authorization_url) {
      if (win && !win.closed) win.close()
      driveFormError.value = '服务器没有返回授权地址，请稍后重试'
      return
    }
    driveAuthUrl.value = res.authorization_url
    if (!win || win.closed) {
      driveFormError.value = '弹窗被浏览器拦了，点上面“打开 Google 授权页”手动打开，同意后回来，列表会自动刷新'
      return
    }
    win.location.href = res.authorization_url
    pendingOAuthReturn.value = true
    toast.success('已打开 Google 授权页，同意新增的写权限后回到本页，列表会自动刷新，转存失败的任务会自动重跑')
  } catch (e) {
    if (win && !win.closed) win.close()
    driveFormError.value = humanizeError(e)
  } finally {
    rebindingId.value = null
  }
}

async function handleVerifyBinding(b: StorageBinding) {
  if (verifyingBinding.value !== null) return
  verifyingBinding.value = b.id
  try {
    const updated = await storageBindingApi.verify(b.id)
    driveBindings.value = driveBindings.value.map((x) => (x.id === b.id ? updated : x))
    toast.success(updated.status === 'active' ? '验证通过，这个盘可以用了' : '验证后仍不可用，请重新绑定')
  } catch (err: any) {
    toast.error(toUserMessage(err, 'removeFailed'))
  } finally {
    verifyingBinding.value = null
  }
}

async function handleUnbindDrive(b: StorageBinding) {
  // 两步确认，照抄设备移除的口径（4 秒内未确认自动收回）
  if (unbindingId.value !== null) return
  if (pendingUnbind.value !== b.id) {
    pendingUnbind.value = b.id
    window.setTimeout(() => {
      if (pendingUnbind.value === b.id) pendingUnbind.value = null
    }, 4000)
    return
  }
  pendingUnbind.value = null
  unbindingId.value = b.id
  try {
    await storageBindingApi.remove(b.id)
    toast.success('已解绑')
    await loadDriveBindings()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'removeFailed'))
  } finally {
    unbindingId.value = null
  }
}

async function handleBindPan115() {
  const label = pan115Label.value.trim()
  const cookie = pan115Cookie.value.trim()
  if (!label) {
    pan115FormError.value = '先给这个 115 账号起个名字'
    return
  }
  if (!cookie) {
    pan115FormError.value = '先把 115 Cookie 粘进来'
    return
  }
  // 提交前格式预检（后端只做去空白，不校验形态）：Cookie 应是“键=值; 键=值”的一段，
  // 单个词、没有等号、过短的都先拦下来，不等后端验活失败才报错；不回显 Cookie 全文。
  if (cookie.length < 10 || !cookie.includes('=')) {
    pan115FormError.value = '这段看起来不像 Cookie：应是“键=值; 键=值”的一段，请从 115 网页版登录态里重新复制'
    return
  }
  pan115FormError.value = ''
  pan115Creating.value = true
  try {
    // Cookie 只送出一次：无论成败，提交后立刻清空输入框，不留存在页面状态里；
    // 报错只展示后端文案（后端文案不含凭据），绝不回显 Cookie 全文
    const created = await storageBindingApi.createPan115(label, cookie)
    if (created.status === 'active') {
      toast.success('115 绑好了，可以播你自己 115 里的片子了')
    } else {
      // 建时验活失败属三档内时后端仍 201 返回该行：绑是落下了，但不可用，响亮说出来
      pan115FormError.value = created.error_message || '绑定落下了，但验活没过，请检查 Cookie 后重新绑定'
    }
    await loadDriveBindings()
  } catch (e) {
    pan115FormError.value = humanizeError(e)
  } finally {
    pan115Cookie.value = ''
    pan115Creating.value = false
  }
}

async function handleVerifyPan115(b: StorageBinding) {
  // 重验用后端已存的密封 Cookie，不用用户再粘一次；口径照抄谷歌盘重验
  if (verifyingBinding.value !== null) return
  verifyingBinding.value = b.id
  try {
    const updated = await storageBindingApi.verify(b.id)
    pan115Bindings.value = pan115Bindings.value.map((x) => (x.id === b.id ? updated : x))
    toast.success(updated.status === 'active' ? '验证通过，这个 115 账号可以用了' : (updated.error_message ? `验证后仍不可用：${updated.error_message}` : '验证后仍不可用，请重新绑定'))
  } catch (err: any) {
    toast.error(toUserMessage(err, 'removeFailed'))
  } finally {
    verifyingBinding.value = null
  }
}

async function handleUnbindPan115(b: StorageBinding) {
  // 两步确认，照抄设备移除 / 谷歌盘解绑的口径（4 秒内未确认自动收回）
  if (unbindingId.value !== null) return
  if (pendingUnbind.value !== b.id) {
    pendingUnbind.value = b.id
    window.setTimeout(() => {
      if (pendingUnbind.value === b.id) pendingUnbind.value = null
    }, 4000)
    return
  }
  pendingUnbind.value = null
  unbindingId.value = b.id
  try {
    await storageBindingApi.remove(b.id)
    toast.success('已解绑')
    await loadDriveBindings()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'removeFailed'))
  } finally {
    unbindingId.value = null
  }
}

// ===== 发现页卡片展示（模式 + 每服显隐，偏好存 localStorage） =====
const cardPrefs = useServerCardPrefs()
const { mode, setMode, toggleServer } = cardPrefs
const { servers, load: loadServers, loading: serversLoading, loadError: serversLoadError } = useServerCards()

const modeOptions: { v: ServerCardMode; label: string }[] = [
  { v: 'auto', label: '自动' },
  { v: 'cards', label: '大卡' },
  { v: 'overview', label: '总览' },
]

const ACCESS_LABEL: Record<ServerCardItem['accessMode'], string> = {
  paid: '付费服',
  free: '公益服',
}

function serverTagText(s: ServerCardItem): string {
  return ACCESS_LABEL[s.accessMode]
}

/* ---------- 隐私：观影碑时长榜上榜（存账号） ---------- */
const showOnLeaderboard = ref(true)
const privacyLoading = ref(false)
const privacyError = ref('')

async function loadPrivacy() {
  privacyLoading.value = true
  privacyError.value = ''
  try {
    const d = await panelApi.privacyGet()
    showOnLeaderboard.value = d.show_on_leaderboard !== false
  } catch (e) {
    privacyError.value = humanizeError(e)
  } finally {
    privacyLoading.value = false
  }
}

async function toggleLeaderboard() {
  if (privacyLoading.value) return
  const next = !showOnLeaderboard.value
  privacyLoading.value = true
  try {
    const d = await panelApi.privacySet(next)
    showOnLeaderboard.value = d.show_on_leaderboard !== false
    toast.success(next ? '已允许上榜' : '已关掉上榜')
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    privacyLoading.value = false
  }
}

// ===== 追剧订阅（账号页内，订阅与状态；不放首页/发现页） =====
const follows = ref<SeriesFollow[]>([])
const followLoaded = ref(false)
const followError = ref('')
const followSeriesId = ref('')
const followFormError = ref('')
const followSubmitting = ref(false)

function followStateText(state: string, queueReason: string | null): string {
  if (state === 'ready') return '已转存到你的盘'
  if (state === 'pending') return '转存排队中'
  if (state === 'running') return '转存进行中'
  if (state === 'done') return '转存完成'
  if (state === 'failed') return '转存失败'
  if (queueReason === 'no_binding') return '未绑定网盘，绑定后下一轮自动转存'
  if (queueReason === 'unsupported_provider_115') return '115 源不在本单范围'
  if (queueReason === 'unknown_provider') return '来源不明，暂不转存'
  return '等待下一轮扫集'
}

async function loadFollows() {
  followError.value = ''
  try {
    const res = await seriesFollowApi.list()
    follows.value = res.follows || []
    followLoaded.value = true
  } catch (e) {
    // 开关关时后端 404：整块隐藏，不当成加载失败反复提示
    const status = (e as { response?: { status?: number } }).response?.status
    if (status === 404) {
      followLoaded.value = false
      followError.value = ''
    } else {
      followError.value = humanizeError(e)
    }
  }
}

async function handleSubscribeFollow() {
  followFormError.value = ''
  const id = Number(followSeriesId.value)
  if (!id) {
    followFormError.value = '请先填剧集条目 id'
    return
  }
  followSubmitting.value = true
  try {
    await seriesFollowApi.subscribe(id)
    followSeriesId.value = ''
    await loadFollows()
  } catch (e) {
    followFormError.value = humanizeError(e)
  } finally {
    followSubmitting.value = false
  }
}

async function cancelFollow(seriesItemId: number) {
  try {
    await seriesFollowApi.unsubscribe(seriesItemId)
    await loadFollows()
  } catch (e) {
    followFormError.value = humanizeError(e)
  }
}

onMounted(() => {
  loadFollows()
  loadSubs()
  if (userStore.isFreeRealm) void loadRenewal()
  loadDevices()
  loadProviders()
  loadDriveBindings()
  loadPrivacy()
  if (!serversLoading.value && !servers.value.length) void loadServers()
  // 从播放器选择页带 #my-drive 跳过来时，等网盘区段渲染后再落点；
  // 区段由 providersLoaded 门控，watch 里同样处理（见下）。
  window.addEventListener('focus', onOAuthReturn)
  document.addEventListener('visibilitychange', onVisibilityReturn)
})

watch(providersLoaded, (loaded) => {
  if (loaded && window.location.hash === '#my-drive') {
    void nextTick(() => {
      document.getElementById('my-drive')?.scrollIntoView({ block: 'start' })
    })
  }
})
</script>

<style scoped>
/* 账号设置设施：v4 纸色（只用 v4-paper.css 定义的 token，不硬编码色值） */

/* 追剧订阅区是 .v4-scope 的直属子级（不在 .v4-page 内），吃不到 .v4-page 的
   底栏留白，最后一截会被固定底栏盖住且滑不上来；这里按 .v4-page 同款口径补齐。 */
.v4-scope > section[aria-label="追剧订阅"] {
  padding-bottom: calc(76px + env(safe-area-inset-bottom));
}

.acct-card { padding: 6px; }

.acct-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 52px;
  padding: 10px 12px;
  background: transparent;
  border: none;
  border-radius: var(--paper-r-sm, 8px);
  cursor: pointer;
  color: var(--ink);
  font-size: 15px;
  text-align: left;
}
.acct-row:active { background: var(--paper2); }
.acct-row.as-link { text-decoration: none; box-sizing: border-box; }

.acct-row-ic {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border-radius: var(--paper-r-sm, 8px);
  background: var(--paper2);
  color: var(--ink2);
}
.acct-row-text { flex: 1; font-weight: 500; }
.acct-row-arrow { color: var(--ink2); flex-shrink: 0; }

/* ---- 我的设备 ---- */
.dev-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.dev-count {
  font-size: 12px;
  color: var(--ink2);
  font-variant-numeric: tabular-nums;
}
.v4-mini-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  /* R41：纵向 padding 13px→15px —— 原注释声称 44px，实算约 40px，提到 15px 才诚实达标（视觉不变） */
  padding: 15px 10px;
  margin: -8px 0;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-full, 999px);
  color: var(--ink2);
  font-size: 12px;
  cursor: pointer;
}
/* R38 P3-2：.spinning 旋转动画已删（忙碌改走文案「刷新中」）；reduce 伪守卫一并删 */

.dev-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 0;
}
.dev-row + .dev-row { border-top: 1px solid var(--line); }
.dev-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.dev-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dev-meta {
  font-size: 12px;
  color: var(--ink2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.dev-badge {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: var(--paper-r-full, 999px);
  font-size: 12px;
  white-space: nowrap;
  background: var(--paper2);
  color: var(--ink2);
}
.dev-badge.ok { background: var(--au-success-soft); color: var(--green); }
.dev-badge.off { background: var(--au-danger-soft); color: var(--red); }
.dev-remove {
  flex-shrink: 0;
  /* R41：纵向 padding 13px→15px —— 原注释声称 44px，实算约 40px，提到 15px 才诚实达标（视觉不变） */
  padding: 15px 10px;
  margin: -7px 0;
  background: transparent;
  border: none;
  border-radius: var(--paper-r-sm, 8px);
  color: var(--ink2);
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
}
.dev-remove:active:not(:disabled) { background: var(--paper2); }
.dev-remove:disabled { opacity: 0.5; cursor: not-allowed; }
.dev-remove.confirming { color: var(--red); font-weight: 600; }
.dev-tip {
  margin: 10px 0 0;
  padding-top: 10px;
  border-top: 1px solid var(--line);
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink2);
}

/* ---- 改密码弹窗（纸色） ---- */
.acct-mask {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 24px;
  padding: max(24px, env(safe-area-inset-top)) max(24px, env(safe-area-inset-right)) max(24px, env(safe-area-inset-bottom)) max(24px, env(safe-area-inset-left));
  overflow-y: auto;
  background: var(--scrim);
  z-index: 100;
}
.acct-modal {
  width: 100%;
  max-width: 340px;
  margin: auto;
  max-height: calc(100% - 48px);
  max-height: calc(100dvh - 48px);
  overflow-y: auto;
  padding: 20px;
  background: var(--paper-card);
  border: 1.5px solid var(--ink);
  border-radius: 14px;
  box-shadow: 4px 4px 0 rgba(42, 38, 32, 0.15);
}
.acct-modal-title {
  margin: 0 0 14px;
  font-size: 17px;
  font-weight: 600;
  color: var(--ink);
}
.acct-field { margin-bottom: 12px; }
.acct-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--ink2);
}
.acct-field input {
  width: 100%;
  height: 44px;
  padding: 0 12px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-sm, 8px);
  color: var(--ink);
  font-size: 16px;
  outline: none;
  box-sizing: border-box;
}
.acct-field input:focus { border-color: var(--ink); }
.acct-field input:focus-visible { outline: 2px solid var(--ink); outline-offset: 1px; }
.acct-error {
  margin: 0 0 10px;
  padding: 8px 12px;
  background: var(--au-danger-soft);
  border-radius: var(--paper-r-sm, 8px);
  color: var(--red);
  font-size: 13px;
}
.acct-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}
.acct-btn {
  height: 44px;
  padding: 0 16px;
  border-radius: var(--paper-r-md, 10px);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}
.acct-btn.ghost {
  background: var(--paper2);
  border: 1px solid var(--line);
  color: var(--ink2);
}
.acct-btn.primary {
  background: var(--ink);
  border: 1px solid var(--ink);
  color: var(--on-dark);
  font-weight: 600;
}
.acct-btn:disabled { opacity: 0.5; cursor: not-allowed; }
/* R36：终止订阅确认弹窗的正文（走 .paper-confirm 纸色弹窗，文字排版本地样式） */
.acct-confirm-text { font-size: 14px; line-height: 1.9; color: var(--ink); margin: 0; }

/* 发现页卡片展示：模式三段选择 + 每服显隐开关（只用 --paper-* token） */
.mode-seg {
  display: flex;
  border: 1.5px solid var(--paper-ink);
  border-radius: 8px;
  overflow: hidden;
}
.mode-seg button {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  background: none;
  border: 0;
  font-size: 12px;
  color: var(--paper-ink);
  cursor: pointer;
}
.mode-seg button + button {
  border-left: 1px solid var(--paper-line);
}
.mode-seg button.is-on {
  background: var(--paper-primary);
  color: #2A2620;
  font-weight: 700;
}
.mode-hint {
  margin: 8px 0 0;
  font-size: 11px;
  color: var(--paper-ink-2);
}
.srv-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 0;
}
.srv-row + .srv-row {
  border-top: 1px dashed var(--paper-line);
}
.srv-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--paper-ink);
}
.srv-tag {
  font-size: 11px;
  color: var(--paper-ink-2);
}
.srv-switch {
  position: relative;
  flex: none;
  width: 48px;
  height: 44px;
  padding: 0;
  border-radius: 999px;
  border: 0;
  background: none;
  cursor: pointer;
}
.srv-switch::before {
  content: '';
  position: absolute;
  left: 6px;
  top: 12px;
  width: 36px;
  height: 20px;
  border-radius: 999px;
  border: 1.5px solid var(--paper-ink);
  background: var(--paper-card);
}
.srv-switch.is-on::before {
  background: var(--paper-primary);
}
.srv-switch::after {
  content: '';
  position: absolute;
  top: 14px;
  left: 8px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--paper-card);
  border: 1.5px solid var(--paper-ink);
}
.srv-switch.is-on::after {
  left: auto;
  right: 8px;
  background: var(--paper-ink);
}
.srv-empty {
  padding: 10px 0;
  font-size: 12px;
  color: var(--paper-ink-2);
}
</style>
