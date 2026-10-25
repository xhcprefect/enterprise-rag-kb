import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useUserStore } from '@/stores/user'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, titleKey: 'nav.login' }
  },
  {
    path: '/',
    component: () => import('@/layouts/AppLayout.vue'),
    redirect: '/chat',
    children: [
      { path: 'chat', name: 'chat', component: () => import('@/views/ChatView.vue'), meta: { titleKey: 'nav.chat' } },
      { path: 'knowledge', name: 'knowledge', component: () => import('@/views/KnowledgeView.vue'), meta: { titleKey: 'nav.knowledge' } },
      { path: 'documents/:kbId', name: 'documents', component: () => import('@/views/DocumentsView.vue'), meta: { titleKey: 'nav.documents' } },
      { path: 'users', name: 'users', component: () => import('@/views/UsersView.vue'), meta: { titleKey: 'nav.users', adminOnly: true } },
      { path: 'audit', name: 'audit', component: () => import('@/views/AuditView.vue'), meta: { titleKey: 'nav.audit', adminOnly: true } },
      { path: 'profile', name: 'profile', component: () => import('@/views/ProfileView.vue'), meta: { titleKey: 'nav.profile' } }
    ]
  },
  { path: '/:pathMatch(.*)*', redirect: '/chat' }
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  const userStore = useUserStore()
  if (to.meta.public) return true
  if (!userStore.isLoggedIn) return { name: 'login', query: { redirect: to.fullPath } }
  if (!userStore.profile) {
    try { await userStore.fetchProfile() }
    catch {
      userStore.logout()
      return { name: 'login' }
    }
  }
  if (to.meta.adminOnly && !userStore.isAdmin) return { name: 'chat' }
  return true
})

export default router
