import type { Router, RouteRecordRaw } from 'vue-router'
import type { UserRole } from './routes.ts'

/** 复制后过滤，保留原配置供下次登录或切换角色使用。 */
export function filterAuthorizedRoutes(routes: RouteRecordRaw[], role: UserRole): RouteRecordRaw[] {
  return routes.flatMap((route) => {
    if (route.meta?.roles && !route.meta.roles.includes(role)) return []
    const record = { ...route }
    if (route.children) record.children = filterAuthorizedRoutes(route.children, role)
    return [record]
  })
}

export function getNavigationItems(workspace: RouteRecordRaw, role?: UserRole) {
  if (!role) return []
  const authorized = filterAuthorizedRoutes([workspace], role)[0]
  return (authorized?.children || []).flatMap((route) => {
    const { titleKey, menu } = route.meta || {}
    return titleKey && menu ? [{ path: route.path, titleKey, icon: menu.icon }] : []
  })
}

export function createRouteAccess(router: Router, workspace: RouteRecordRaw) {
  let registeredRole: UserRole | null = null
  let removeRoutes: Array<() => void> = []

  function reset() {
    for (const remove of removeRoutes) remove()
    removeRoutes = []
    registeredRole = null
  }

  /** 返回是否重新注册，供守卫决定是否重新匹配当前 URL。 */
  function register(role: UserRole) {
    if (registeredRole === role) return false
    reset()
    removeRoutes = filterAuthorizedRoutes([workspace], role).map(route => router.addRoute(route))
    registeredRole = role
    return true
  }

  return { register, reset }
}

export type RouteAccess = ReturnType<typeof createRouteAccess>
