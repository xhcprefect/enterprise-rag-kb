<template>
  <div class="layout">
    <aside class="aside" :class="{ collapsed }">
      <div class="brand">
        <div class="brand-mark">
          <el-icon :size="19"><Reading /></el-icon>
        </div>
        <div v-show="!collapsed" class="brand-text">
          <strong>企业知识库</strong>
          <span>RAG 知识检索</span>
        </div>
      </div>

      <nav class="nav">
        <el-tooltip
          v-for="item in menus"
          :key="item.path"
          :content="item.title"
          placement="right"
          :disabled="!collapsed"
        >
          <router-link :to="item.path" class="nav-item" :class="{ active: activeMenu === item.path }">
            <el-icon :size="18"><component :is="item.icon" /></el-icon>
            <span v-show="!collapsed" class="nav-text">{{ item.title }}</span>
          </router-link>
        </el-tooltip>
      </nav>

      <div class="aside-foot">
        <button class="collapse-btn" type="button" @click="collapsed = !collapsed">
          <el-icon :size="17">
            <Expand v-if="collapsed" />
            <Fold v-else />
          </el-icon>
          <span v-show="!collapsed">收起菜单</span>
        </button>
      </div>
    </aside>

    <div class="body">
      <header class="header">
        <div class="crumbs">
          <span class="crumb-root">工作台</span>
          <el-icon class="crumb-sep" :size="12"><ArrowRight /></el-icon>
          <span class="crumb-current">{{ route.meta.title }}</span>
        </div>

        <div class="header-right">
          <el-tag v-if="userStore.isAdmin" size="small" effect="plain" type="primary" round>
            管理员
          </el-tag>

          <el-dropdown trigger="click" @command="onCommand">
            <div class="user">
              <el-avatar :size="32" class="avatar">
                {{ (userStore.profile?.display_name || userStore.profile?.username || '?').charAt(0) }}
              </el-avatar>
              <span class="username">
                {{ userStore.profile?.display_name || userStore.profile?.username }}
              </span>
              <el-icon :size="13" class="caret"><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">
                  <el-icon><User /></el-icon>个人中心
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <el-icon><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main class="main">
        <router-view v-slot="{ Component }">
          <transition name="page-fade" mode="out-in">
            <component :is="Component" :key="route.fullPath" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const collapsed = ref(false)

const activeMenu = computed(() => {
  if (route.name === 'documents') return '/knowledge'
  if (route.name === 'profile') return ''
  return route.path
})

const menus = computed(() => {
  const items = [
    { path: '/chat', title: '知识问答', icon: 'ChatDotRound' },
    { path: '/knowledge', title: '知识库管理', icon: 'Files' }
  ]
  if (userStore.isAdmin) {
    items.push(
      { path: '/users', title: '用户管理', icon: 'UserFilled' },
      { path: '/audit', title: '审计日志', icon: 'Tickets' }
    )
  }
  return items
})

async function onCommand(command: string) {
  if (command === 'profile') {
    router.push('/profile')
    return
  }
  if (command === 'logout') {
    try {
      await ElMessageBox.confirm('确定要退出登录吗？', '提示', {
        type: 'warning',
        confirmButtonText: '退出登录',
        cancelButtonText: '取消'
      })
    } catch {
      return
    }
    await userStore.logout()
    router.push('/login')
  }
}
</script>

<style scoped>
.layout {
  display: flex;
  height: 100vh;
  background: var(--bg);
}

/* ---------- 侧边栏 ---------- */
.aside {
  width: 236px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  padding: 18px 14px 14px;
  background: #ffffff;
  border-right: 1px solid var(--line);
  transition: width 0.22s cubic-bezier(0.4, 0, 0.2, 1);
  overflow: hidden;
}

.aside.collapsed {
  width: 76px;
  padding-left: 12px;
  padding-right: 12px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 0 6px;
  height: 44px;
  margin-bottom: 22px;
  white-space: nowrap;
}

.brand-mark {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  border-radius: 11px;
  display: grid;
  place-items: center;
  color: #fff;
  background: linear-gradient(135deg, #6b8cff 0%, #4c6ef5 55%, #3b57d6 100%);
  box-shadow: 0 4px 12px rgba(76, 110, 245, 0.3);
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
  min-width: 0;
}

.brand-text strong {
  font-size: 15px;
  font-weight: 650;
  color: var(--text);
  letter-spacing: 0.01em;
}

.brand-text span {
  font-size: 11px;
  color: var(--text-3);
  letter-spacing: 0.04em;
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 11px;
  height: 42px;
  padding: 0 12px;
  border-radius: 10px;
  color: var(--text-2);
  font-size: 14px;
  text-decoration: none;
  white-space: nowrap;
  transition: background 0.15s ease, color 0.15s ease;
}

.nav-item:hover {
  background: #f4f6fb;
  color: var(--text);
}

.nav-item.active {
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 600;
}

.nav-item.active :deep(.el-icon) {
  color: var(--brand);
}

.nav-text {
  overflow: hidden;
  text-overflow: ellipsis;
}

.aside-foot {
  padding-top: 10px;
  border-top: 1px solid var(--line);
}

.collapse-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  height: 38px;
  padding: 0 12px;
  border: none;
  border-radius: 9px;
  background: transparent;
  color: var(--text-3);
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s ease, color 0.15s ease;
}

.collapse-btn:hover {
  background: #f4f6fb;
  color: var(--text-2);
}

/* ---------- 右侧主体 ---------- */
.body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.header {
  height: 64px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 24px;
  background: rgba(255, 255, 255, 0.82);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--line);
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  min-width: 0;
}

.crumb-root {
  color: var(--text-3);
}

.crumb-sep {
  color: #c3cad6;
}

.crumb-current {
  color: var(--text);
  font-weight: 600;
  font-size: 15px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 14px;
}

.user {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 10px 5px 5px;
  border-radius: 999px;
  cursor: pointer;
  outline: none;
  transition: background 0.15s ease;
}

.user:hover {
  background: #f4f6fb;
}

.avatar {
  background: linear-gradient(135deg, #6b8cff, #4c6ef5);
  color: #fff;
  font-weight: 600;
  flex-shrink: 0;
}

.username {
  font-size: 13.5px;
  color: var(--text);
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.caret {
  color: var(--text-3);
}

.main {
  /* 供子页面（如聊天页）计算可用高度：顶栏 64 + 上下内边距 */
  --main-pad-y: 50px;
  flex: 1;
  overflow: auto;
  padding: 22px 24px 28px;
}

@media (max-width: 900px) {
  .aside {
    width: 76px;
    padding-left: 12px;
    padding-right: 12px;
  }

  .brand-text,
  .nav-text,
  .collapse-btn span {
    display: none;
  }

  .main {
    --main-pad-y: 32px;
    padding: 16px;
  }

  .username {
    display: none;
  }
}
</style>
