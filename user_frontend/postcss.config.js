export default {
  plugins: {
    tailwindcss: {},
    // 命名断点只需要展开（定义由 vite 插件从 aurora.css 提取并前置到每个样式模块，
    // 单一真源仍是 aurora.css 顶部；preserve: false 不留自定义查询）。
    'postcss-custom-media': {
      preserve: false,
    },
    autoprefixer: {},
  },
}
