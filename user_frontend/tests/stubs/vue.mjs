// 最小 vue 桩：仅覆盖 stores/user.ts 用到的 ref / computed。
export function ref(initial) {
  return { value: initial }
}
export function computed(fn) {
  return {
    get value() {
      return fn()
    },
  }
}
