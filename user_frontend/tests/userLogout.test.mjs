// PA-048 回归测试：退出登录必须调用后端注销口，不能只清本地。
// 运行：cd user_frontend && node tests/userLogout.test.mjs
// 惯例与 admin_frontend/tests/buildEconomyPayload.test.mjs 一致：node 直接运行，
// 不引入 vitest/jest；stores/user.ts 的 pinia/vue/@/api 导入由 userLogout.hooks.mjs
// 重定向到 tests/stubs 下的桩。Node 24 原生支持 .ts 类型剥离，无需编译。
import { register } from 'node:module'
import assert from 'node:assert'

register('./userLogout.hooks.mjs', import.meta.url)

// localStorage 桩：user.ts 的 logout 会删 aetrix_worn_badge_v1 键。
const ls = new Map()
globalThis.localStorage = {
  getItem: (k) => (ls.has(k) ? ls.get(k) : null),
  setItem: (k, v) => ls.set(k, String(v)),
  removeItem: (k) => ls.delete(k),
}

const { useUserStore } = await import('../src/stores/user.ts')
const { authApi, tokenStore } = await import('./stubs/api.mjs')

let passed = 0
function check(name, actual, expected) {
  assert.deepStrictEqual(actual, expected, name)
  passed += 1
  console.log(`PASS ${name}`)
}

function seedLoggedIn() {
  authApi.__reset()
  tokenStore.__seed('tok-1', 'ref-1')
  ls.set('user', JSON.stringify({ id: 1, username: 'u1' }))
  ls.set('aetrix_worn_badge_v1', 'x')
  const store = useUserStore()
  store.user.value = { id: 1, username: 'u1' }
  store.token.value = 'tok-1'
  return store
}

// 1) 退出时发一次注销 POST，且发请求时本地 token 还在（先发请求、后清本地，
//    否则拦截器拿不到 Bearer，服务端无法定位要吊销的会话）。
{
  const store = seedLoggedIn()
  await store.logout()
  check('logout posts once', authApi.logoutCalls.length, 1)
  check('token still present when logout posted', authApi.logoutCalls[0].accessAtCall, 'tok-1')
  check('local token cleared', tokenStore.access, null)
  check('local refresh cleared', tokenStore.refresh, null)
  check('store token cleared', store.token.value, null)
  check('store user cleared', store.user.value, null)
  check('worn badge key cleared', globalThis.localStorage.getItem('aetrix_worn_badge_v1'), null)
}

// 2) 注销口 500 时也不把用户锁在登录态里：logout 必须 resolve 且本地已清，
//    调用方（AppHeader / AccountSettings / TgBindGuide）随后照常跳转登录页。
{
  const store = seedLoggedIn()
  authApi.__setLogoutBehavior('fail')
  let threw = null
  try {
    await store.logout()
  } catch (e) {
    threw = e
  }
  check('logout resolves even when server logout fails', threw, null)
  check('logout still attempted once on failure', authApi.logoutCalls.length, 1)
  check('local token cleared on failure', tokenStore.access, null)
  check('store user cleared on failure', store.user.value, null)
}

console.log(`\n${passed} passed`)
