// node 模块定制钩子：把 stores/user.ts 的四个导入重定向到 tests/stubs 下的桩，
// 其余模块走默认解析。由 userLogout.test.mjs 在动态 import user.ts 之前注册。
const REDIRECTS = {
  pinia: './stubs/pinia.mjs',
  vue: './stubs/vue.mjs',
  '@/api': './stubs/api.mjs',
  '@/api/emby': './stubs/emby.mjs',
}

export async function resolve(specifier, context, nextResolve) {
  if (Object.prototype.hasOwnProperty.call(REDIRECTS, specifier)) {
    return {
      url: new URL(REDIRECTS[specifier], import.meta.url).href,
      shortCircuit: true,
    }
  }
  return nextResolve(specifier, context)
}
