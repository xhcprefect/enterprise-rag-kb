import axios, { type AxiosInstance, type AxiosResponse } from 'axios'
import { ElMessage } from 'element-plus'

/** 后端统一响应结构（见 backend/app/schemas/common.py）。 */
interface ApiEnvelope<T = unknown> {
  code: number
  success: boolean
  message: string
  data: T
}

const client: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  timeout: 120000
})

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('rag_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

/** 判空并解包 {code, success, message, data}，其余响应原样返回。 */
function unwrap(response: AxiosResponse) {
  const body = response.data as ApiEnvelope | undefined
  if (body && typeof body === 'object' && 'success' in body && 'data' in body) {
    response.data = body.data
  }
  return response
}

client.interceptors.response.use(
  unwrap,
  (error) => {
    const status = error.response?.status
    const body = error.response?.data as ApiEnvelope | string | undefined
    // 登录接口的 401 表示账号或密码错误，属于正常业务失败，要提示给用户
    const isLoginRequest = (error.config?.url || '').includes('/auth/login')

    if (status === 401) {
      localStorage.removeItem('rag_token')
      if (!isLoginRequest && !location.pathname.startsWith('/login')) {
        location.href = '/login'
      }
    }

    // 主动取消（AbortController）不是错误，不弹提示
    if (axios.isCancel(error)) {
      return Promise.reject(error)
    }

    const message =
      (typeof body === 'object' && body?.message) ||
      (typeof body === 'string' && body) ||
      error.message ||
      '请求失败'

    // 令牌过期的 401 会跳转到登录页，不再重复弹提示；
    // 登录失败等其它 401 需要正常提示
    if (status !== 401 || isLoginRequest) {
      ElMessage.error(message)
    }
    return Promise.reject(error)
  }
)

export default client
