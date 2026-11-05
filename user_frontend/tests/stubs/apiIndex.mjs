// api/index 的最小桩：只供 checkinReward.test.mjs 导入 api/economy.ts 时占位
// 其默认导出的 axios 实例（economy.ts 里各 API 对象在模块级引用它）。
// 纯函数（checkinStreakBonus 等）不经它发请求。
export default {
  get: async () => null,
  post: async () => null,
  put: async () => null,
  delete: async () => null,
  interceptors: { request: { use() {} }, response: { use() {} } },
}
