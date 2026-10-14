/**
 * 管理端 HTTP 客户端
 *
 * - baseURL: /api/admin（v2.2.0 后端统一前缀）
 * - 鉴权：Authorization: Bearer <access_token>（localStorage）
 * - 401：清掉失效凭证后**原地重载**，由路由守卫用门户会话静默免登恢复；
 *   恢复不了才落回登录页（不再弹一句「凭证已过期」的报错）
 * - 响应：后端直接返回 JSON（无 code 包裹），失败时抛出 detail 信息
 * - v2.42.5 全局反馈：成功弹 ElMessage.success、失败弹 ElMessage.error（含具体原因）；
 *   只读查询不打扰（查询失败页面自己有错误态），**写操作**（post/put/patch/del/upload）
 *   才弹——某次调用不想要默认提示，传 `{ silent: true }` 自行兜底。
 */
import axios from 'axios'
import type { AxiosError, AxiosRequestConfig, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import { toAdminError } from './errorMessage'

export const TOKEN_KEY = 'admin_access_token'
export const ADMIN_KEY = 'admin_info'

/** 扩展配置：silent = 跳过全局成功/失败 toast（调用方自己兜底时用） */
export interface AdminRequestConfig extends AxiosRequestConfig {
  silent?: boolean
}

const request = axios.create({
  baseURL: '/api/admin',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

/** 请求拦截：挂后台凭证。抽成具名函数是为了让第二个实例（publicRequest）复用。 */
function attachAuth(config: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
}

request.interceptors.request.use(attachAuth)

/**
 * 把原始 AxiosError 的 response 挂回抛出的 Error 上：
 * 全局提示照旧只用 message，但需要按状态码分支的调用方（如「标记已入库」的 409 走确认流程）
 * 可以读 err.response.status，不用为此再绕一套裸 axios。
 */
function withResponse(err: Error, source: AxiosError): Error {
  ;(err as Error & { response?: AxiosError['response']; code?: string }).response = source.response
  ;(err as Error & { code?: string }).code = source.code
  return err
}

request.interceptors.response.use(
  onResponse,
  onError,
)

/**
 * 成功响应：直接交出 data（视图层拿到的是业务对象，不是 AxiosResponse）。
 *
 * 抽成具名函数是为了让 publicRequest 复用**同一套**行为，而不是再抄一遍。
 */
function onResponse(response: AxiosResponse): any {
  // 这里刻意什么都不做：查询保持安静（列表页不该每刷新弹一次「已加载」），写操作的
  // 「报喜」也交给各视图里已有的 ElMessage.success——拦截器没有业务语义，弹不出
  // 「已创建 / 已保存」的差别，只会多出一层通用文案噪音。（原先这里是个空 if 块，
  // 带着一堆注释却什么都不执行，读代码的人会以为漏实现了。）
  return response.data
}

/**
 * 失败响应：401 自愈 + 全局错误提示。同样抽出来给两个实例共用。
 *
 * Batch G：错误文案统一走 toAdminError —— 无响应（断网）/ 超时 / 422 / 500
 * 各有明确人话文案（与 user 端 toUserMessage 同口径），英文技术信息不再上屏。
 * 401 自愈逻辑、silent 标记、withResponse 挂载行为保持不变。
 */
function onError(error: AxiosError<{ detail?: string }>): Promise<never> {
  const status = error.response?.status
  const cfg = error.config as AdminRequestConfig | undefined
  const silent = cfg?.silent === true

  if (status === 401) {
    // 「会话过期」与「业务接口自己回的 401」必须分开处理：
    // 后者（如挂载表单里 RC 账号密码填错）不是登录态问题，整页重载会把用户
    // 正在填的表单整个清掉。
    // 判据：这次请求**带了**后台凭证却仍被拒 → 会话失效；**没带**凭证 → 登录页自身
    // 的 401（密码输错）或业务接口的鉴权 401，都不该触发重载。
    const sentAuth = Boolean(error.config?.headers?.Authorization)
    const isSession401 = sentAuth
    const message = toAdminError(error, '登录状态已失效')

    if (isSession401) {
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(ADMIN_KEY)
      // 避免登录页自身的 401（密码输错）循环重载
      if (!window.location.pathname.endsWith('/login')) {
        if (canAutoRecover()) {
          // 整页重载后路由守卫会拿门户会话静默接管：成功即原地恢复，失败才会去登录页
          markAutoRecover()
          window.location.reload()
        } else {
          // 刚恢复过又失效：不再循环，直接交给登录页
          window.location.href = '/admin/login'
        }
      }
      // 401 不做全局报错：这是一次可自愈的会话过期，提示交给登录页的说明文案
      return Promise.reject(new Error(message))
    }

    // 业务性 401：只提示，不动登录态、不重载
    if (!silent) ElMessage.error(message)
    return Promise.reject(withResponse(new Error(message), error))
  }

  // Batch G：全局失败提示的文案统一经 toAdminError 翻译
  const message = toAdminError(error, '请求失败，请稍后重试')
  // 全局失败提示：红色、带具体原因。silent 的调用方自己处理错误展示。
  if (!silent) ElMessage.error(message)
  return Promise.reject(withResponse(new Error(message), error))
}

/** 401 自愈节流：短时间内只自动重载一次，避免「重载 → 又 401 → 再重载」的死循环 */
const RECOVER_KEY = 'admin_401_recover_at'
const RECOVER_WINDOW = 15000

function canAutoRecover(): boolean {
  const last = Number(sessionStorage.getItem(RECOVER_KEY) || 0)
  return Date.now() - last > RECOVER_WINDOW
}

function markAutoRecover(): void {
  sessionStorage.setItem(RECOVER_KEY, String(Date.now()))
}

/**
 * 同一帧内的成功 toast 去重：拦截器兜底 + 视图层显式 ElMessage.success 会在
 * 同一次操作里先后触发（先数据返回、后视图提示），视觉上就是「闪两下」。
 * 用文案做 key：同文案 500ms 内只弹一条；视图层的具体文案永远先到，
 * 拦截器这条通用兜底只在「视图层完全没写提示」时可见。
 */
const recentToasts = new Map<string, number>()
function dedupeToast(text: string): boolean {
  const now = Date.now()
  const last = recentToasts.get(text) || 0
  recentToasts.set(text, now)
  // 顺手清理过期项，防止长会话下 Map 无限膨胀
  if (recentToasts.size > 32) {
    for (const [k, t] of recentToasts) {
      if (now - t > 5000) recentToasts.delete(k)
    }
  }
  return now - last < 500
}

/** 所有 API 返回 any（由调用方按类型断言），保持调用层简洁 */
export function get<T = any>(url: string, params?: Record<string, unknown>): Promise<T> {
  return request.get(url, { params, silent: true } as AdminRequestConfig) as Promise<T>
}

/**
 * 写操作（POST/PUT/PATCH/DELETE）：默认在失败时弹红色错误（成功提示由视图层
 * 按「保存了什么 / 创建了什么」弹具体文案——拦截器不知道业务语义，不抢戏）。
 * `silent: true` 连失败提示也跳过，调用方自行兜底。
 */
export function post<T = any>(url: string, data?: unknown, options?: { silent?: boolean }): Promise<T> {
  return request.post(url, data, { silent: options?.silent } as AdminRequestConfig) as Promise<T>
}

/** multipart 直传；不要手动设置 boundary，交给浏览器 / Axios 生成。 */
export function upload<T = any>(url: string, data: FormData, options?: { silent?: boolean }): Promise<T> {
  return request.post(url, data, { silent: options?.silent, headers: { 'Content-Type': 'multipart/form-data' } } as AdminRequestConfig) as Promise<T>
}

export function getBlob(url: string): Promise<Blob> {
  return request.get(url, { responseType: 'blob', silent: true } as AdminRequestConfig) as Promise<Blob>
}

/** POST 一份 JSON 拿回二进制（封面预览就是这种：改一个字渲染一次图）。 */
export function postBlob(url: string, data?: unknown): Promise<Blob> {
  return request.post(url, data, { responseType: 'blob', silent: true } as AdminRequestConfig) as Promise<Blob>
}

export function put<T = any>(
  url: string, data?: unknown, params?: Record<string, unknown>
): Promise<T> {
  return request.put(url, data, { params } as AdminRequestConfig) as Promise<T>
}

export function patch<T = any>(url: string, data?: unknown, options?: { silent?: boolean }): Promise<T> {
  return request.patch(url, data, { silent: options?.silent } as AdminRequestConfig) as Promise<T>
}

export function del<T = any>(url: string, params?: Record<string, unknown>): Promise<T> {
  return request.delete(url, { params } as AdminRequestConfig) as Promise<T>
}

/**
 * 同源**绝对路径**的 GET（不带 ``/api/admin`` 前缀）。
 *
 * ## 为什么要单独一个实例
 *
 * axios 的 ``baseURL`` 是**字符串拼接**，不是 URL 解析：带 baseURL 的实例上写
 * ``get('/api/health')`` 实际请求的是 ``/api/admin/api/health`` → 404。
 * （这个坑真实发生过两次：``fetchPanelHealth`` 先写成 ``'/../health'``，靠浏览器
 * 归一化“能用”；后来改成“直白的绝对路径” ``'/api/health'``，反而 404。）
 *
 * 少数端点确实不在后台前缀下（``/api/health``），它们的正确写法就是这里：
 * 一个**不设 baseURL** 的实例，url 原样发出。拦截器复用上面那三个具名函数，
 * 所以鉴权头、401 自愈与错误提示与其它后台请求完全一致。
 */
const publicRequest = axios.create({
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})
publicRequest.interceptors.request.use(attachAuth)
publicRequest.interceptors.response.use(onResponse, onError)

/**
 * 打同源绝对路径（必须以 ``/`` 开头）。详见上方说明。
 *
 * 刻意**不传** ``silent``：走的是同一个 ``onError``，面板健康挂了照样弹全局错误
 * 提示。静默失败只会让「页面数据永远空着」这种问题更难被发现。
 */
export function getPublic<T = any>(url: string, params?: Record<string, unknown>): Promise<T> {
  return publicRequest.get(url, { params } as AdminRequestConfig) as Promise<T>
}

export { dedupeToast }
