// @/api/emby 桩：仅覆盖 stores/user.ts 用到的 resetEmbyBaseUrl。
export let resetCalls = 0
export function resetEmbyBaseUrl() {
  resetCalls += 1
}
