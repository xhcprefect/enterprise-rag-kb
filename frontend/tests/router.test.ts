import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import { reactive } from 'vue'
import { createMemoryHistory, createRouter, type RouteRecordRaw } from 'vue-router'
import { createRouteAccess, filterAuthorizedRoutes, getNavigationItems } from '../src/router/access.ts'
import { getLoginRedirect, installRouteGuards } from '../src/router/guards.ts'
import { staticRoutes, workspaceRoute, type UserRole } from '../src/router/routes.ts'
import { messages } from '../src/i18n/messages/index.ts'

type Profile = { id: string; role: UserRole }
const admin: Profile = { id: 'admin-id', role: 'admin' }
const member: Profile = { id: 'member-id', role: 'user' }

/** 保留真实路径和元数据，只替换页面组件，让测试直接运行于 Node。 */
function stubComponents(routes: RouteRecordRaw[]): RouteRecordRaw[] {
  return routes.map((route) => {
    const record = { ...route }
    if ('component' in record) record.component = { render: () => null }
    if (route.children) record.children = stubComponents(route.children)
    return record
  })
}

function createHarness(t: TestContext, options: {
  token?: string
  profile?: Profile | null
  loadProfile?: () => Promise<Profile>
} = {}) {
  const router = createRouter({ history: createMemoryHistory(), routes: stubComponents(staticRoutes) })
  const access = createRouteAccess(router, stubComponents([workspaceRoute])[0])
  let fetchCount = 0
  const logoutCalls: boolean[] = []
  const session = reactive({
    token: options.token || '',
    profile: (options.profile || null) as Profile | null,
    async fetchProfile() {
      fetchCount += 1
      const token = session.token
      const profile = await (options.loadProfile?.() || Promise.resolve(member))
      if (session.token === token) session.profile = { ...profile }
      return profile
    },
    async logout(revoke = true) {
      logoutCalls.push(revoke)
      session.token = ''
      session.profile = null
    }
  })
  t.after(installRouteGuards(router, session, access))
  return { router, session, access, logoutCalls, get fetchCount() { return fetchCount } }
}

test('only login and fallback routes are registered before authentication', (t) => {
  const { router } = createHarness(t)
  assert.deepEqual(router.getRoutes().map(route => route.name).sort(), ['login', 'not-found'])
  for (const name of ['workspace', 'chat', 'knowledge', 'documents', 'usage', 'profile', 'users', 'audit']) {
    assert.equal(router.hasRoute(name), false, name)
  }
})

test('role filtering keeps the source configuration intact and menus match accessible routes', () => {
  const memberRoutes = filterAuthorizedRoutes([workspaceRoute], 'user')[0].children || []
  assert.deepEqual(memberRoutes.map(route => route.name), ['chat', 'knowledge', 'documents', 'usage', 'profile'])
  assert.equal(workspaceRoute.children?.length, 7)
  assert.deepEqual(getNavigationItems(workspaceRoute, 'user').map(item => item.path), ['/chat', '/knowledge', '/usage'])
  assert.deepEqual(getNavigationItems(workspaceRoute, 'admin').map(item => item.path), ['/chat', '/knowledge', '/usage', '/users', '/audit'])
  assert.deepEqual(getNavigationItems(workspaceRoute), [])
  assert.equal(memberRoutes.find(route => route.name === 'documents')?.meta?.activeMenu, '/knowledge')
  assert.equal(memberRoutes.find(route => route.name === 'profile')?.meta?.activeMenu, '')

  for (const locale of ['zh-CN', 'en-US'] as const) {
    for (const item of getNavigationItems(workspaceRoute, 'admin')) {
      const [namespace, key] = item.titleKey.split('.')
      assert.equal(namespace, 'nav')
      assert.ok(messages[locale].nav[key as keyof typeof messages[typeof locale]['nav']])
      assert.ok(item.icon)
    }
  }
})

test('dynamic registration is idempotent, replaces roles, and removes all business routes', (t) => {
  const { router, access } = createHarness(t)
  assert.equal(access.register('admin'), true)
  assert.equal(access.register('admin'), false)
  assert.equal(router.hasRoute('users'), true)
  assert.equal(router.hasRoute('audit'), true)
  assert.equal(router.getRoutes().length, 10)

  assert.equal(access.register('user'), true)
  assert.equal(router.hasRoute('users'), false)
  assert.equal(router.hasRoute('audit'), false)
  assert.equal(router.hasRoute('chat'), true)
  assert.equal(router.hasRoute('usage'), true)
  assert.equal(router.getRoutes().length, 8)
  access.reset()
  access.reset()
  assert.deepEqual(router.getRoutes().map(route => route.name).sort(), ['login', 'not-found'])
})

test('an unauthenticated deep link survives login with its parameters, query and hash', async (t) => {
  const { router, session } = createHarness(t)
  const destination = '/documents/kb-123?page=2&keyword=%E6%B5%8B%E8%AF%95#upload'
  await router.push(destination)
  assert.equal(router.currentRoute.value.name, 'login')
  assert.equal(router.currentRoute.value.query.redirect, destination)
  assert.equal(router.hasRoute('documents'), false)

  session.token = 'member-token'
  session.profile = { ...member }
  await router.replace(getLoginRedirect(router.currentRoute.value.query.redirect))
  assert.equal(router.currentRoute.value.name, 'documents')
  assert.equal(router.currentRoute.value.params.kbId, 'kb-123')
  assert.equal(router.currentRoute.value.fullPath, destination)
  assert.equal(router.hasRoute('users'), false)
})

test('refreshing an admin deep link loads the server role and rematches the original URL', async (t) => {
  const harness = createHarness(t, { token: 'saved-token', loadProfile: async () => admin })
  const destination = '/users?page=2#accounts'
  await harness.router.push(destination)
  assert.equal(harness.fetchCount, 1)
  assert.equal(harness.router.currentRoute.value.name, 'users')
  assert.equal(harness.router.currentRoute.value.fullPath, destination)
  assert.equal(harness.router.currentRoute.value.matched[0].name, 'workspace')
  await harness.router.push('/audit')
  assert.equal(harness.fetchCount, 1)
})

test('members cannot open admin URLs, including on the initial navigation', async (t) => {
  const { router } = createHarness(t, { token: 'member-token', loadProfile: async () => member })
  for (const path of ['/users', '/audit', '/missing-page']) {
    await router.push(path)
    assert.equal(router.currentRoute.value.name, 'chat', path)
    assert.equal(router.hasRoute('users'), false)
    assert.equal(router.hasRoute('audit'), false)
  }
})

test('members can open usage but still cannot open admin-only pages', async (t) => {
  const { router } = createHarness(t, { token: 'member-token', profile: member })
  await router.push('/usage?granularity=day')
  assert.equal(router.currentRoute.value.name, 'usage')
  assert.equal(router.currentRoute.value.query.granularity, 'day')
  assert.equal(router.hasRoute('users'), false)
  assert.equal(router.hasRoute('audit'), false)

  const adminHarness = createHarness(t, { token: 'admin-token', profile: admin })
  await adminHarness.router.push('/usage')
  assert.equal(adminHarness.router.currentRoute.value.name, 'usage')
  assert.equal(adminHarness.router.hasRoute('users'), true)
})

test('the workspace root resolves to chat after dynamic initialization', async (t) => {
  const { router } = createHarness(t, { token: 'member-token', profile: member })
  await router.push('/')
  assert.equal(router.currentRoute.value.name, 'chat')
  assert.equal(router.currentRoute.value.path, '/chat')
})

test('logout clears routes synchronously and the next account cannot inherit admin access', async (t) => {
  const { router, session } = createHarness(t, { token: 'admin-token', profile: admin })
  await router.push('/users')
  assert.equal(router.hasRoute('users'), true)
  await session.logout()
  assert.equal(router.hasRoute('workspace'), false)
  assert.equal(router.hasRoute('users'), false)
  assert.equal(router.hasRoute('audit'), false)
  await router.push('/login')

  session.token = 'member-token'
  session.profile = { ...member }
  await router.push('/users')
  assert.equal(router.currentRoute.value.name, 'chat')
  assert.equal(router.hasRoute('users'), false)
})

test('changing the authenticated role invalidates routes before the next navigation', async (t) => {
  const { router, session } = createHarness(t, { token: 'admin-token', profile: { ...admin } })
  await router.push('/users')
  session.profile!.role = 'user'
  assert.equal(router.hasRoute('users'), false)
  await router.push('/audit')
  assert.equal(router.currentRoute.value.name, 'chat')
  assert.equal(router.hasRoute('audit'), false)

  session.profile!.role = 'admin'
  await router.push('/audit')
  assert.equal(router.currentRoute.value.name, 'audit')
})

test('profile failures clear authentication without trying to revoke an invalid token', async (t) => {
  const harness = createHarness(t, {
    token: 'expired-token',
    loadProfile: async () => { throw new Error('unauthorized') }
  })
  await harness.router.push('/knowledge?view=list')
  assert.equal(harness.router.currentRoute.value.name, 'login')
  assert.equal(harness.router.currentRoute.value.query.redirect, '/knowledge?view=list')
  assert.equal(harness.session.token, '')
  assert.equal(harness.session.profile, null)
  assert.deepEqual(harness.logoutCalls, [false])
  assert.equal(harness.router.hasRoute('workspace'), false)
})

test('the public login page does not fetch a profile for unauthenticated visitors', async (t) => {
  const harness = createHarness(t)
  await harness.router.push('/login')
  assert.equal(harness.router.currentRoute.value.name, 'login')
  assert.equal(harness.fetchCount, 0)
})

test('authenticated visitors leave the login page and retain authorized login redirects', async (t) => {
  const { router } = createHarness(t, { token: 'admin-token', profile: admin })
  await router.push('/login?redirect=%2Faudit%3Fpage%3D2%23events')
  assert.equal(router.currentRoute.value.fullPath, '/audit?page=2#events')
  await router.push('/login?redirect=%2Flogin')
  assert.equal(router.currentRoute.value.name, 'chat')
})

test('login redirects accept only local paths and cannot loop through the login page', () => {
  for (const invalid of [undefined, null, [], ['/users'], '', 'https://example.com', '//example.com', '/\\example.com', '/chat\n', '/login', '/LOGIN/', '/login?redirect=/users', '/login/#top']) {
    assert.equal(getLoginRedirect(invalid), '/chat', String(invalid))
  }
  for (const valid of ['/users?page=2#accounts', '/documents/kb-123', '/knowledge', '/']) {
    assert.equal(getLoginRedirect(valid), valid)
  }
})

test('switching accounts ignores a stale profile failure and preserves the requested URL', async (t) => {
  let reject!: (error: Error) => void
  let started!: () => void
  const loading = new Promise<Profile>((_resolve, rejectPromise) => { reject = rejectPromise })
  const requested = new Promise<void>((resolve) => { started = resolve })
  const harness = createHarness(t, {
    token: 'old-token',
    loadProfile: () => { started(); return loading }
  })
  const navigation = harness.router.push('/knowledge?view=list#documents')
  await requested
  harness.session.token = 'new-member-token'
  harness.session.profile = { ...member }
  reject(new Error('old request failed'))
  await navigation
  assert.equal(harness.router.currentRoute.value.fullPath, '/knowledge?view=list#documents')
  assert.equal(harness.session.token, 'new-member-token')
  assert.deepEqual(harness.logoutCalls, [])
  assert.equal(harness.router.hasRoute('users'), false)
})

test('a profile response that finishes after logout cannot re-register protected routes', async (t) => {
  let release!: (profile: Profile) => void
  let started!: () => void
  const loading = new Promise<Profile>((resolve) => { release = resolve })
  const requested = new Promise<void>((resolve) => { started = resolve })
  const { router, session } = createHarness(t, {
    token: 'old-admin-token',
    loadProfile: () => { started(); return loading }
  })
  const navigation = router.push('/users')
  await requested
  await session.logout(false)
  release(admin)
  await navigation
  assert.equal(router.currentRoute.value.name, 'login')
  assert.equal(session.profile, null)
  assert.equal(router.hasRoute('users'), false)
  assert.equal(router.hasRoute('workspace'), false)
})
