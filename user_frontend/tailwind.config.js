/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          // ui/beautify-20261018：深青家族（与浅色 --au-primary #016a71 / -strong #015a60 / -deep #01404a 同源）
          50: '#ecf5f5',
          100: '#d2e9e9',
          200: '#a3d3d3',
          300: '#6db8b9',
          400: '#2f979a',
          500: '#016a71',
          600: '#015a60',
          700: '#01404a',
          800: '#012f36',
          900: '#01222a',
        },
        accent: {
          // 只有一支强调色：紫 / 粉并入琥珀，语义绿降饱和
          purple: '#e8a84a',
          pink: '#d4923a',
          emerald: '#8fbf8a',
          amber: '#e8a84a',
        },
      },
      fontFamily: {
        // ui/beautify-20261018：走 --au-font-sans 系统字体栈（DESIGN.md §3：不引入 Web 字体）
        sans: ['var(--au-font-sans)'],
      },
      // ui/beautify-20261018：圆角映射 --au-r-* 令牌（卡片 16px / 输入与填充按钮 12px / 幽灵按钮 6px / chips 999px）
      borderRadius: {
        sm: 'var(--au-r-sm)',
        md: 'var(--au-r-md)',
        lg: 'var(--au-r-lg)',
        xl: 'var(--au-r-xl)',
        full: 'var(--au-r-full)',
      },
    },
  },
  // .container 是 aurora.css 的页面骨架（max-width 1080 + gutter）。Tailwind 自带的同名组件
  // 在 ≥1280px 会把 max-width 改成 1280、在样式表里还排在后面——宽屏下首页主体比顶栏宽一截、
  // 贴着屏幕左右边。全站没有用 Tailwind 的 container 语义，直接关掉
  corePlugins: {
    container: false,
  },
  plugins: [],
}
