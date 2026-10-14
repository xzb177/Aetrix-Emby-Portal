import { createApp } from 'vue'
import { createPinia } from 'pinia'

// element-plus 按需引入（unplugin-vue-components + ElementPlusResolver）：
// <el-*> 模板标签构建时自动解析，只打包用到的组件；不再全量注册。
// 注意：不要加回 `import ElementPlus from 'element-plus'` / `app.use(ElementPlus)`，
// 那会把整个组件库（1MB+）打进首屏包。

import App from './App.vue'
import router from './router'
// 站点品牌（能力：站点与品牌）：站名 / Logo / 主题色由后台自己填，启动时读一次。
// 先挂载再拉取，不让一次额外请求把首屏拖住；拉到后主题色与文档标题会自己更新。
import { initBranding } from './composables/branding'

// 【UI 统一 U0】主题 CSS 引入已冻结：新增全量引入必须经设计审查，并同步更新
// scripts/check_frontend_tokens.py 的 MAIN_CSS_ALLOWLIST，否则 CI 直接失败。
// 暗房影院令牌（与用户端同名同值）必须最先引入：tokens.css 只是它的别名层
import './styles/aurora.css'
import './styles/tokens.css'
import './styles/base.css'
import './styles/index.css'
import './styles/element-plus-theme.css'
// 窄屏适配层必须最后引入（它要覆盖上面几层与各页面 scoped 样式）
import './styles/responsive.css'

// 函数式组件的样式必须手动引：ElMessage / ElMessageBox / v-loading 不经过模板解析，
// unplugin 的按需样式注入对「直接 import 调用」不生效——漏了这段，toast 的
// position:fixed 等基础样式进不了包，弹层会掉到文档流末尾（视口外，看不到反馈）。
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/loading/style/css'
import { ElLoadingDirective } from 'element-plus'

const app = createApp(App)

// 组件里 `await ElMessageBox.confirm(...)` / `prompt(...)` 在用户点「取消」「关闭」时
// 会以 'cancel' / 'close' 拒绝——这是正常交互，不是一个错误。Element Plus 没有别的
// 回调口子，所以统一在这里把它们咽掉：不写这一段，管理员每取消一次对话框，控制台
// 就会多一条 "Unhandled error during execution of native event handler"，
// 真正的异常也就淹在里面了。
app.config.errorHandler = (err) => {
  if (err === 'cancel' || err === 'close') return
  console.error(err)
}

app.use(createPinia())
app.use(router)
// v-loading 指令（全量引入时代自带，按需引入后必须手动注册）
app.directive('loading', ElLoadingDirective)

app.mount('#app')

// index.html 里的首屏加载提示挂在 #app.loading::before 上，挂载完成后必须摘掉类名，
// 否则「加载中...」会一直盖在界面上（它是一个固定定位的伪元素，不随内容变化消失）。
document.getElementById('app')?.classList.remove('loading')

void initBranding()
