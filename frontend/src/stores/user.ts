import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { authApi, type User } from '@/api'

export const useUserStore = defineStore('user', () => {
  const token = ref<string>(localStorage.getItem('rag_token') || '')
  const profile = ref<User | null>(null)

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => profile.value?.role === 'admin')

  async function login(username: string, password: string) {
    const { data } = await authApi.login(username, password)
    token.value = data.access_token
    localStorage.setItem('rag_token', data.access_token)
    await fetchProfile()
  }

  async function fetchProfile() {
    const { data } = await authApi.me()
    profile.value = data
    return data
  }

  async function logout(revoke = true) {
    try {
      if (revoke) await authApi.logout()
    } catch {
      // 令牌已失效时退出接口会失败，不影响本地清理
    }
    token.value = ''
    profile.value = null
    localStorage.removeItem('rag_token')
  }

  return { token, profile, isLoggedIn, isAdmin, login, fetchProfile, logout }
})
