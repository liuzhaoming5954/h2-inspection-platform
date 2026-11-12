import { createApp } from 'vue'
import { ElButton, ElDialog, ElInput, ElMessage, ElTag } from 'element-plus'
import 'element-plus/dist/index.css'
import './style.css'
import App from './App.vue'

const app = createApp(App)
app.use(ElButton).use(ElDialog).use(ElInput).use(ElTag)
app.config.globalProperties.$message = ElMessage
app.mount('#app')
