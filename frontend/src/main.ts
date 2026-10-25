import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import {
  ArrowDown, ArrowLeft, ArrowRight, ChatDotRound, ChatLineRound, CircleCheck,
  CircleCheckFilled, CircleClose, Delete, Document, DocumentCopy, EditPen, Expand,
  Files, Fold, Folder, InfoFilled, Loading, Lock, MagicStick, MoreFilled, Plus,
  Promotion, Reading, Refresh, Search, SwitchButton, Tickets, Unlock, Upload,
  UploadFilled, User, UserFilled, VideoPause
} from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import 'highlight.js/styles/github.css'

import App from './App.vue'
import router from './router'
import './styles/main.css'

const app = createApp(App)

const icons = {
  ArrowDown, ArrowLeft, ArrowRight, ChatDotRound, ChatLineRound, CircleCheck,
  CircleCheckFilled, CircleClose, Delete, Document, DocumentCopy, EditPen, Expand,
  Files, Fold, Folder, InfoFilled, Loading, Lock, MagicStick, MoreFilled, Plus,
  Promotion, Reading, Refresh, Search, SwitchButton, Tickets, Unlock, Upload,
  UploadFilled, User, UserFilled, VideoPause
}
for (const [key, component] of Object.entries(icons)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
