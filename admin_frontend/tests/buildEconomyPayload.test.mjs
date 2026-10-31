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

console.log(`ALL ${passed} PASSED`)
