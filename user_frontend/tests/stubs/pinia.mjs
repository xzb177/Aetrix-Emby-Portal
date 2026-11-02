// 最小 pinia 桩：defineStore 返回一个调用 setup 一次的工厂。
// 注意：桩返回 setup 的原始绑定（ref 对象），不做真 pinia 的解包代理；
// 测试里用 store.token.value 这种形式断言，与真 store 的 store.token 等价语义。
export function defineStore(_id, setup) {
  let instance = null
  return function useStore() {
    if (!instance) instance = setup()
    return instance
  }
}
