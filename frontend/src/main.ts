import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { i18n } from './i18n'
import {
  ArrowDown, ArrowLeft, ArrowRight, ChatDotRound, ChatLineRound, CircleCheck,
  CircleCheckFilled, CircleClose, Coin, DataLine, Delete, Document, DocumentCopy, EditPen, Expand,
  Files, Fold, Folder, InfoFilled, Loading, Lock, MagicStick, MoreFilled, Plus,
  Promotion, Reading, Refresh, Search, Setting, SwitchButton, Tickets, TrendCharts, Unlock, Upload,
  UploadFilled, User, UserFilled, VideoPause
} from '@element-plus/icons-vue'
// 先加载基础变量和命令式弹层样式，再由 main.css 覆盖品牌与深浅主题变量。
import 'element-plus/theme-chalk/base.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'

import App from './App.vue'
import router, { setupRouterGuards } from './router'
import './styles/main.css'

const app = createApp(App)

const icons = {
  ArrowDown, ArrowLeft, ArrowRight, ChatDotRound, ChatLineRound, CircleCheck,
  CircleCheckFilled, CircleClose, Coin, DataLine, Delete, Document, DocumentCopy, EditPen, Expand,
  Files, Fold, Folder, InfoFilled, Loading, Lock, MagicStick, MoreFilled, Plus,
  Promotion, Reading, Refresh, Search, Setting, SwitchButton, Tickets, TrendCharts, Unlock, Upload,
  UploadFilled, User, UserFilled, VideoPause
}
for (const [key, component] of Object.entries(icons)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(i18n)
setupRouterGuards()
app.use(router)
app.mount('#app')
