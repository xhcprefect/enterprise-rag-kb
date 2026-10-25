<template>
  <div class="layout">
    <button v-if="mobileNavOpen" class="nav-backdrop" type="button" :aria-label="t('layout.closeNav')" @click="closeNav"></button>
    <aside ref="navPanelRef" class="aside" :class="{ collapsed, 'mobile-open': mobileNavOpen }" :inert="isNarrow && !mobileNavOpen" :aria-hidden="isNarrow && !mobileNavOpen ? true : undefined" :role="isNarrow ? 'dialog' : undefined" :aria-modal="isNarrow && mobileNavOpen ? true : undefined" :aria-label="t('layout.mainNav')" @keydown="onNavKeydown">
      <router-link to="/chat" class="brand" :aria-label="t('layout.home')" @click="closeNav">
        <span class="brand-mark"><el-icon :size="22"><Reading /></el-icon></span>
        <span v-show="!collapsed || mobileNavOpen" class="brand-text"><strong>{{ t('common.appName') }}<span class="brand-dot">.</span></strong><span>KNOWLEDGE WORKSPACE</span></span>
      </router-link>
      <button v-if="isNarrow" class="mobile-nav-close" type="button" :aria-label="t('layout.closeNav')" @click="closeNav"><el-icon :size="18"><ArrowLeft /></el-icon></button>
      <div v-show="!collapsed || mobileNavOpen" class="nav-label">{{ t('common.workspace') }}</div>
      <nav class="nav">
        <el-tooltip v-for="item in menus" :key="item.path" :content="item.title" placement="right" :disabled="!collapsed || mobileNavOpen">
          <router-link :to="item.path" class="nav-item" :class="{ active: activeMenu === item.path }" :aria-current="activeMenu === item.path ? 'page' : undefined" @click="closeNav">
            <el-icon :size="19"><component :is="item.icon" /></el-icon><span v-show="!collapsed || mobileNavOpen" class="nav-text">{{ item.title }}</span>
            <span v-show="(!collapsed || mobileNavOpen) && activeMenu === item.path" class="nav-active-dot"></span>
          </router-link>
        </el-tooltip>
      </nav>
      <div v-show="!collapsed || mobileNavOpen" class="workspace-note">
        <div class="note-icon"><el-icon :size="19"><DocumentCopy /></el-icon></div>
        <strong>{{ t('layout.noteTitle') }}</strong><p>{{ t('layout.noteLine1') }}<br />{{ t('layout.noteLine2') }}</p>
        <span class="note-line"></span><span class="note-caption">{{ t('layout.noteCaption') }}</span>
      </div>
      <div class="aside-foot">
        <router-link to="/profile" class="side-profile" :title="displayName" @click="closeNav">
          <span class="side-avatar">{{ displayName.charAt(0).toUpperCase() }}</span>
          <span v-show="!collapsed || mobileNavOpen" class="side-profile-text"><strong>{{ displayName }}</strong><span>{{ t(userStore.isAdmin ? 'layout.adminAccount' : 'layout.memberAccount') }}</span></span>
          <el-icon v-show="!collapsed || mobileNavOpen" class="profile-arrow"><ArrowRight /></el-icon>
        </router-link>
        <button class="collapse-btn" type="button" :aria-label="t(collapsed ? 'layout.expandNav' : 'layout.collapseNav')" @click="collapsed = !collapsed">
          <el-icon :size="16"><Expand v-if="collapsed" /><Fold v-else /></el-icon><span v-show="!collapsed">{{ t('layout.collapseSidebar') }}</span>
        </button>
      </div>
    </aside>

    <div class="body" :inert="isNarrow && mobileNavOpen">
      <header class="header">
        <div class="header-left">
          <button class="mobile-nav-toggle" type="button" :aria-expanded="mobileNavOpen" :aria-label="t('layout.openNav')" @click="mobileNavOpen = !mobileNavOpen"><el-icon :size="21"><Expand /></el-icon></button>
          <div class="crumbs"><span class="crumb-root">{{ t('common.workspace') }}</span><span class="crumb-sep">/</span><span class="crumb-current">{{ currentTitle }}</span></div>
        </div>
        <div class="header-right">
          <span class="header-date">{{ today }}</span><span class="header-divider"></span>
          <AppearanceControls />
          <el-dropdown trigger="click" @command="onCommand">
            <button class="user" type="button" :aria-label="t('layout.accountMenu')">
              <el-avatar :size="32" class="avatar">{{ displayName.charAt(0).toUpperCase() }}</el-avatar>
              <span class="user-copy"><strong>{{ displayName }}</strong><span>{{ t(userStore.isAdmin ? 'common.admin' : 'common.member') }}</span></span>
              <el-icon :size="12" class="caret"><ArrowDown /></el-icon>
            </button>
            <template #dropdown><el-dropdown-menu>
              <el-dropdown-item command="profile"><el-icon><User /></el-icon>{{ t('nav.profile') }}</el-dropdown-item>
              <el-dropdown-item command="logout" divided><el-icon><SwitchButton /></el-icon>{{ t('layout.logout') }}</el-dropdown-item>
            </el-dropdown-menu></template>
          </el-dropdown>
        </div>
      </header>
      <main class="main" :class="{ 'main-chat': route.name === 'chat' }">
        <router-view v-slot="{ Component }"><transition name="page-fade" mode="out-in"><component :is="Component" :key="route.fullPath" /></transition></router-view>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessageBox } from 'element-plus'
import AppearanceControls from '@/components/AppearanceControls.vue'
import { formatDateTime } from '@/i18n/format'
import { useUserStore } from '@/stores/user'
import { useResponsiveDrawer } from '@/composables/useResponsiveDrawer'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const userStore = useUserStore()
const collapsed = ref(false)
const { isNarrow, isOpen: mobileNavOpen, panelRef: navPanelRef, close: closeNav, onKeydown: onNavKeydown } = useResponsiveDrawer()
const displayName = computed(() => userStore.profile?.display_name || userStore.profile?.username || t('common.user'))
const today = computed(() => formatDateTime(new Date(), { month: 'long', day: 'numeric', weekday: 'short' }))
const currentTitle = computed(() => t(typeof route.meta.titleKey === 'string' ? route.meta.titleKey : 'nav.chat'))
const activeMenu = computed(() => route.name === 'documents' ? '/knowledge' : route.name === 'profile' ? '' : route.path)
const menus = computed(() => {
  const items = [
    { path: '/chat', title: t('nav.chat'), icon: 'ChatDotRound' },
    { path: '/knowledge', title: t('nav.knowledge'), icon: 'Files' }
  ]
  if (userStore.isAdmin) items.push(
    { path: '/users', title: t('nav.users'), icon: 'UserFilled' },
    { path: '/audit', title: t('nav.audit'), icon: 'Tickets' }
  )
  return items
})
watch(() => route.fullPath, closeNav)
async function onCommand(command: string) {
  if (command === 'profile') { router.push('/profile'); return }
  if (command === 'logout') {
    try {
      await ElMessageBox.confirm(t('layout.logoutConfirm'), t('common.tip'), {
        type: 'warning', confirmButtonText: t('layout.logout'), cancelButtonText: t('common.cancel')
      })
    } catch { return }
    await userStore.logout()
    router.push('/login')
  }
}
</script>

<style scoped>
.layout { display: flex; height: 100vh; height: 100dvh; overflow: hidden; background: var(--bg); }
.aside { position: relative; z-index: 40; display: flex; flex: 0 0 244px; flex-direction: column; width: 244px; padding: 30px 18px 16px; overflow: hidden; background: var(--nav-bg, #18352c); color: #d7e4d9; transition: width .22s, flex-basis .22s, transform .22s; }
.aside.collapsed { flex-basis: 80px; width: 80px; padding-inline: 13px; }
.brand { display: flex; align-items: center; gap: 11px; min-height: 46px; padding: 0 7px; margin-bottom: 41px; color: inherit; text-decoration: none; white-space: nowrap; }
.brand-mark { display: grid; place-items: center; flex-shrink: 0; width: 37px; height: 40px; border: 1px solid #668b70; border-radius: 11px; background: #2b5140; color: #d8e8ba; }
.brand-text { display: flex; flex-direction: column; gap: 7px; min-width: 0; }
.brand-text strong { color: #f4f6ef; font-size: 18px; font-weight: 600; letter-spacing: .01em; }
.brand-dot { padding-left: 2px; color: #cce2a4; }
.brand-text > span { color: #94ae9d; font-size: 8px; letter-spacing: .15em; }
.nav-label { margin: 0 14px 13px; color: #91ab99; font-size: 10px; letter-spacing: .12em; }
.nav { display: flex; flex-direction: column; gap: 7px; }
.nav-item { display: flex; align-items: center; gap: 12px; min-height: 46px; padding: 0 14px; border: 1px solid transparent; border-radius: 9px; color: #b8cbbb; font-size: 13px; text-decoration: none; white-space: nowrap; transition: color .18s, background .18s, border-color .18s; }
.nav-item:hover { color: #fff; background: #244638; }
.nav-item.active { border-color: #44644b; background: #355741; color: #e3efcb; font-weight: 550; }
.nav-item .el-icon { flex-shrink: 0; }
.nav-text { flex: 1; }
.nav-active-dot { width: 5px; height: 5px; border-radius: 50%; background: #d2e8a7; }
.workspace-note { position: relative; margin: auto 5px 22px; padding: 19px 17px 17px; border: 1px solid #385543; border-radius: 12px; background: #203f31; }
.note-icon { display: flex; color: #c6dca9; margin-bottom: 14px; }
.workspace-note strong { color: #dce7d6; font-size: 12px; font-weight: 500; line-height: 1.7; }
.workspace-note p { margin: 9px 0 13px; color: #9cb49e; font-size: 11px; line-height: 1.9; }
.note-line { display: block; width: 26px; height: 2px; margin-bottom: 11px; background: #8fae72; }
.note-caption { color: #829f88; font-size: 9px; letter-spacing: .08em; line-height: 1.7; }
.aside-foot { margin-top: auto; padding-top: 13px; border-top: 1px solid #365040; }
.side-profile { display: flex; align-items: center; gap: 10px; padding: 9px 7px; border-radius: 9px; color: inherit; text-decoration: none; }
.side-profile:hover { background: #244638; }
.side-avatar { display: grid; place-items: center; width: 33px; height: 33px; flex-shrink: 0; border: 1px solid #658068; border-radius: 10px; color: #e1e8d3; background: #3c5842; font-size: 12px; font-weight: 550; }
.side-profile-text { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 5px; }
.side-profile-text strong { overflow: hidden; color: #e4ebdf; font-size: 12px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.side-profile-text > span { color: #96ad9c; font-size: 10px; }
.profile-arrow { color: #8ca38e; font-size: 12px; }
.collapse-btn { display: flex; align-items: center; gap: 12px; width: 100%; min-height: 35px; padding: 0 14px; margin-top: 5px; border: 0; border-radius: 8px; color: #97ad9c; background: transparent; font-size: 11px; cursor: pointer; white-space: nowrap; }
.collapse-btn:hover { color: #dce6d7; background: #244638; }
.body { display: flex; flex: 1; min-width: 0; flex-direction: column; }
.header { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 20px; height: 74px; padding: 0 34px; border-bottom: 1px solid var(--line); background: var(--surface); }
.header-left, .header-right { display: flex; align-items: center; gap: 20px; min-width: 0; }
.header-left { flex: 1; }
.header-right { flex-shrink: 0; }
.crumbs { display: flex; align-items: center; gap: 13px; min-width: 0; font-size: 12px; white-space: nowrap; }
.crumb-root { flex-shrink: 0; color: var(--text-3); }
.crumb-sep { color: var(--line-strong); }
.crumb-current { overflow: hidden; color: var(--text); font-weight: 550; text-overflow: ellipsis; }
.header-date { color: var(--text-3); font-size: 11px; white-space: nowrap; }
.header-divider { height: 22px; width: 1px; background: var(--line); }
.user { display: flex; align-items: center; gap: 10px; padding: 5px 2px; border: 0; border-radius: 8px; background: transparent; cursor: pointer; }
.avatar { border: 1px solid var(--accent-border); color: var(--brand-dark); background: var(--brand-soft); font-size: 12px; font-weight: 600; }
.user-copy { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; }
.user-copy strong { max-width: 110px; overflow: hidden; color: var(--text); font-size: 11px; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }
.user-copy > span { color: var(--text-3); font-size: 9px; }
.caret { margin-left: 5px; color: var(--text-3); }
.main { --main-pad-y: 66px; --app-header-height: 74px; flex: 1; min-height: 0; padding: 30px 34px 36px; overflow: auto; }
.main-chat { padding: 24px 28px; --main-pad-y: 48px; }
.mobile-nav-toggle { display: none; align-items: center; justify-content: center; width: 36px; height: 36px; flex-shrink: 0; padding: 0; border: 1px solid var(--line); border-radius: 8px; color: var(--text); background: var(--surface); cursor: pointer; }
.mobile-nav-close { position: absolute; top: 86px; right: 20px; display: grid; place-items: center; width: 28px; height: 28px; border: 1px solid #466350; border-radius: 7px; background: #244638; color: #dce7d6; cursor: pointer; }
.nav-backdrop { display: none; }
@media (max-height: 730px) { .workspace-note { display: none !important; } }
@media (max-width: 1180px) { .header { padding-inline: 25px; } .header-date, .header-divider { display: none; } .main { padding: 25px; --main-pad-y: 50px; } .main-chat { padding: 20px; --main-pad-y: 40px; } .aside { flex-basis: 220px; width: 220px; padding-inline: 13px; } }
@media (max-width: 900px) {
  .aside, .aside.collapsed { position: fixed; inset: 0 auto 0 0; width: 244px; padding: 26px 18px 16px; transform: translateX(-100%); box-shadow: 10px 0 40px rgb(15 35 22 / 12%); }
  .aside.mobile-open { transform: translateX(0); }
  .nav-backdrop { position: fixed; z-index: 35; display: block; inset: 0; width: 100%; height: 100%; border: 0; background: var(--overlay); backdrop-filter: blur(2px); }
  .collapse-btn { display: none; }
  .mobile-nav-toggle { display: flex; }
  .header { height: 64px; padding-inline: 20px; }
  .header-left { gap: 12px; }
  .main { --app-header-height: 64px; }
}
@media (max-width: 600px) {
  .header { gap: 10px; padding-inline: 16px; }
  .user-copy, .caret, .crumb-root, .crumb-sep { display: none; }
  .header-right { gap: 10px; }
  .main { padding: 22px 16px; --main-pad-y: 44px; }
  .main-chat { padding: 12px; --main-pad-y: 24px; }
}
</style>
