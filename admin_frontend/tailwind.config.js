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
          // ui/beautify-20261018：放映机琥珀家族（与 --au-primary 一族同源：400=#e8a84a / 500=#d4923a / 600=#a86f24 / 700=#7f541b）
          50: '#fbf3e6',
          100: '#f6e3c3',
          200: '#efcd94',
          300: '#ecbb6c',
          400: '#e8a84a',
          500: '#d4923a',
          600: '#a86f24',
          700: '#7f541b',
          800: '#573914',
          900: '#2f1f0b',
        },
      },
      // ui/beautify-20261018：圆角 / 字体栈走 --au-r-* / --au-font-* 令牌
      borderRadius: {
        sm: 'var(--au-r-sm)',
        md: 'var(--au-r-md)',
        lg: 'var(--au-r-lg)',
        xl: 'var(--au-r-xl)',
        full: 'var(--au-r-full)',
      },
      fontFamily: {
        sans: ['var(--au-font-sans)'],
        serif: ['var(--au-font-serif)'],
      },
    },
  },
  // aurora.css 已定义页面骨架类（.container 等），Tailwind 同名组件在宽屏下会打架；直接关掉
  corePlugins: {
    container: false,
  },
  plugins: [],
}
