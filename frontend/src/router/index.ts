import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { createRouteAccess } from './access.ts'
import { installRouteGuards } from './guards.ts'
import { staticRoutes, workspaceRoute } from './routes.ts'

const router = createRouter({ history: createWebHistory(), routes: staticRoutes })

/** 在 Pinia 安装后、首次导航前调用。 */
export function setupRouterGuards() {
  return installRouteGuards(router, useUserStore(), createRouteAccess(router, workspaceRoute))
}

export default router
