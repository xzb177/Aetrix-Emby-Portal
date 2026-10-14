/**
 * 站点品牌（能力：站点与品牌）—— 管理后台侧
 *
 * 与用户端（`user_frontend/src/composables/useBranding.ts`）读同一份配置、落到**同一组令牌**
 * （`--au-primary*`，暗房影院 v6 起两端统一；管理端历史的 `--primary*` 是它们的别名，
 * 见 styles/tokens.css）。主题色由后台自己填，改完刷新即生效，不需要重新构建前端。
 */
import { reactive } from 'vue'
import { siteApi, type Branding } from '@/api/site'

export const DEFAULT_SITE_NAME = 'Aetrix'
export const DEFAULT_THEME_COLOR = '#e8a84a'
/**
 * 「没自定义过」的主题色：后端默认值仍是旧品牌青 #22d3ee，加上现在的放映机琥珀。
 * 命中时不注入任何覆盖——让 aurora.css 自己的深色琥珀 / 白日深琥珀说了算（同用户端口径）。
 */
const LEGACY_DEFAULT_THEME_COLORS = new Set(['#22d3ee', DEFAULT_THEME_COLOR])
// 管理后台页脚展示的版本号（views/Layout.vue 的 foot-version）。构建期写死，不读根目录
// VERSION —— 所以必须与 VERSION 保持一致，由 scripts/check_version.py 门禁守着：
// 它曾停在 v2.33.0 而 VERSION 已到 2.42.6，导致「看界面版本判断线上跑的是哪个构建」
// 变成一个假信号（页脚永远显示同一个值）。
export const APP_VERSION = 'v2.54.0'

export const branding = reactive<Branding>({
  site_name: DEFAULT_SITE_NAME,
  logo_url: '',
  theme_color: DEFAULT_THEME_COLOR,
  seo_title: '',
  seo_description: '',
  seo_keywords: '',
})

let initialized = false

export function siteName(): string {
  return branding.site_name || DEFAULT_SITE_NAME
}

export function adminTitle(page?: string): string {
  return page ? `${page} · ${siteName()} 管理后台` : `${siteName()} 管理后台`
}

function hexToRgb(hex: string): [number, number, number] {
  let value = hex.replace('#', '').trim()
  if (value.length === 3) value = value.split('').map((c) => c + c).join('')
  if (value.length === 8) value = value.slice(0, 6)
  const num = Number.parseInt(value, 16)
  if (!Number.isFinite(num) || value.length !== 6) return [232, 168, 74]
  return [(num >> 16) & 255, (num >> 8) & 255, num & 255]
}

const rgba = (rgb: [number, number, number], alpha: number) =>
  `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`

/** 提亮 / 压暗（主色悬停与按下态） */
function shift(rgb: [number, number, number], factor: number): string {
  const part = (v: number) => Math.max(0, Math.min(255, Math.round(v * factor)))
  return `rgb(${part(rgb[0])}, ${part(rgb[1])}, ${part(rgb[2])})`
}

/** 主色上的文字色：按亮度选深/浅，保证对比度 */
function onColor(rgb: [number, number, number]): string {
  const luminance = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255
  return luminance > 0.6 ? '#1a1205' : '#ffffff'
}

/** 相对亮度（0~1，未做 sRGB 线性化，只用于「够不够亮」的阈值判断） */
function luminance(rgb: [number, number, number]): number {
  return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255
}

function toHex(rgb: [number, number, number]): string {
  const part = (v: number) => v.toString(16).padStart(2, '0')
  return `#${part(rgb[0])}${part(rgb[1])}${part(rgb[2])}`
}

/** 品牌色注入用的 <style>（随主题切换分叉深浅两套，见 applyBrandingTokens） */
let brandStyle: HTMLStyleElement | null = null

/**
 * 品牌主题色注入（v2.42.6 改造，对齐用户端 v2.42.3 的做法）
 *
 * 不再写 `documentElement.style`：inline 自定义属性优先级最高，
 * `html[data-theme='light']` 里的浅色覆盖**永远**赢不了它——
 * 所以外观切到白日时主色仍是深色档的亮色，而浅色主题的说明文字、
 * 描边按钮文字都是「亮青压白底」，对比度只有 ~1.9:1，看不清。
 *
 * 改为注入一段 <style>，深浅各一条规则：
 *
 * - 深色（`:root:root`）用管理员填的原色；
 * - 浅色（`:root:root[data-theme='light']`）把品牌色连乘压暗到白底可读
 *   （亮度阈值 0.42），派生色（悬停/浅底/描边/文字色）跟着有效色重算。
 *
 * 选择器用 `:root:root` 提高特异性（0,2,0 > 浅色块的 0,1,1），
 * 防后续注入的同特异性样式表抢赢；主题切换只改 html 的 data-theme，
 * 两套品牌色即时切换，不需要重新计算。
 */
function applyBrandingTokens() {
  const color = (branding.theme_color || '').trim().toLowerCase()
  if (!color || LEGACY_DEFAULT_THEME_COLORS.has(color)) {
    // 默认主题色：不注入，主题令牌（深色琥珀 / 白日深琥珀）自己说了算
    if (brandStyle) brandStyle.textContent = ''
    return
  }
  const rgb = hexToRgb(branding.theme_color)

  let light = [...rgb] as [number, number, number]
  for (let i = 0; i < 6 && luminance(light) > 0.42; i++) {
    light = [
      Math.max(0, Math.min(255, Math.round(light[0] * 0.82))),
      Math.max(0, Math.min(255, Math.round(light[1] * 0.82))),
      Math.max(0, Math.min(255, Math.round(light[2] * 0.82))),
    ]
  }

  // 与用户端 useBranding.ts 同一组令牌、同一套派生：两端换主题色的效果一致
  const darkRules = [
    `--au-primary:${branding.theme_color}`,
    `--au-primary-strong:${shift(rgb, 0.85)}`,
    `--au-primary-deep:${shift(rgb, 0.55)}`,
    `--au-primary-soft:${rgba(rgb, 0.12)}`,
    `--au-primary-mid:${rgba(rgb, 0.2)}`,
    `--au-primary-border:${rgba(rgb, 0.28)}`,
    '--au-primary-glow:transparent',
    `--au-on-primary:${onColor(rgb)}`,
    `--au-border-focus:${rgba(rgb, 0.55)}`,
  ].join(';')

  const lightRules = [
    `--au-primary:${toHex(light)}`,
    `--au-primary-strong:${shift(light, 0.85)}`,
    `--au-primary-deep:${shift(light, 0.55)}`,
    `--au-primary-soft:${rgba(light, 0.1)}`,
    `--au-primary-mid:${rgba(light, 0.16)}`,
    `--au-primary-border:${rgba(light, 0.26)}`,
    '--au-primary-glow:transparent',
    `--au-on-primary:${onColor(light)}`,
    `--au-border-focus:${rgba(light, 0.55)}`,
  ].join(';')

  if (!brandStyle) {
    brandStyle = document.createElement('style')
    brandStyle.id = 'brand-tokens'
    document.head.appendChild(brandStyle)
  }
  brandStyle.textContent =
    `:root:root{${darkRules}}:root:root[data-theme='light']{${lightRules}}`
}

export function applyBranding() {
  applyBrandingTokens()

  // 标题：路由守卫先写的是「页面名 · 旧站名 管理后台」，这里只换站名那段，
  // 别把页面名（例如「用户管理」）冲掉。
  const suffix = `· ${DEFAULT_SITE_NAME} 管理后台`
  if (document.title.endsWith(suffix)) {
    document.title = `${document.title.slice(0, -suffix.length)}· ${siteName()} 管理后台`
  } else {
    document.title = adminTitle()
  }

  let tag = document.querySelector<HTMLMetaElement>('meta[name="description"]')
  if (branding.seo_description) {
    if (!tag) {
      tag = document.createElement('meta')
      tag.setAttribute('name', 'description')
      document.head.appendChild(tag)
    }
    tag.setAttribute('content', branding.seo_description)
  }
}

export async function initBranding(): Promise<void> {
  if (initialized) return
  initialized = true
  try {
    Object.assign(branding, await siteApi.branding())
  } catch {
    return // 拿不到就用默认品牌，后台不该因为一次请求失败而变白屏
  }
  applyBranding()
}
