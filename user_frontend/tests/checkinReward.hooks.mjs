// node 模块定制钩子：只把 api/economy.ts 对 './index' 的导入重定向到桩，
// 其余模块走默认解析。由 checkinReward.test.mjs 在动态 import 前注册。
const STUB = './stubs/apiIndex.mjs'

export async function resolve(specifier, context, nextResolve) {
  if (specifier === './index' && (context.parentURL || '').endsWith('/src/api/economy.ts')) {
    return { url: new URL(STUB, import.meta.url).href, shortCircuit: true }
  }
  return nextResolve(specifier, context)
}
