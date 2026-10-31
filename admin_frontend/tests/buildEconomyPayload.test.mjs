// A-066 回归测试：node 直接运行（无需前端测试框架，仓库 admin_frontend 无 vitest/jest）。
// 运行：cd admin_frontend && node tests/buildEconomyPayload.test.mjs
import assert from 'node:assert'
import { buildEconomyPayload } from '../src/utils/buildEconomyPayload.ts'

const always = () => true
let passed = 0
function check(name, actual, expected) {
  assert.deepStrictEqual(actual, expected, name)
  passed += 1
  console.log(`PASS ${name}`)
}

// 核心：空串 int 不出现在 payload（旧代码会落成 '0'）
check('empty int omitted', buildEconomyPayload(
  [{ key: 'checkin_base_min', type: 'int' }, { key: 'checkin_base_max', type: 'int' }],
  { checkin_base_min: '', checkin_base_max: '5' }, always,
), { checkin_base_max: '5' })

// 审计场景：组内改任一字段，未填 int 一律不写 0
check('group save does not zero unset ints', buildEconomyPayload(
  [
    { key: 'checkin_base_min', type: 'int' },
    { key: 'checkin_base_max', type: 'int' },
    { key: 'checkin_streak_bonus', type: 'int' },
    { key: 'vitality_checkin_gain', type: 'int' },
  ],
  { checkin_base_min: '', checkin_base_max: '', checkin_streak_bonus: '2', vitality_checkin_gain: '' }, always,
), { checkin_streak_bonus: '2' })

// 显式 0 是真值，必须保留（与空串区分）
check('explicit zero kept', buildEconomyPayload(
  [{ key: 'vitality_checkin_gain', type: 'int' }],
  { vitality_checkin_gain: '0' }, always,
), { vitality_checkin_gain: '0' })

// 旧有钳制行为不变：负数/非数字/纯空白
check('negative and non-numeric clamp to 0, blank omitted', buildEconomyPayload(
  [
    { key: 'a', type: 'int' },
    { key: 'b', type: 'int' },
    { key: 'c', type: 'int' },
  ],
  { a: '-3', b: 'abc', c: '   ' }, always,
), { a: '0', b: '0' })

// bool 分支口径不变
check('bool semantics unchanged', buildEconomyPayload(
  [
    { key: 'e', type: 'bool' },
    { key: 'f', type: 'bool' },
    { key: 'g', type: 'bool' },
  ],
  { e: '', f: 'false', g: 'yes' }, always,
), { f: 'false', g: 'true' })

// str/secret 原样透传（含空串与掩码，后端自有保留语义）
check('str and secret passthrough', buildEconomyPayload(
  [
    { key: 's', type: 'str' },
    { key: 'k', type: 'secret' },
  ],
  { s: '', k: '******' }, always,
), { s: '', k: '******' })

// 不被后端接收的键不进载荷
check('unsupported skipped', buildEconomyPayload(
  [{ key: 'x', type: 'int' }, { key: 'y', type: 'int' }],
  { x: '3', y: '4' }, (key) => key === 'x',
), { x: '3' })

// ---- A-123：已配值被清空 = 发空串触发后端删行回默认；从未配过则仍省略 ----

// int 已配（original 有值）后清空 → payload 带空串键
check('cleared configured int sends empty string', buildEconomyPayload(
  [{ key: 'checkin_base_min', type: 'int' }],
  { checkin_base_min: '' }, always,
  { checkin_base_min: '5' },
), { checkin_base_min: '' })

// int 从未配过（original 也为空）留空 → 仍省略（A-066 口径不倒退）
check('never-configured int left empty still omitted', buildEconomyPayload(
  [{ key: 'checkin_base_min', type: 'int' }],
  { checkin_base_min: '' }, always,
  { checkin_base_min: '' },
), {})

// 不传 originals（旧调用形态）→ 清空一律省略，与历史行为一致
check('no originals keeps legacy omission', buildEconomyPayload(
  [{ key: 'checkin_base_min', type: 'int' }],
  { checkin_base_min: '' }, always,
), {})

// 混合组：清空的已配 int 发空串、未配 int 省略、有值 int 正常钳制
check('group save mixes cleared, unset and set ints', buildEconomyPayload(
  [
    { key: 'checkin_base_min', type: 'int' },
    { key: 'checkin_base_max', type: 'int' },
    { key: 'checkin_streak_bonus', type: 'int' },
  ],
  { checkin_base_min: '', checkin_base_max: '', checkin_streak_bonus: '2' }, always,
  { checkin_base_min: '3', checkin_base_max: '', checkin_streak_bonus: '1' },
), { checkin_base_min: '', checkin_streak_bonus: '2' })

// 已配 int 改成别的值：与 originals 无关，走原钳制路径
check('configured int changed value unaffected by originals', buildEconomyPayload(
  [{ key: 'checkin_base_min', type: 'int' }],
  { checkin_base_min: '7' }, always,
  { checkin_base_min: '5' },
), { checkin_base_min: '7' })

// bool 已配后清空（表单空值形态为 ''）→ payload 带空串键删行回默认
check('cleared configured bool sends empty string', buildEconomyPayload(
  [{ key: 'checkin_enabled', type: 'bool' }],
  { checkin_enabled: '' }, always,
  { checkin_enabled: 'false' },
), { checkin_enabled: '' })

// bool 从未配过留空 → 仍省略
check('never-configured bool left empty still omitted', buildEconomyPayload(
  [{ key: 'checkin_enabled', type: 'bool' }],
  { checkin_enabled: '' }, always,
  { checkin_enabled: '' },
), {})

// bool 已配正常切换：originals 不影响原有 true/false 口径
check('configured bool toggle unaffected by originals', buildEconomyPayload(
  [{ key: 'checkin_enabled', type: 'bool' }],
  { checkin_enabled: 'false' }, always,
  { checkin_enabled: 'true' },
), { checkin_enabled: 'false' })

console.log(`ALL ${passed} PASSED`)
