// 报到奖励动态实数回归测试（2026-11-05 用户亲点）：奖励框「可得」预估的
// 计算口径——只算基础+连签加成（封顶），不含暴击/惩罚/里程碑。
// 运行：cd user_frontend && node tests/checkinReward.test.mjs
// 惯例与 tests/userLogout.test.mjs 一致：node 直接运行、不引入 vitest/jest；
// api/economy.ts 对 './index' 的导入由 checkinReward.hooks.mjs 重定向到桩。
import { register } from 'node:module'
import assert from 'node:assert'

register('./checkinReward.hooks.mjs', import.meta.url)

const { checkinStreakBonus, estimateCheckinReward, formatCheckinEstimate } =
  await import('../src/api/economy.ts')

let passed = 0
function check(name, actual, expected) {
  assert.deepStrictEqual(actual, expected, name)
  passed += 1
  console.log(`PASS ${name}`)
}

// 生产现行口径：base=3 固定、连签 +1/天、封顶 5。
// streakAfter＝报到后连签数（已报到算明日、未报到算今日，组件恒传 streak+1）。

// 连签加成本身
check('bonus streakAfter=1', checkinStreakBonus(1, 1, 5), 0)
check('bonus streakAfter=2', checkinStreakBonus(2, 1, 5), 1)
check('bonus streakAfter=6 hits cap', checkinStreakBonus(6, 1, 5), 5)
check('bonus streakAfter=100 stays capped', checkinStreakBonus(100, 2, 10), 10)

// 已报到、连报 5 天 → 明日（streakAfter=6）可得 3+5=8（验算样例）
check('checked streak=5 -> tomorrow 8',
  formatCheckinEstimate(estimateCheckinReward(3, 3, 6, 1, 5)), '+8')
// 已报到、连报 1 天 → 明日（streakAfter=2）可得 3+1=4
check('checked streak=1 -> tomorrow 4',
  formatCheckinEstimate(estimateCheckinReward(3, 3, 2, 1, 5)), '+4')
// 未报到、断签 streak=0 → 今日（streakAfter=1）可得 3+0=3
check('unchecked streak=0 -> today 3',
  formatCheckinEstimate(estimateCheckinReward(3, 3, 1, 1, 5)), '+3')
// 未报到、昨日连签 4 → 今日（streakAfter=5）可得 3+4=7
check('unchecked streak=4 -> today 7',
  formatCheckinEstimate(estimateCheckinReward(3, 3, 5, 1, 5)), '+7')
// 基础为范围 3~8 → 显示范围（加成 4）：7~12
check('base range -> +7~12',
  formatCheckinEstimate(estimateCheckinReward(3, 8, 5, 1, 5)), '+7~12')
// 范围入参写反（旧配置容错）：后端会互换，前端同样归一
check('base range reversed normalised',
  formatCheckinEstimate(estimateCheckinReward(8, 3, 5, 1, 5)), '+7~12')

console.log(`\n${passed} passed`)
