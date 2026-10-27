import type { RouteRecordRaw } from 'vue-router'
import type { User } from '../api/index.ts'

export type UserRole = User['role']

declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
    titleKey?: string
    roles?: UserRole[]
    menu?: { icon: string }
    activeMenu?: string
  }
}

/** 未登录时只注册基础路由；兜底页交给守卫处理，避免提前重定向丢失深链接。 */
export const staticRoutes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, titleKey: 'nav.login' }
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: { render: () => null }
  }
]

/** 业务路由和侧栏菜单共用此配置，角色以 /auth/me 返回值为准。 */
export const workspaceRoute: RouteRecordRaw = {
  path: '/',
  name: 'workspace',
  component: () => import('@/layouts/AppLayout.vue'),
  redirect: { name: 'chat' },
  meta: { roles: ['admin', 'user'] },
  children: [
    {
      path: '/chat',
      name: 'chat',
      component: () => import('@/views/ChatView.vue'),
      meta: { titleKey: 'nav.chat', menu: { icon: 'ChatDotRound' } }
    },
    {
      path: '/knowledge',
      name: 'knowledge',
      component: () => import('@/views/KnowledgeView.vue'),
      meta: { titleKey: 'nav.knowledge', menu: { icon: 'Files' } }
    },
    {
      path: '/documents/:kbId',
      name: 'documents',
      component: () => import('@/views/DocumentsView.vue'),
      meta: { titleKey: 'nav.documents', activeMenu: '/knowledge' }
    },
    {
      path: '/usage',
      name: 'usage',
      component: () => import('@/views/UsageView.vue'),
      meta: { titleKey: 'nav.usage', menu: { icon: 'TrendCharts' } }
    },
    {
      path: '/users',
      name: 'users',
      component: () => import('@/views/UsersView.vue'),
      meta: { titleKey: 'nav.users', roles: ['admin'], menu: { icon: 'UserFilled' } }
    },
    {
      path: '/audit',
      name: 'audit',
      component: () => import('@/views/AuditView.vue'),
      meta: { titleKey: 'nav.audit', roles: ['admin'], menu: { icon: 'Tickets' } }
    },
    {
      path: '/profile',
      name: 'profile',
      component: () => import('@/views/ProfileView.vue'),
      meta: { titleKey: 'nav.profile', activeMenu: '' }
    }
  ]
}
