import { watch } from 'vue'
import type { Router, RouteLocationNormalized } from 'vue-router'
import type { User } from '../api/index.ts'
import type { RouteAccess } from './access.ts'

interface AuthSession {
  token: string
  profile: Pick<User, 'id' | 'role'> | null
  fetchProfile(): Promise<unknown>
  logout(revoke?: boolean): Promise<void>
}

/** 登录回跳只接受站内路径，排除登录页自身，避免外部地址和循环跳转。 */
export function getLoginRedirect(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return '/chat'
  if (/[\\\u0000-\u001f\u007f]/.test(value)) return '/chat'
  const path = value.split(/[?#]/)[0].replace(/\/+$/, '').toLowerCase()
  return path === '/login' ? '/chat' : value
}

function replaceLocation(to: Pick<RouteLocationNormalized, 'path' | 'query' | 'hash'>) {
  return { path: to.path, query: to.query, hash: to.hash, replace: true }
}

function loginLocation(to: RouteLocationNormalized) {
  return {
    name: 'login',
    query: to.name === 'login' ? to.query : { redirect: to.fullPath },
    replace: true
  }
}

export function installRouteGuards(router: Router, session: AuthSession, access: RouteAccess) {
  // 同步清理，确保退出、换账号或更新角色后不残留上一个用户的路由。
  const stopWatching = watch(
    [() => session.token, () => session.profile?.id, () => session.profile?.role],
    () => access.reset(),
    { flush: 'sync' }
  )

  const removeGuard = router.beforeEach(async (to) => {
    if (!session.token) {
      access.reset()
      return to.meta.public ? true : loginLocation(to)
    }
    if (to.meta.public && to.name !== 'login') return true

    const token = session.token
    if (!session.profile) {
      try {
        await session.fetchProfile()
      } catch {
        // 旧请求失败不能退出已经切换的新账号，也不再用失效令牌请求退出接口。
        if (session.token === token) await session.logout(false)
        return session.token ? replaceLocation(to) : loginLocation(to)
      }
    }
    if (session.token !== token) {
      return session.token ? replaceLocation(to) : loginLocation(to)
    }
    if (!session.profile) {
      await session.logout(false)
      return loginLocation(to)
    }

    const registered = access.register(session.profile.role)
    if (to.name === 'login') return replaceLocation(router.resolve(getLoginRedirect(to.query.redirect)))
    if (registered) {
      // addRoute 不会自动更新已匹配的兜底路由，重新匹配且完整保留查询参数和锚点。
      return replaceLocation(to)
    }
    if (to.name === 'not-found' || (to.meta.roles && !to.meta.roles.includes(session.profile.role))) {
      return { name: 'chat', replace: true }
    }
    return true
  })

  return () => {
    stopWatching()
    removeGuard()
    access.reset()
  }
}
