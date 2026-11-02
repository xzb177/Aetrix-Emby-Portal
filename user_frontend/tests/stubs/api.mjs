// @/api 桩：只覆盖 stores/user.ts 用到的 tokenStore / authApi。
// authApi.logout 的行为由测试用例通过 __setLogoutBehavior 注入；
// 每次调用都会记录调用时 tokenStore.access 的快照，用以断言“先发请求、后清本地”的顺序。
export const tokenStore = {
  _access: null,
  _refresh: null,
  get access() {
    return this._access
  },
  get refresh() {
    return this._refresh
  },
  set(access, refresh) {
    this._access = access
    if (refresh) this._refresh = refresh
  },
  clear() {
    this._access = null
    this._refresh = null
  },
  __seed(access, refresh) {
    this._access = access
    this._refresh = refresh
  },
}

export const authApi = {
  logoutCalls: [],
  _behavior: 'ok',
  __setLogoutBehavior(behavior) {
    this._behavior = behavior
  },
  __reset() {
    this.logoutCalls = []
    this._behavior = 'ok'
  },
  async logout() {
    // 记录调用瞬间 token 是否还在（顺序断言的关键证据）
    this.logoutCalls.push({ accessAtCall: tokenStore.access })
    if (this._behavior === 'fail') {
      const err = new Error('mock logout 500')
      err.response = { status: 500 }
      throw err
    }
  },
  async login() {
    throw new Error('stub: login not used by this test')
  },
  async getCurrentUser() {
    throw new Error('stub: getCurrentUser not used by this test')
  },
  async register() {
    throw new Error('stub: register not used by this test')
  },
}
