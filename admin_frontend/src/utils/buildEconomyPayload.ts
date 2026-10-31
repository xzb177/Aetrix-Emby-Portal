/**
 * 经济分组保存载荷构造：Settings.vue saveGroupCore 的纯函数抽出。
 *
 * A-066 回归锚点：int 字段空串不许落成 '0'。空串时不传该键，后端
 * 保持无行，读取侧回退各键自己的代码缺省；口径与同函数 bool 分支一致。
 * 注意残留口径：已落库的行被管理员清空时，不传键不会删行（bool 分支
 * 同款行为），如需“清空 = 回默认”须另单统一改 bool/int 两分支，本单不动。
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
): Record<string, string> {
  const payload: Record<string, string> = {}
  for (const f of fields) {
    if (!isSupported(f.key)) continue
    const raw = String(settings[f.key] ?? '').trim()
    if (f.type === 'int') {
      if (raw === '') continue
      const n = Number.parseInt(raw, 10)
      payload[f.key] = String(Number.isFinite(n) && n >= 0 ? n : 0)
    } else if (f.type === 'bool') {
      if (raw === '') continue
      payload[f.key] = raw === 'false' ? 'false' : 'true'
    } else {
      payload[f.key] = raw
    }
  }
  return payload
}
