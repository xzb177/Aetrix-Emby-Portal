#!/usr/bin/env node
/**
 * check-home-css.mjs — 首页 CSS 双向检查（home-refactor 设计方案 §2.3）
 *
 * 扫描 4 项（无新依赖，CI 可跑）：
 *  1. 失效类扫描：template 用到的静态 class，在自身 <style scoped> 无定义、
 *     也不在 L2 全局白名单（styles/*.css） → 报错（scoped 不穿透 = 样式失效）
 *  2. 死代码扫描：scoped 内定义的类，在自身 template 无使用 → 死代码候选清单
 *     （人工复核后删除；白名单 DEAD_ALLOWLIST 保护跨组件共享类）
 *  3. 跨文件 @media 扫描：A 组件类名前缀出现在 B 文件的 @media 块 → 报错
 *  4. 硬编码扫描：@media 中 px 值不在断点表则报错；
 *     <style> 出现 #hex 颜色 / 非 1px 边框的 px 间距字号圆角 → 告警
 *
 * 用法：node scripts/check-home-css.mjs [--json]
 * 退出码：0 = 无报错（告警不影响退出码），1 = 有报错。
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const asJson = args.includes('--json');

// ---- 配置 ----
const HOME_VIEW = 'src/views/HomeView.vue';
const HOME_DIR = 'src/components/home';
const GLOBAL_CSS = [
  'src/styles/aurora.css',
  'src/styles/neo-noir-tokens.css',
  'src/styles/index.css',
  'src/styles/mobile.css',
];
// 断点表（设计方案 §3.1）
const BREAKPOINTS = new Set([360, 640, 768, 1024]);
// 死代码白名单：确实跨组件共享、不能删的类
const DEAD_ALLOWLIST = new Set([
  // 上浮到 L2（aurora.css 全局）的共享类（设计方案 §2.4）
  'program-head', 'program-eyebrow', 'program-row', 'program-title', 'program-more',
]);
// 组件类名前缀 → 所属组件（用于跨文件 @media 扫描）
const PREFIX_OWNER = {
  'hero-': 'HomeHero.vue',
  'ticket': 'HomeTicketCard.vue', // .ticket / .ticket-*
  'stub-': 'HomeTicketCard.vue',
  'sk-stat': 'HomeTicketCard.vue',
  'sk-stub': 'HomeTicketCard.vue',
  'resume': 'HomeContinueWatching.vue', // .resume / .resume-*
  'whisper': 'HomeHero.vue',
  'notice-': 'HomeBanner.vue',
  'banner-': 'HomeBanner.vue',
  'recent': 'HomeTodayReleases.vue',
  'playing': 'HomeNowPlaying.vue', // .playing / .playing-*
  'live-dot': 'HomeNowPlaying.vue',
  'todo-': 'HomeTodoList.vue',
  'help-': 'HomeGuideList.vue',
  'stop-confirm': 'StopSessionDialog.vue',
  'welcome': 'WelcomeBackCard.vue',
};

// ---- 工具 ----
const read = (p) => readFileSync(join(ROOT, p), 'utf8');

function extractBlock(src, tag) {
  // 处理嵌套 <template>（如 <template v-if> / <template #slot>）：深度计数
  const openRe = new RegExp(`<${tag}(?=[\\s>])`, 'g');
  const closeRe = new RegExp(`</${tag}>`, 'g');
  const m = openRe.exec(src);
  if (!m) return '';
  let depth = 0, i = m.index, start = -1;
  const combined = new RegExp(`<${tag}(?=[\\s>])|</${tag}>`, 'g');
  combined.lastIndex = i;
  let t;
  while ((t = combined.exec(src))) {
    if (t[0].startsWith('</')) {
      depth--;
      if (depth === 0) return src.slice(start, t.index);
    } else {
      if (depth === 0) {
        // 跳过开始标签的属性，直至 '>'
        const gt = src.indexOf('>', combined.lastIndex);
        start = gt + 1;
      }
      depth++;
    }
  }
  return '';
}

function splitSfc(src) {
  const tpl = extractBlock(src, 'template');
  const stl = extractBlock(src, 'style');
  const scr = extractBlock(src, 'script');
  return { tpl, stl, scr };
}

// Vue <Transition name="x"> 生成的 6 个过渡类：Vue 在过渡过程中会用到，
// 但定义是可选的（enter-to / leave-from 常省略），不计入失效类扫描
const TRANSITION_GENERATED = new Set();

// 模板中静态 class="a b"（仅 template 区；排除 :class / v-bind:class）
function usedClasses(tpl) {
  const used = new Set();
  for (const m of tpl.matchAll(/(?<![\w:-])class="([^"]*)"/g)) {
    for (const c of m[1].split(/\s+/)) if (c) used.add(c);
  }
  // :class / v-bind:class：只提取引号内的静态字符串与对象字面量键名
  for (const m of tpl.matchAll(/(?::|v-bind:)class="([^"]*)"/g)) {
    const expr = m[1];
    for (const s of expr.matchAll(/['"]([^'"]+)['"]/g)) {
      for (const c of s[1].split(/\s+/)) if (c) used.add(c);
    }
    for (const s of expr.matchAll(/[{,]\s*(?:'([^']+)'|"([^"]+)"|([A-Za-z_][\w-]*))\s*:/g)) {
      used.add(s[1] ?? s[2] ?? s[3]);
    }
  }
  for (const m of tpl.matchAll(/<Transition[^>]*\bname="([^"]+)"/g)) {
    const n = m[1];
    for (const sfx of ['enter-active', 'enter-from', 'enter-to', 'leave-active', 'leave-from', 'leave-to']) {
      used.add(`${n}-${sfx}`);
      TRANSITION_GENERATED.add(`${n}-${sfx}`);
    }
  }
  return used;
}

// style 块中定义的选择器类名（含 @media 内）
function definedClasses(css) {
  const defs = new Set();
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, '');
  for (const m of noComments.matchAll(/\.(-?[_a-zA-Z][_a-zA-Z0-9-]*)/g)) {
    defs.add(m[1]);
  }
  return defs;
}

function extractMediaBlocks(css) {
  const blocks = [];
  const re = /@media\s*([^{]+)\{/g;
  let m;
  while ((m = re.exec(css))) {
    let depth = 1, i = re.lastIndex;
    while (i < css.length && depth > 0) {
      if (css[i] === '{') depth++;
      else if (css[i] === '}') depth--;
      i++;
    }
    blocks.push({ cond: m[1].trim(), body: css.slice(re.lastIndex, i - 1) });
    re.lastIndex = i;
  }
  return blocks;
}

// ---- 收集 ----
const components = [HOME_VIEW];
if (existsSync(join(ROOT, HOME_DIR))) {
  for (const f of readdirSync(join(ROOT, HOME_DIR))) {
    if (f.endsWith('.vue')) components.push(`${HOME_DIR}/${f}`);
  }
}
const info = new Map(); // path -> { used, defined, sfc }
for (const p of components) {
  const sfc = splitSfc(read(p));
  info.set(p, {
    used: usedClasses(sfc.tpl),
    defined: definedClasses(sfc.stl),
    sfc,
    name: p.split('/').pop(),
  });
}
const globalDefs = new Set();
for (const p of GLOBAL_CSS) {
  if (existsSync(join(ROOT, p))) for (const c of definedClasses(read(p))) globalDefs.add(c);
}
// L2 白名单：全局类（跨页面复用）视为已定义
const GLOBAL_WHITELIST = globalDefs;

const errors = [];
const warnings = [];
const deadCandidates = [];

// ---- 扫描 1：失效类 ----
for (const [p, it] of info) {
  for (const c of it.used) {
    if (TRANSITION_GENERATED.has(c)) continue; // 过渡态类定义可选，不算失效
    if (!it.defined.has(c) && !GLOBAL_WHITELIST.has(c)) {
      errors.push(`[失效类] ${it.name} template 用了 .${c}，但自身 scoped 无定义、全局样式也无定义 → 样式不生效`);
    }
  }
}

// ---- 扫描 2：死代码 ----
for (const [p, it] of info) {
  for (const c of it.defined) {
    if (DEAD_ALLOWLIST.has(c)) continue;
    if (!it.used.has(c) && !GLOBAL_WHITELIST.has(c)) {
      deadCandidates.push(`${it.name} :: .${c}`);
    }
  }
}

// ---- 扫描 3：跨文件 @media ----
// 真正的"跨文件"：@media 块里出现了别的组件所拥有的类（该类在属主组件 scoped 有定义）
// → 响应式样式必须跟组件走（随组件搬迁或删除）
for (const [p, it] of info) {
  for (const b of extractMediaBlocks(it.sfc.stl)) {
    const bodyClasses = definedClasses(b.body);
    for (const [prefix, owner] of Object.entries(PREFIX_OWNER)) {
      if (owner === it.name) continue;
      const ownerInfo = info.get([...info.keys()].find((k) => k.endsWith('/' + owner)));
      if (!ownerInfo) continue;
      for (const c of bodyClasses) {
        if ((c === prefix || c.startsWith(prefix)) && ownerInfo.defined.has(c)) {
          errors.push(`[跨文件@media] ${it.name} 的 @media(${b.cond}) 里出现了 ${owner} 的类 .${c} → 响应式样式必须跟组件走`);
          break;
        }
      }
    }
  }
}

// ---- 扫描 4a：裸断点值（只查宽度断点；900px 是 App 壳层专用→告警并注明） ----
// 首页作用域（本 PR 统一范围）：报错；其他文件：告警（项目级统一是 follow-up）
const HOME_SCOPE = new Set([...components, 'src/styles/aurora.css']);
const scanFiles = [...components, ...GLOBAL_CSS.filter((p) => existsSync(join(ROOT, p)))];
for (const p of scanFiles) {
  const src = info.has(p) ? info.get(p).sfc.stl : read(p);
  const where = p.split('/').pop();
  const inScope = HOME_SCOPE.has(p);
  for (const b of extractMediaBlocks(src)) {
    if (!/width/i.test(b.cond)) continue; // 高度/方向/悬停等非宽度断点不查
    for (const m of b.cond.matchAll(/(\d+(?:\.\d+)?)px/g)) {
      const v = parseFloat(m[1]);
      if (BREAKPOINTS.has(v)) continue;
      if (v === 900) {
        warnings.push(`[壳层断点] ${where} @media(${b.cond}) 用 900px：App 壳层专用断点（顶栏第二行滑动选项卡），页面组件不许用`);
      } else if (inScope) {
        errors.push(`[裸断点] ${where} @media(${b.cond}) 出现 ${v}px，不在断点表 {360,640,768,1024}`);
      } else {
        warnings.push(`[裸断点·非首页] ${where} @media(${b.cond}) 出现 ${v}px：非首页范围，待项目级断点统一 follow-up`);
      }
    }
  }
}

// ---- 扫描 5：--neo-safe-bottom 野路线冻结 ----
for (const [p, it] of info) {
  if (/--neo-safe-bottom/.test(it.sfc.stl) || /--neo-safe-bottom/.test(it.sfc.tpl)) {
    warnings.push(`[野路线] ${it.name} 使用了已冻结的 --neo-safe-bottom → 改走 --au-dock-space`);
  }
}

// ---- 扫描 4b：硬编码颜色 / px 间距 ----
for (const [p, it] of info) {
  const css = it.sfc.stl.replace(/\/\*[\s\S]*?\*\//g, '');
  const hexes = new Set();
  for (const m of css.matchAll(/#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b/g)) hexes.add('#' + m[1]);
  for (const h of hexes) warnings.push(`[硬编码颜色] ${it.name} 出现 ${h} → 应走 var(--au-*)`);
  for (const m of css.matchAll(/(margin|padding|font-size|border-radius|gap|line-height|letter-spacing)\s*:[^;]*?(\d+(?:\.\d+)?)px/gi)) {
    warnings.push(`[硬编码间距] ${it.name} ${m[1]}: ...${m[2]}px → 应走 --au-space-*/--au-* 令牌`);
  }
}

// ---- 输出 ----
const result = {
  errors,
  warnings,
  deadCandidates: deadCandidates.sort(),
  summary: {
    components: components.length,
    errors: errors.length,
    warnings: warnings.length,
    deadCandidates: deadCandidates.length,
  },
};

if (asJson) {
  console.log(JSON.stringify(result, null, 2));
} else {
  console.log('=== check-home-css: 双向检查结果 ===\n');
  console.log(`--- 扫描 1 · 失效类 (${errors.filter((e) => e.startsWith('[失效类]')).length}) ---`);
  for (const e of errors.filter((e) => e.startsWith('[失效类]'))) console.log('  ' + e);
  console.log(`\n--- 扫描 3 · 跨文件 @media (${errors.filter((e) => e.startsWith('[跨文件@media]')).length}) ---`);
  for (const e of errors.filter((e) => e.startsWith('[跨文件@media]'))) console.log('  ' + e);
  console.log(`\n--- 扫描 4a · 裸断点 (${errors.filter((e) => e.startsWith('[裸断点]')).length} 报错) ---`);
  for (const e of errors.filter((e) => e.startsWith('[裸断点]'))) console.log('  ' + e);
  console.log(`\n--- 扫描 2 · 死代码候选 (${deadCandidates.length}) ---`);
  for (const d of result.deadCandidates) console.log('  ' + d);
  console.log(`\n--- 告警 (${warnings.length}) ---`);
  for (const w of warnings.slice(0, 40)) console.log('  ' + w);
  if (warnings.length > 40) console.log(`  ... 还有 ${warnings.length - 40} 条`);
  console.log(`\n=== 汇总: 报错 ${errors.length} · 告警 ${warnings.length} · 死代码候选 ${deadCandidates.length} ===`);
  if (errors.length) { console.log('RESULT: FAIL'); process.exit(1); }
  console.log('RESULT: PASS');
}
