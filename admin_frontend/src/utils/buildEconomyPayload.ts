/**
 * 经济分组保存载荷构造：Settings.vue saveGroupCore 的纯函数抽出。
 *
 * A-066 回归锚点：int 字段空串不许落成 '0'。从未配过的字段空串时不传
 * 该键，后端保持无行，读取侧回退各键自己的代码缺省。
 * A-123：已配过的 int/bool 字段被管理员清空时，必须带空串发给后端
 * （后端 PUT 口径：int/bool 键空串 = 删行回退默认），否则删行能力永远
 * 用不上、旧值在刷新后复活。区分「从有值清空」与「一直为空」以
 * ``originals``（加载时的服务端生效值）该键是否非空为准。
 */
export type EconomyPayloadFieldType = 'bool' | 'int' | 'str' | 'secret'

export interface EconomyPayloadField {
  key: string
  type: EconomyPayloadFieldType
}

export function buildEconomyPayload(
  fields: EconomyPayloadField[],
  settings: Record<string, unknown>,
  isSupported: (key: string) => boolean,
  originals: Record<string, unknown> = {},
): Record<string, string> {
  const payload: Record<string, string> = {}
  for (const f of fields) {
    if (!isSupported(f.key)) continue
    const raw = String(settings[f.key] ?? '').trim()
    if (f.type === 'int') {
      if (raw === '') {
        // 已配过又被清空 → 发空串删行回默认；从未配过 → 省略（A-066）
        if (String(originals[f.key] ?? '').trim() !== '') payload[f.key] = ''
        continue
      }
      const n = Number.parseInt(raw, 10)
      payload[f.key] = String(Number.isFinite(n) && n >= 0 ? n : 0)
    } else if (f.type === 'bool') {
      if (raw === '') {
        if (String(originals[f.key] ?? '').trim() !== '') payload[f.key] = ''
        continue
      }
      payload[f.key] = raw === 'false' ? 'false' : 'true'
    } else {
      payload[f.key] = raw
    }
  }
  return payload
}
