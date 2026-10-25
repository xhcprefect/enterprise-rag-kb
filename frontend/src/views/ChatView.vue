<template>
  <div class="chat-page">
    <!-- 左侧：知识库 + 历史会话 -->
    <aside class="sidebar">
      <div class="kb-picker">
        <label>对话模式</label>
        <el-radio-group
          v-model="chatMode"
          size="small"
          class="mode-switch"
          :disabled="streaming"
          @change="onModeChange"
        >
          <el-radio-button value="rag">知识库问答</el-radio-button>
          <el-radio-button value="chat">普通对话</el-radio-button>
        </el-radio-group>

        <label>当前知识库</label>
        <el-select
          v-model="kbId"
          placeholder="选择知识库"
          class="kb-select"
          :disabled="chatMode === 'chat' || streaming"
          @change="onKbChange"
        >
          <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id">
            <span>{{ kb.name }}</span>
            <span class="kb-count">{{ kb.document_count }} 篇</span>
          </el-option>
        </el-select>
        <el-button
          class="new-chat"
          :disabled="chatMode === 'rag' && !kbId"
          @click="newConversation"
        >
          <el-icon><Plus /></el-icon>新建对话
        </el-button>
      </div>

      <div class="conv-list">
        <div class="conv-title">
          <span>历史会话</span>
          <span class="conv-num">{{ conversations.length }}</span>
        </div>

        <div
          v-for="conv in conversations"
          :key="conv.id"
          class="conv-item"
          :class="{ active: conv.id === currentConversationId }"
          @click="loadConversation(conv.id)"
        >
          <el-icon class="conv-icon" :size="15"><ChatLineRound /></el-icon>
          <span class="conv-name">{{ conv.title }}</span>
          <el-icon class="conv-del" :size="14" @click.stop="removeConversation(conv.id)">
            <Delete />
          </el-icon>
        </div>

        <div v-if="!conversations.length" class="conv-empty">
          <el-icon :size="22"><ChatLineRound /></el-icon>
          <span>暂无历史会话</span>
        </div>
      </div>
    </aside>

    <!-- 右侧：对话区 -->
    <section class="chat-main">
      <div v-if="chatMode === 'rag' && !kbId" class="placeholder">
        <el-empty
          :description="
            kbs.length
              ? '请先在左侧选择一个知识库，或切换到「普通对话」'
              : '还没有知识库，请先去「知识库管理」创建；也可以直接切换到「普通对话」'
          "
        />
      </div>

      <template v-else>
        <header class="chat-head">
          <div class="chat-title">
            <span class="dot-live"></span>
            {{ currentTitle }}
          </div>
          <div class="chat-meta">
            <span>{{ chatMode === 'chat' ? '普通对话' : kbs.find((k) => k.id === kbId)?.name }}</span>
            <span class="sep">·</span>
            <span>{{ messages.length }} 条消息</span>
          </div>
        </header>

        <div ref="scrollRef" class="messages" @scroll.passive="onScroll">
          <div v-if="!messages.length && !streaming" class="welcome">
            <div class="welcome-mark">
              <el-icon :size="30"><MagicStick /></el-icon>
            </div>
            <h2>开始提问吧</h2>
            <p v-if="chatMode === 'rag'">回答将基于所选知识库的内容生成，并附上引用来源</p>
            <p v-else>直接与模型对话，不检索知识库，回答不带引用来源</p>
            <div class="suggestions">
              <button
                v-for="s in activeSuggestions"
                :key="s"
                class="suggestion"
                type="button"
                @click="askSuggestion(s)"
              >
                {{ s }}
              </button>
            </div>
          </div>

          <div v-for="msg in messages" :key="msg.id" class="message" :class="msg.role">
            <el-avatar :size="34" :class="msg.role === 'user' ? 'user-avatar' : 'ai-avatar'">
              <el-icon v-if="msg.role === 'user'"><User /></el-icon>
              <el-icon v-else><MagicStick /></el-icon>
            </el-avatar>

            <div class="bubble-wrap">
              <div class="bubble">
                <!-- 首 token 可能要等十几秒，先给出明确状态，避免"空气泡"像卡死 -->
                <div v-if="msg.role === 'assistant' && msg.streaming && !msg.content" class="thinking">
                  <span class="dot"></span><span class="dot"></span><span class="dot"></span>
                  <span class="thinking-text">
                    {{ chatMode === 'rag' ? '正在检索并生成回答…' : '正在生成回答…' }}
                  </span>
                </div>
                <div
                  v-else-if="msg.role === 'assistant'"
                  class="markdown-body"
                  v-html="renderMarkdown(msg.content, !!msg.streaming)"
                ></div>
                <div v-else class="plain-text">{{ msg.content }}</div>

                <span v-if="msg.streaming && msg.content" class="cursor">▊</span>
              </div>

              <div v-if="msg.citations?.length" class="citations">
                <el-collapse>
                  <el-collapse-item :title="`引用来源（${msg.citations.length}）`">
                    <div v-for="(c, i) in msg.citations" :key="i" class="citation-item">
                      <div class="citation-head">
                        <span class="citation-index">{{ i + 1 }}</span>
                        <span class="citation-name">{{ c.filename }}</span>
                        <span v-if="c.chunk_index !== null" class="citation-meta">
                          第 {{ c.chunk_index! + 1 }} 段
                        </span>
                        <span v-if="c.score !== null" class="citation-score">
                          相似度 {{ (c.score! * 100).toFixed(0) }}%
                        </span>
                      </div>
                      <div class="citation-snippet">{{ c.snippet }}</div>
                    </div>
                  </el-collapse-item>
                </el-collapse>
              </div>
            </div>
          </div>

          <div v-if="errorMessage" class="error-tip">
            <el-alert :title="errorMessage" type="error" :closable="false" show-icon />
          </div>
        </div>

        <div class="composer">
          <div class="composer-box">
            <el-input
              v-model="question"
              type="textarea"
              :rows="2"
              resize="none"
              placeholder="请输入问题，Enter 发送，Shift + Enter 换行"
              :disabled="streaming"
              @keydown.enter.exact.prevent="send"
            />
            <div class="composer-actions">
              <div v-if="chatMode === 'rag'" class="topk">
                <span class="topk-label">检索条数</span>
                <el-slider v-model="topK" :min="1" :max="12" size="small" class="topk-slider" />
                <span class="topk-value">{{ topK }}</span>
              </div>

              <el-button
                v-if="streaming"
                class="composer-btn"
                type="danger"
                plain
                @click="stopStreaming"
              >
                <el-icon><VideoPause /></el-icon>停止
              </el-button>
              <el-button
                v-else
                class="composer-btn"
                type="primary"
                :disabled="!question.trim()"
                @click="send"
              >
                <el-icon><Promotion /></el-icon>发送
              </el-button>
            </div>
          </div>
        </div>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/common'
import {
  chatApi,
  kbApi,
  streamChat,
  type ChatMode,
  type Citation,
  type Conversation,
  type KnowledgeBase
} from '@/api'

interface ChatMessage {
  /** 稳定的 v-for key：用下标会在消息增删时复用错 DOM */
  id: string
  role: 'user' | 'assistant'
  content: string
  citations?: Citation[]
  streaming?: boolean
}

// crypto.randomUUID 只在安全上下文可用；线上走 http，这里退回到序号
let msgSeq = 0
const newMessageId = () =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `m-${Date.now()}-${msgSeq++}`

const escapeHtml = (text: string) =>
  text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

const highlightCode = (code: string, lang: string): string => {
  if (lang && hljs.getLanguage(lang)) {
    try {
      return `<pre><code class="hljs">${hljs.highlight(code, { language: lang }).value}</code></pre>`
    } catch {
      /* fall through */
    }
  }
  return `<pre><code class="hljs">${escapeHtml(code)}</code></pre>`
}

const md: MarkdownIt = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: true,
  highlight: highlightCode
})

// 流式过程中不跑语法高亮：每个 token 都重算代码块高亮是"一顿一顿"的成因之一。
// 收口后用带高亮的实例整段渲染一次即可。
const mdPlain: MarkdownIt = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: true,
  highlight: (code: string) => `<pre><code class="hljs">${escapeHtml(code)}</code></pre>`
})

/**
 * 按内容缓存的渲染结果。
 *
 * 模板里的 ``renderMarkdown(msg.content)`` 是裸函数调用，Vue 每次重渲染都会对**每条**
 * 消息重跑一遍；流式时每帧都在变，历史消息却一个字符都没动。缓存后只有正在增长的
 * 那一条会真正解析，其余全部命中。
 */
const renderCache = new Map<string, string>()

function renderMarkdown(text: string, streaming = false) {
  // 流式中的内容每帧都在变，缓存不可能命中，只会把 Map 撑大，所以不进缓存
  if (streaming) return mdPlain.render(text || '')

  const cached = renderCache.get(text)
  if (cached !== undefined) return cached

  const html = md.render(text || '')
  // 只有收口的消息才进缓存，正常会话远远到不了这个量级；真超了整体清空重算一次
  if (renderCache.size > 500) renderCache.clear()
  renderCache.set(text, html)
  return html
}

const route = useRoute()
const kbs = ref<KnowledgeBase[]>([])
const kbId = ref<string>('')
const chatMode = ref<ChatMode>('rag')
const conversations = ref<Conversation[]>([])
const currentConversationId = ref<string | null>(null)
const messages = ref<ChatMessage[]>([])
const question = ref('')
const topK = ref(6)
const streaming = ref(false)
const errorMessage = ref('')
const scrollRef = ref<HTMLElement>()
let controller: AbortController | null = null
let disposed = false

const suggestions = ['这个知识库包含哪些内容？', '请总结主要信息', '有哪些关键流程？']
const plainSuggestions = ['介绍一下你自己', '帮我写一封请假邮件', '解释一下什么是向量数据库']

const activeSuggestions = computed(() =>
  chatMode.value === 'chat' ? plainSuggestions : suggestions
)

const currentTitle = computed(() => {
  if (!messages.value.length) return '新对话'
  const first = messages.value.find((m) => m.role === 'user')
  return first?.content.slice(0, 24) || '新对话'
})

/** 距底部多少像素内算"贴底"，此时新内容才自动跟随 */
const STICK_THRESHOLD = 80
let stick = true

function onScroll() {
  const el = scrollRef.value
  if (!el) return
  stick = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD
}

/**
 * 贴底时才跟随。直接赋值 scrollTop，不 await nextTick、不读后立即写，
 * 避免每个 token 都触发一次强制同步布局；用户往回翻时也不会被拽回底部。
 */
function followTail() {
  const el = scrollRef.value
  if (el && stick) el.scrollTop = el.scrollHeight
}

/** 需要等 DOM 就绪的场景（加载会话、发送首条）用它，并重新贴底 */
async function scrollToBottom() {
  stick = true
  await nextTick()
  const el = scrollRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function loadKbs() {
  const { data } = await kbApi.list()
  kbs.value = data
  // 支持从知识库卡片「开始问答」带 ?kb=<id> 跳转过来；找不到则退回第一个
  const wanted = typeof route.query.kb === 'string' ? route.query.kb : ''
  const hit = data.some((k) => k.id === wanted)
  if (hit) {
    kbId.value = wanted
  } else if (data.length) {
    kbId.value = data[0].id
  }
  if (hit) chatMode.value = 'rag'
  // 一个知识库都没有时，普通对话是唯一可用的模式
  else if (!data.length) chatMode.value = 'chat'
  appliedMode = chatMode.value

  await onKbChange()
}

async function onKbChange() {
  if (streaming.value) return
  currentConversationId.value = null
  messages.value = []
  await loadConversations()
}

/**
 * 已生效的模式。``el-radio-group`` 点击**已选中**的按钮同样会触发 change，
 * 不比对就会把正在看的会话清空，所以这里记一份。
 */
let appliedMode: ChatMode = 'rag'

async function onModeChange(value: string | number | boolean) {
  const next = value as ChatMode
  // 流式中途禁止切换（单选框也置灰了，这里是兜底）；重复点同一个不做事
  if (streaming.value || next === appliedMode) return
  appliedMode = next
  currentConversationId.value = null
  messages.value = []
  errorMessage.value = ''
  await loadConversations()
}

async function loadConversations() {
  if (chatMode.value === 'chat') {
    // 普通对话与知识库无关，不带 kb_id
    const { data } = await chatApi.conversations({ mode: 'chat' })
    conversations.value = data
    return
  }
  if (!kbId.value) {
    conversations.value = []
    return
  }
  const { data } = await chatApi.conversations({ kbId: kbId.value, mode: 'rag' })
  conversations.value = data
}

async function loadConversation(id: string) {
  if (streaming.value) return
  const { data } = await chatApi.conversation(id)
  currentConversationId.value = data.id
  // 模式与知识库跟着会话走：即便列表是别的筛选条件下拉出来的，也能正确打开
  let switched = false
  if (data.mode && data.mode !== chatMode.value) {
    chatMode.value = data.mode
    appliedMode = data.mode
    switched = true
  }
  if (data.kb_id && data.kb_id !== kbId.value) {
    kbId.value = data.kb_id
    switched = true
  }
  messages.value = data.messages.map((m) => ({
    id: newMessageId(),
    role: m.role,
    content: m.content,
    citations: m.citations || undefined
  }))
  await scrollToBottom()
  // 模式或知识库变了，侧栏列表也要跟着换；
  // 不能走 onModeChange，那会把刚打开的消息清掉
  if (switched) await loadConversations()
}

function newConversation() {
  if (streaming.value) return
  currentConversationId.value = null
  messages.value = []
  question.value = ''
  errorMessage.value = ''
}

async function removeConversation(id: string) {
  try {
    await ElMessageBox.confirm('确定删除该会话吗？', '提示', { type: 'warning' })
  } catch {
    return
  }
  await chatApi.removeConversation(id)
  if (currentConversationId.value === id) newConversation()
  await loadConversations()
  ElMessage.success('已删除')
}

function stopStreaming() {
  controller?.abort()
  controller = null
  streaming.value = false
  const last = messages.value[messages.value.length - 1]
  if (last?.streaming) last.streaming = false
}

function askSuggestion(text: string) {
  question.value = text
  send()
}

async function send() {
  const text = question.value.trim()
  // 普通对话不需要知识库，只有 RAG 模式才要求先选库
  if (!text || streaming.value || (chatMode.value === 'rag' && !kbId.value)) return

  const mode = chatMode.value
  errorMessage.value = ''
  question.value = ''
  messages.value.push({ id: newMessageId(), role: 'user', content: text })

  messages.value.push({ id: newMessageId(), role: 'assistant', content: '', streaming: true })
  // 必须经由数组代理拿到响应式对象：push 进去的普通对象再改属性不会触发视图更新
  const assistant = messages.value[messages.value.length - 1]
  streaming.value = true
  const myController = new AbortController()
  controller = myController
  // 持有信号引用：stopStreaming 会把 controller 置空，但信号上的 aborted 仍然有效
  const signal = myController.signal
  // meta 里的引用先攒着，等回答生成完再挂到气泡上（避免边吐字边露出引用块）
  let pendingCitations: Citation[] = []
  await scrollToBottom()

  // ---- 流式渲染节流 ----
  // 上游可能几毫秒吐一个字符，逐个写进响应式数据会让 Vue 每个字符重渲染一次、
  // 并各触发一次强制布局。这里攒起来，每帧（约 16ms）提交一次。
  let pending = ''
  let rafId = 0
  const flush = () => {
    rafId = 0
    if (!pending) return
    assistant.content += pending
    pending = ''
    // 交给 nextTick 之后再滚动：此时 DOM 已按新内容排版，
    // 读 scrollHeight 不会触发额外的强制同步布局
    nextTick(followTail)
  }

  try {
    await streamChat(
      {
        // 普通对话不传 kb_id；undefined 在 JSON.stringify 时会被整条丢掉
        kb_id: mode === 'rag' ? kbId.value : undefined,
        question: text,
        conversation_id: currentConversationId.value,
        top_k: mode === 'rag' ? topK.value : undefined,
        mode
      },
      {
        onMeta(data) {
          if (signal.aborted || disposed || controller !== myController) return
          currentConversationId.value = data.conversation_id
          pendingCitations = data.citations
        },
        onDelta(token) {
          if (signal.aborted || disposed || controller !== myController) return
          pending += token
          if (!rafId) rafId = requestAnimationFrame(flush)
        },
        onDone() {
          assistant.streaming = false
        },
        onError(message) {
          assistant.streaming = false
          // 已被新请求接管时不要把旧请求的错误顶上去
          if (controller === myController) errorMessage.value = message
        }
      },
      signal
    )
  } catch (err: any) {
    assistant.streaming = false
    if (controller === myController && !disposed && err?.name !== 'AbortError') {
      errorMessage.value = err?.message || '请求失败'
    }
  } finally {
    // 收尾必须把缓冲冲干净，否则最后几个字符会丢
    if (rafId) {
      cancelAnimationFrame(rafId)
      rafId = 0
    }
    flush()

    const aborted = signal.aborted
    assistant.streaming = false
    // 只收拾自己的状态：用户点停止后可能在这次 finally 之前又发了新请求，
    // 那时 streaming / controller 已经属于新请求，不能被这条旧流重置。
    const isCurrent = controller === myController
    if (isCurrent) {
      streaming.value = false
      controller = null
    }
    // 一个 token 都没收到时也要有明确交代，不能留空气泡
    if (!assistant.content) {
      assistant.content = aborted
        ? '（已停止生成）'
        : mode === 'rag'
          ? '抱歉，模型未返回内容，请重试或调小「检索条数」。'
          : '抱歉，模型未返回内容，请重试。'
    }
    // 回答已收口，此时再挂引用来源，视觉上"先答案后出处"
    assistant.citations = pendingCitations
    if (!disposed && isCurrent) await loadConversations()
    // 用 followTail 而不是 scrollToBottom：用户中途往回翻时不该在收尾瞬间被拽回去。
    // 但新会话的第一条回答必须让它可见——那时用户本来就在底部，stick 为 true，
    // 直接调用即可。
    await nextTick()
    followTail()
  }
}

onMounted(loadKbs)
onBeforeUnmount(() => {
  disposed = true
  controller?.abort()
  controller = null
})
</script>

<style scoped>
/* 占满右侧主区：100vh - 顶栏 64 - main 上下内边距（由布局下发，随断点变化） */
.chat-page {
  display: flex;
  height: calc(100vh - 64px - var(--main-pad-y, 50px));
  min-height: 460px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

/* ---------- 侧栏 ---------- */
.sidebar {
  width: 262px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: #fbfcfe;
  border-right: 1px solid var(--line);
}

.kb-picker {
  padding: 18px 16px 14px;
  border-bottom: 1px solid var(--line);
}

.kb-picker label {
  display: block;
  margin-bottom: 8px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text-3);
  text-transform: uppercase;
}

.mode-switch {
  width: 100%;
  display: flex;
  margin-bottom: 14px;
}

.mode-switch :deep(.el-radio-button) {
  flex: 1;
}

.mode-switch :deep(.el-radio-button__inner) {
  width: 100%;
  padding-left: 0;
  padding-right: 0;
}

.kb-select {
  width: 100%;
}

.kb-count {
  float: right;
  color: var(--text-3);
  font-size: 12px;
}

.new-chat {
  width: 100%;
  margin-top: 10px;
}

.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: 14px 10px;
}

.conv-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 6px 8px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text-3);
  text-transform: uppercase;
}

.conv-num {
  font-weight: 500;
  letter-spacing: 0;
}

.conv-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 10px;
  border-radius: 9px;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
  margin-bottom: 2px;
  transition: background 0.14s ease, color 0.14s ease;
}

.conv-item:hover {
  background: #f1f4fa;
}

.conv-item.active {
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 500;
}

.conv-icon {
  flex-shrink: 0;
  color: var(--text-3);
}

.conv-item.active .conv-icon {
  color: var(--brand);
}

.conv-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conv-del {
  opacity: 0;
  flex-shrink: 0;
  color: var(--text-3);
  transition: opacity 0.15s ease, color 0.15s ease;
}

.conv-item:hover .conv-del {
  opacity: 1;
}

.conv-del:hover {
  color: var(--danger);
}

.conv-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 40px 0;
  font-size: 12.5px;
  color: #b3bccd;
}

/* ---------- 主区 ---------- */
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #fff;
}

.placeholder {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.chat-head {
  height: 54px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 24px;
  border-bottom: 1px solid var(--line);
}

.chat-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dot-live {
  width: 7px;
  height: 7px;
  flex-shrink: 0;
  border-radius: 50%;
  background: var(--success);
  box-shadow: 0 0 0 3px rgba(18, 183, 106, 0.15);
}

.chat-meta {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-3);
}

.chat-meta .sep {
  margin: 0 6px;
  color: #d3daea;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 26px 28px 30px;
}

/* 欢迎页 */
.welcome {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
}

.welcome-mark {
  width: 66px;
  height: 66px;
  border-radius: 20px;
  display: grid;
  place-items: center;
  color: var(--brand);
  background: linear-gradient(135deg, #eef1ff 0%, #e2e8ff 100%);
  box-shadow: 0 10px 26px rgba(76, 110, 245, 0.16);
}

.welcome h2 {
  margin: 20px 0 8px;
  font-size: 21px;
  font-weight: 650;
  color: var(--text);
  letter-spacing: -0.01em;
}

.welcome p {
  margin: 0;
  font-size: 13.5px;
  color: var(--text-3);
}

.suggestions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 10px;
  margin-top: 26px;
  max-width: 560px;
}

.suggestion {
  padding: 9px 16px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
  color: var(--text-2);
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s ease;
}

.suggestion:hover {
  border-color: var(--brand);
  color: var(--brand);
  background: var(--brand-soft);
  box-shadow: 0 4px 12px rgba(76, 110, 245, 0.12);
}

/* 消息 */
.message {
  display: flex;
  gap: 12px;
  margin-bottom: 24px;
}

.message.user {
  flex-direction: row-reverse;
}

.user-avatar {
  background: linear-gradient(135deg, #6b8cff, #4c6ef5);
  flex-shrink: 0;
}

.ai-avatar {
  background: linear-gradient(135deg, #34d399, #12b76a);
  flex-shrink: 0;
}

.bubble-wrap {
  max-width: 78%;
  min-width: 0;
}

.message.user .bubble-wrap {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

.bubble {
  padding: 12px 16px;
  border-radius: 4px 14px 14px 14px;
  background: #f6f8fc;
  border: 1px solid #eef1f7;
  font-size: 14px;
  line-height: 1.72;
  color: var(--text);
}

.message.user .bubble {
  border-radius: 14px 4px 14px 14px;
  background: linear-gradient(135deg, #5b7cff 0%, #4c6ef5 100%);
  border-color: transparent;
  color: #fff;
  box-shadow: 0 4px 14px rgba(76, 110, 245, 0.24);
}

.plain-text {
  white-space: pre-wrap;
}

.cursor {
  animation: blink 1s step-end infinite;
  color: var(--brand);
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}

.thinking {
  display: flex;
  align-items: center;
  gap: 4px;
  min-height: 22px;
  color: var(--text-3);
  font-size: 13px;
}

.thinking-text {
  margin-left: 6px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #b3bccd;
  animation: bounce 1.2s infinite ease-in-out;
}

.dot:nth-child(2) {
  animation-delay: 0.15s;
}

.dot:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes bounce {
  0%,
  60%,
  100% {
    transform: translateY(0);
    opacity: 0.5;
  }
  30% {
    transform: translateY(-4px);
    opacity: 1;
  }
}

/* 引用 */
.citations {
  margin-top: 10px;
  padding: 2px 14px;
  background: #fbfcfe;
  border: 1px solid var(--line);
  border-radius: 10px;
}

.citations :deep(.el-collapse) {
  border: none;
  background: transparent;
  --el-collapse-header-height: 38px;
  --el-collapse-border-color: transparent;
}

.citations :deep(.el-collapse-item__header) {
  padding: 0 2px;
  background: transparent;
  border-bottom: none;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-2);
}

.citations :deep(.el-collapse-item__wrap) {
  background: transparent;
  border-bottom: none;
}

.citations :deep(.el-collapse-item__content) {
  padding-bottom: 12px;
}

.citation-item {
  padding: 10px 12px;
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 9px;
  margin-bottom: 8px;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.citation-item:hover {
  border-color: #d5ddf2;
  box-shadow: var(--shadow-sm);
}

.citation-item:last-child {
  margin-bottom: 0;
}

.citation-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.citation-index {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  border-radius: 5px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 11px;
  font-weight: 700;
}

.citation-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.citation-meta,
.citation-score {
  font-size: 11.5px;
  color: var(--text-3);
  flex-shrink: 0;
}

.citation-score {
  margin-left: auto;
  padding: 1px 7px;
  border-radius: 999px;
  background: #f2f5fb;
}

.citation-snippet {
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.65;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.error-tip {
  margin-bottom: 14px;
}

/* 输入区 */
.composer {
  flex-shrink: 0;
  padding: 12px 24px 18px;
  border-top: 1px solid var(--line);
  background: linear-gradient(180deg, #ffffff 0%, #fbfcfe 100%);
}

.composer-box {
  padding: 10px 12px 8px;
  border: 1px solid var(--line-strong);
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 2px 10px rgba(27, 36, 55, 0.04);
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}

.composer-box:focus-within {
  border-color: var(--brand);
  box-shadow: 0 0 0 3px rgba(76, 110, 245, 0.12);
}

.composer-box :deep(.el-textarea__inner) {
  box-shadow: none !important;
  padding: 4px 4px 6px;
  background: transparent;
  font-size: 14px;
  line-height: 1.6;
}

.composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-top: 6px;
  border-top: 1px solid #f1f4f9;
}

.topk {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* 普通对话不显示检索条数，发送按钮要自己靠右，否则会贴到左边 */
.composer-btn {
  margin-left: auto;
}

.topk-label {
  font-size: 12px;
  color: var(--text-3);
  white-space: nowrap;
}

.topk-slider {
  width: 96px;
}

.topk-value {
  min-width: 18px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
}

@media (max-width: 900px) {
  .sidebar {
    width: 0;
    border: none;
    overflow: hidden;
  }

  .messages {
    padding: 18px 16px;
  }

  .bubble-wrap {
    max-width: 88%;
  }

  .chat-meta {
    display: none;
  }

  .composer {
    padding: 10px 14px 14px;
  }
}
</style>
