<template>
  <div class="chat-page">
    <button v-if="isNarrow && sidebarOpen" class="sidebar-backdrop" type="button" aria-label="关闭会话侧栏" @click="closeSidebar"></button>
    <aside ref="sidebarRef" class="sidebar" :class="{ 'is-open': sidebarOpen }" :inert="isNarrow && !sidebarOpen" :aria-hidden="isNarrow && !sidebarOpen ? true : undefined" :role="isNarrow ? 'dialog' : undefined" :aria-modal="isNarrow && sidebarOpen ? true : undefined" aria-label="对话设置与历史会话" @keydown="onSidebarKeydown">
      <div class="sidebar-head">
        <div><strong>对话工作台</strong><span>连接知识，发现答案</span></div>
        <button class="sidebar-close icon-button" type="button" aria-label="关闭会话侧栏" @click="closeSidebar"><el-icon><ArrowLeft /></el-icon></button>
      </div>
      <div class="kb-picker">
        <span class="field-label">对话模式</span>
        <el-radio-group v-model="chatMode" size="small" class="mode-switch" aria-label="对话模式" :disabled="streaming" @change="onModeChange">
          <el-radio-button value="rag">知识库问答</el-radio-button>
          <el-radio-button value="chat">普通对话</el-radio-button>
        </el-radio-group>
        <label class="field-label" for="chat-knowledge-base">当前知识库</label>
        <el-select id="chat-knowledge-base" v-model="kbId" placeholder="选择知识库" class="kb-select" :disabled="chatMode === 'chat' || streaming" @change="onKbChange">
          <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id">
            <span>{{ kb.name }}</span><span class="kb-count">{{ kb.document_count }} 篇</span>
          </el-option>
        </el-select>
        <el-button class="new-chat" :disabled="streaming || (chatMode === 'rag' && !kbId)" @click="newConversation"><el-icon><Plus /></el-icon>开启新对话</el-button>
      </div>

      <div class="conv-list">
        <div class="conv-title"><span>最近会话</span><span class="conv-num">{{ conversations.length }}</span></div>
        <div v-for="conv in conversations" :key="conv.id" class="conv-item" :class="{ active: conv.id === currentConversationId }">
          <button class="conv-select" type="button" :disabled="streaming" :aria-current="conv.id === currentConversationId ? 'true' : undefined" :title="conv.title" @click="loadConversation(conv.id)">
            <el-icon class="conv-icon" :size="15"><ChatLineRound /></el-icon><span class="conv-name">{{ conv.title }}</span>
          </button>
          <button class="conv-del" type="button" :disabled="streaming" :aria-label="`删除会话：${conv.title}`" @click="removeConversation(conv.id)"><el-icon :size="14"><Delete /></el-icon></button>
        </div>
        <div v-if="!conversations.length" class="conv-empty"><el-icon :size="25"><ChatLineRound /></el-icon><span>还没有历史会话</span><small>从一个问题开始吧</small></div>
      </div>
      <div class="sidebar-note"><el-icon><InfoFilled /></el-icon><span>知识库问答检索文档内容，普通对话直接与模型交流。</span></div>
    </aside>

    <section class="chat-main" :inert="isNarrow && sidebarOpen">
      <header class="chat-head">
        <div class="chat-heading">
          <button class="sidebar-toggle icon-button" type="button" aria-label="打开对话设置与历史会话" :aria-expanded="sidebarOpen" @click="toggleSidebar"><el-icon :size="19"><Expand /></el-icon></button>
          <span class="chat-heading-icon"><el-icon :size="19"><ChatDotRound /></el-icon></span>
          <div class="chat-heading-copy"><h1 class="chat-title">{{ currentTitle }}</h1><span class="chat-context">{{ chatMode === 'chat' ? '与 AI 助手自由交流' : currentKbName || '选择知识库，开始探索' }}</span></div>
        </div>
        <div class="chat-meta"><span class="mode-badge"><el-icon><DocumentCopy v-if="chatMode === 'rag'" /><ChatLineRound v-else /></el-icon>{{ chatMode === 'chat' ? '普通对话' : '知识库问答' }}</span><span v-if="messages.length" class="message-count">{{ messages.length }} 条消息</span></div>
      </header>

      <div v-if="chatMode === 'rag' && !kbId" class="placeholder">
        <el-empty :description="kbs.length ? '选择一个知识库，开始有据可查的问答。' : '先创建知识库并添加文档，或切换到普通对话。'" />
        <el-button v-if="isNarrow" plain type="primary" @click="toggleSidebar">选择知识库或对话模式</el-button>
        <router-link v-else to="/knowledge" class="empty-link">前往知识库管理<el-icon><ArrowRight /></el-icon></router-link>
      </div>

      <template v-else>
        <div ref="scrollRef" class="messages" aria-label="对话消息" :aria-busy="streaming" @scroll.passive="onScroll">
          <div v-if="!messages.length && !streaming" class="welcome">
            <span class="welcome-mark"><el-icon :size="29"><MagicStick /></el-icon></span>
            <span class="welcome-eyebrow">让知识，触手可及</span>
            <h2>今天，有什么想了解的？</h2>
            <p v-if="chatMode === 'rag'">从文档中寻找线索，让回答有据可查。<br />关于「{{ currentKbName }}」，你可以这样问：</p>
            <p v-else>写作、梳理思路，或探索一个新概念。<br />从你关心的问题开始，与 AI 助手交流。</p>
            <div class="suggestions">
              <button v-for="(s, index) in activeSuggestions" :key="s" class="suggestion" type="button" @click="askSuggestion(s)">
                <span class="suggestion-top"><el-icon><component :is="suggestionIcons[index]" /></el-icon><strong>{{ suggestionTitles[index] }}</strong><el-icon class="suggestion-arrow"><ArrowRight /></el-icon></span>
                <span class="suggestion-text">{{ s }}</span>
              </button>
            </div>
          </div>

          <div v-for="msg in messages" :key="msg.id" class="message" :class="msg.role">
            <el-avatar :size="32" :class="msg.role === 'user' ? 'user-avatar' : 'ai-avatar'"><el-icon v-if="msg.role === 'user'"><User /></el-icon><el-icon v-else><MagicStick /></el-icon></el-avatar>
            <div class="bubble-wrap">
              <div class="message-author">{{ msg.role === 'user' ? '你' : 'AI 助手' }}<span v-if="msg.streaming" class="generating-label">生成中</span></div>
              <div class="bubble">
                <div v-if="msg.role === 'assistant' && msg.streaming && !msg.content" class="thinking" role="status"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span class="thinking-text">{{ chatMode === 'rag' ? '正在检索并生成回答…' : '正在生成回答…' }}</span></div>
                <div v-else-if="msg.role === 'assistant'" class="markdown-body" :class="{ streaming: msg.streaming }" v-html="renderMarkdown(msg.content, !!msg.streaming)"></div>
                <div v-else class="plain-text">{{ msg.content }}</div>
              </div>
              <div v-if="msg.citations?.length" class="citations">
                <el-collapse><el-collapse-item>
                  <template #title><span class="citation-title"><el-icon><DocumentCopy /></el-icon>引用来源<span class="citation-count">{{ msg.citations.length }}</span></span></template>
                  <div v-for="(c, i) in msg.citations" :key="i" class="citation-item">
                    <div class="citation-head"><span class="citation-index">{{ i + 1 }}</span><span class="citation-name" :title="c.filename || '未命名文档'">{{ c.filename || '未命名文档' }}</span><span v-if="c.chunk_index !== null" class="citation-meta">第 {{ c.chunk_index! + 1 }} 段</span><span v-if="c.score !== null" class="citation-score">相似度 {{ (c.score! * 100).toFixed(0) }}%</span></div>
                    <div class="citation-snippet">{{ c.snippet }}</div>
                  </div>
                </el-collapse-item></el-collapse>
              </div>
            </div>
          </div>
          <div v-if="errorMessage" class="error-tip"><el-alert :title="errorMessage" type="error" :closable="false" show-icon /></div>
        </div>

        <div class="composer">
          <div class="composer-box">
            <el-input v-model="question" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" resize="none" aria-label="输入问题" :placeholder="chatMode === 'rag' ? '向知识库提问，让资料为你提供线索…' : '输入你的问题，开始对话…'" :disabled="streaming" @keydown="onComposerKeydown" />
            <div class="composer-actions">
              <div v-if="chatMode === 'rag'" class="topk"><span class="topk-label">检索片段</span><el-slider v-model="topK" :min="1" :max="12" size="small" aria-label="检索片段数" :disabled="streaming" class="topk-slider" /><span class="topk-value">{{ topK }}</span></div>
              <span v-else class="composer-mode"><el-icon><ChatLineRound /></el-icon>普通对话</span>
              <span class="input-hint"><kbd>Enter</kbd> 发送 · <kbd>Shift + Enter</kbd> 换行</span>
              <el-button v-if="streaming" class="composer-btn" type="danger" plain @click="stopStreaming"><el-icon><VideoPause /></el-icon>停止</el-button>
              <el-button v-else class="composer-btn" type="primary" :disabled="!question.trim()" @click="send">发送<el-icon class="send-icon"><Promotion /></el-icon></el-button>
            </div>
          </div>
          <p class="composer-disclaimer">{{ chatMode === 'rag' ? '回答由 AI 生成，请结合引用来源核对关键信息。' : '回答由 AI 生成，请核对重要信息。' }}</p>
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
import { useResponsiveDrawer } from '@/composables/useResponsiveDrawer'
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
const { isNarrow, isOpen: sidebarOpen, panelRef: sidebarRef, close: closeSidebar, toggle: toggleSidebar, onKeydown: onSidebarKeydown } = useResponsiveDrawer()
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
const suggestionTitles = computed(() => chatMode.value === 'chat'
  ? ['认识助手', '写作协助', '概念解读']
  : ['了解全貌', '梳理重点', '查找流程'])
const suggestionIcons = ['Files', 'EditPen', 'Search']
const currentKbName = computed(() => kbs.value.find((kb) => kb.id === kbId.value)?.name || '')

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
  closeSidebar()
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
  closeSidebar()
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

function onComposerKeydown(event: KeyboardEvent) {
  // 输入法确认候选词的 Enter 不发送；Shift + Enter 等组合键保留原生换行。
  if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229
    || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return
  event.preventDefault()
  void send()
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
    // 先记下是否真的产出了回答：下面的兜底文案不是回答，不能给它配引用来源。
    // 否则模型报错时会同时出现「引用来源 6」和一句"抱歉，模型未返回内容"，
    // 看起来像"明明检索到了却不回答"，自相矛盾。
    const answered = !!assistant.content
    // 一个 token 都没收到时也要有明确交代，不能留空气泡
    if (!assistant.content) {
      assistant.content = aborted
        ? '（已停止生成）'
        : mode === 'rag'
          ? '抱歉，模型未返回内容，请重试或调小「检索条数」。'
          : '抱歉，模型未返回内容，请重试。'
    }
    // 回答已收口，此时再挂引用来源，视觉上"先答案后出处"。
    // 只有真正产出了回答才挂：报错/空回答时引用块只会让人困惑。
    assistant.citations = answered ? pendingCitations : undefined
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
.chat-page { position: relative; isolation: isolate; display: flex; height: calc(100vh - var(--app-header-height, 74px) - var(--main-pad-y, 48px)); height: calc(100dvh - var(--app-header-height, 74px) - var(--main-pad-y, 48px)); min-height: 440px; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius-lg); background: var(--surface); box-shadow: var(--shadow-sm); }
.sidebar { display: flex; flex-direction: column; flex-shrink: 0; width: 246px; min-height: 0; border-right: 1px solid var(--line); background: #fafbf8; }
.sidebar-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 25px 21px 0; }
.sidebar-head > div { display: flex; flex-direction: column; gap: 7px; }
.sidebar-head strong { color: var(--text); font-size: 14px; font-weight: 600; }
.sidebar-head > div > span { color: var(--text-3); font-size: 12px; }
.icon-button { display: grid; place-items: center; width: 32px; height: 32px; flex-shrink: 0; padding: 0; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: var(--text-2); cursor: pointer; }
.icon-button:hover { border-color: var(--line-strong); color: var(--brand); background: var(--brand-tint); }
.sidebar-close, .sidebar-toggle { display: none; }
.kb-picker { padding: 24px 18px 20px; border-bottom: 1px solid var(--line); }
.field-label { display: block; margin-bottom: 9px; color: var(--text-3); font-size: 12px; font-weight: 500; letter-spacing: .03em; }
.mode-switch { display: flex; width: 100%; margin-bottom: 21px; padding: 4px; border: 1px solid #e6ebe1; border-radius: 9px; background: #eff3ea; }
.mode-switch :deep(.el-radio-button) { flex: 1; min-width: 0; }
.mode-switch :deep(.el-radio-button__inner) { width: 100%; padding: 9px 0; border: 0 !important; border-radius: 6px !important; background: transparent; color: #7a8976; box-shadow: none !important; font-size: 12px; }
.mode-switch :deep(.el-radio-button.is-active .el-radio-button__inner) { color: var(--brand-dark); background: #fff; box-shadow: 0 1px 4px rgb(30 57 31 / 8%) !important; font-weight: 550; }
.mode-switch :deep(.el-radio-button.is-disabled .el-radio-button__inner) { color: #a1ada0; cursor: not-allowed; }
.kb-select { width: 100%; }
.kb-select :deep(.el-select__wrapper) { background: #fff; font-size: 12px; }
.kb-count { float: right; margin-left: 12px; color: var(--text-3); font-size: 12px; }
.new-chat { width: 100%; height: 38px; margin-top: 14px; border-color: #cfddc9; color: var(--brand-dark); background: #eef4e9; font-size: 12px; }
.new-chat:not(.is-disabled):hover { border-color: #9cba94; color: var(--brand-dark); background: #e6efdf; }
.conv-list { flex: 1; min-height: 0; overflow-y: auto; padding: 21px 11px 15px; }
.conv-title { display: flex; align-items: center; justify-content: space-between; margin-bottom: 13px; padding: 0 10px; color: var(--text-3); font-size: 12px; }
.conv-num { min-width: 19px; padding: 1px 5px; border-radius: 5px; background: #edf1e8; color: #809077; text-align: center; font-size: 10px; font-variant-numeric: tabular-nums; }
.conv-item { display: flex; align-items: center; min-width: 0; margin-bottom: 5px; padding-right: 5px; border: 1px solid transparent; border-radius: 8px; transition: background .15s, border-color .15s; }
.conv-item:hover { background: #f0f4eb; }
.conv-item.active { border-color: #d9e5d2; background: #eaf2e4; }
.conv-select { display: flex; align-items: center; flex: 1; min-width: 0; gap: 9px; padding: 11px 9px; border: 0; border-radius: 7px; background: transparent; color: var(--text-2); font-size: 12px; text-align: left; cursor: pointer; }
.conv-select:disabled, .conv-del:disabled { cursor: not-allowed; opacity: .5; }
.conv-item.active .conv-select { color: var(--brand-dark); font-weight: 550; }
.conv-icon { flex-shrink: 0; color: #8e9b86; }
.conv-item.active .conv-icon { color: #5f8861; }
.conv-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-del { display: grid; place-items: center; flex-shrink: 0; width: 25px; height: 27px; padding: 0; border: 0; border-radius: 6px; background: transparent; color: #92a08a; opacity: 0; cursor: pointer; transition: opacity .15s, color .15s; }
.conv-item:hover .conv-del, .conv-item:focus-within .conv-del { opacity: 1; }
.conv-del:hover { background: #f8ece6; color: var(--danger); }
.conv-empty { display: flex; flex-direction: column; align-items: center; gap: 9px; padding: 39px 0; color: #a1af96; font-size: 12px; }
.conv-empty .el-icon { margin-bottom: 3px; color: #b5c1ab; }
.conv-empty small { color: #a8b39f; font-size: 12px; }
.sidebar-note { display: flex; align-items: flex-start; gap: 7px; margin: 0 18px; padding: 15px 0 19px; border-top: 1px solid var(--line); color: #8c9a83; font-size: 12px; line-height: 1.8; }
.sidebar-note .el-icon { flex-shrink: 0; margin-top: 4px; font-size: 12px; }
.chat-main { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; background: #fff; }
.chat-head { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 16px; min-height: 77px; padding: 15px 29px; border-bottom: 1px solid var(--line); }
.chat-heading { display: flex; align-items: center; gap: 12px; min-width: 0; }
.chat-heading-icon { display: grid; place-items: center; width: 37px; height: 39px; flex-shrink: 0; border: 1px solid #e1eadb; border-radius: 10px; color: #5e8059; background: #f2f6ec; }
.chat-heading-copy { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.chat-title { overflow: hidden; max-width: 360px; margin: 0; color: var(--text); font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.chat-context { overflow: hidden; color: var(--text-3); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.chat-meta { display: flex; align-items: center; flex-shrink: 0; gap: 14px; }
.mode-badge { display: inline-flex; align-items: center; gap: 6px; padding: 6px 9px; border: 1px solid #e3ebdd; border-radius: 6px; background: #f7f9f2; color: #68805b; font-size: 12px; white-space: nowrap; }
.mode-badge .el-icon { font-size: 12px; }
.message-count { color: var(--text-3); font-size: 12px; white-space: nowrap; }
.placeholder { display: flex; flex: 1; flex-direction: column; align-items: center; justify-content: center; gap: 3px; padding: 24px; text-align: center; }
.empty-link { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; text-decoration: none; }
.messages { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 29px clamp(22px, 3vw, 55px) 22px; scroll-padding-bottom: 24px; }
.welcome { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100%; padding: 22px 0 32px; text-align: center; }
.welcome-mark { display: grid; place-items: center; width: 64px; height: 66px; margin-bottom: 21px; border: 1px solid #d9e5cd; border-radius: 19px; background: #f0f5e6; color: #5c7f4d; transform: rotate(-6deg); }
.welcome-mark .el-icon { transform: rotate(6deg); }
.welcome-eyebrow { color: #91a184; font-size: 12px; letter-spacing: .13em; }
.welcome h2 { margin: 13px 0 12px; color: var(--text); font-size: clamp(22px, 2.1vw, 29px); line-height: 1.5; font-weight: 550; letter-spacing: -.04em; }
.welcome > p { max-width: 480px; margin: 0; color: var(--text-3); font-size: 12px; line-height: 1.9; overflow-wrap: anywhere; }
.suggestions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; width: 100%; max-width: 608px; margin-top: 31px; }
.suggestion { min-width: 0; padding: 17px 15px 16px; border: 1px solid #e6ecdf; border-radius: 11px; background: #fcfdf9; color: var(--text-2); text-align: left; cursor: pointer; transition: background .18s, border-color .18s, transform .18s; }
.suggestion:hover { transform: translateY(-3px); border-color: #b9cfab; background: #f4f8ee; }
.suggestion-top { display: flex; align-items: center; gap: 7px; margin-bottom: 11px; color: #7c926e; }
.suggestion-top > .el-icon:first-child { font-size: 15px; }
.suggestion-top strong { flex: 1; color: #536d48; font-size: 12px; font-weight: 550; }
.suggestion-arrow { color: #acb99f; font-size: 12px; }
.suggestion-text { display: block; color: var(--text-3); font-size: 12px; line-height: 1.85; }
.message { display: flex; gap: 12px; max-width: 950px; margin: 0 auto 29px; }
.message.user { flex-direction: row-reverse; }
.user-avatar { flex-shrink: 0; border: 1px solid #e2e7dc; background: #f1f4eb; color: #77866c; border-radius: 10px; }
.ai-avatar { flex-shrink: 0; border: 1px solid #d5e2c9; background: #e9f1de; color: #557b47; border-radius: 10px; }
.bubble-wrap { max-width: 88%; min-width: 0; flex: 1; }
.message-author { display: flex; align-items: center; gap: 9px; margin: 2px 0 7px; color: #7e8e73; font-size: 12px; font-weight: 550; }
.generating-label { color: #9bad8e; font-size: 10px; font-weight: 400; }
.message.user .bubble-wrap { display: flex; flex: 0 1 auto; flex-direction: column; align-items: flex-end; max-width: 77%; }
.bubble { padding: 3px 0; color: var(--text); font-size: 14px; line-height: 1.85; }
.message.user .bubble { padding: 12px 17px; border: 1px solid #e0e9d7; border-radius: 12px 3px 12px 12px; background: #eef4e7; color: #3c5838; }
.plain-text { white-space: pre-wrap; overflow-wrap: anywhere; }
/* 光标画在正文最后一个块元素的末尾，而不是作为兄弟节点跟在 .markdown-body 后面：
   后者是块级元素，紧跟其后的行内 span 只能另起一行，光标会孤零零掉到下一行（看起来像渲染出错）。
   用伪元素内联在文字流里，收尾时紧贴最后一个字符，符合"正在打字"的直觉。
   闪烁用 opacity 而非 step-end 的硬切，减少突兀感；1.06s 慢一档，长时间生成时不晃眼。

   只在段落/标题/引用/列表项这类文字块上画：流式若正好停在代码块里，
   伪元素会被 flex/表格布局挤出怪异位置，那种情况下宁可不显示光标。 */
.markdown-body.streaming :deep(> :last-child) {
  &:is(p, h1, h2, h3, h4, h5, h6, blockquote, li)::after {
    content: '';
    display: inline-block;
    width: 2px;
    height: 1em;
    margin-left: 2px;
    background: var(--brand);
    vertical-align: text-bottom;
    animation: caret 1.06s steps(2, start) infinite;
  }
}
@keyframes caret { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
.thinking { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; min-height: 25px; color: var(--text-3); font-size: 12px; }
.thinking-text { margin-left: 6px; }
.dot { width: 5px; height: 5px; border-radius: 50%; background: #9bb28c; animation: bounce 1.2s infinite ease-in-out; }
.dot:nth-child(2) { animation-delay: .15s; }
.dot:nth-child(3) { animation-delay: .3s; }
@keyframes bounce { 0%, 60%, 100% { transform: translateY(0); opacity: .5; } 30% { transform: translateY(-3px); opacity: 1; } }
/* 减少动态效果时动画会停住，三点会定格成三个半透明灰点、看不出"还在处理"。
   改成静态实心点，配合旁边的「生成中」文字，状态依然明确。 */
@media (prefers-reduced-motion: reduce) {
  .dot { animation: none !important; opacity: 1; }
  .markdown-body.streaming :deep(> :last-child):is(p, h1, h2, h3, h4, h5, h6, blockquote, li)::after { animation: none !important; opacity: 1; }
}
.citations { margin-top: 14px; padding: 2px 13px; border: 1px solid #e4eadc; border-radius: 10px; background: #fafcf7; }
.citations :deep(.el-collapse) { border: 0; background: transparent; --el-collapse-header-height: 37px; --el-collapse-border-color: transparent; }
.citations :deep(.el-collapse-item__header) { padding: 0; border: 0; background: transparent; color: var(--text-2); font-size: 12px; }
.citations :deep(.el-collapse-item__wrap) { border: 0; background: transparent; }
.citations :deep(.el-collapse-item__content) { padding-bottom: 12px; }
.citation-title { display: flex; align-items: center; gap: 7px; font-weight: 500; }
.citation-title > .el-icon { color: #859c74; font-size: 13px; }
.citation-count { display: inline-grid; place-items: center; min-width: 18px; height: 17px; margin-left: 2px; padding: 0 5px; border-radius: 4px; background: #edf3e6; color: #7c946b; font-size: 10px; line-height: 1; }
.citation-item { margin-bottom: 8px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
.citation-item:last-child { margin-bottom: 0; }
.citation-head { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-bottom: 7px; }
.citation-index { display: grid; place-items: center; width: 18px; height: 18px; flex-shrink: 0; border-radius: 4px; background: #edf3e6; color: #779364; font-size: 12px; font-weight: 600; }
.citation-name { overflow: hidden; max-width: 100%; color: var(--text); font-size: 12px; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }
.citation-meta, .citation-score { color: var(--text-3); font-size: 12px; white-space: nowrap; }
.citation-score { margin-left: auto; padding: 1px 6px; border-radius: 4px; background: #f3f6ed; color: #81936d; }
.citation-snippet { display: -webkit-box; overflow: hidden; -webkit-line-clamp: 3; -webkit-box-orient: vertical; color: var(--text-2); font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; }
.error-tip { max-width: 950px; margin: 0 auto 14px; }
.composer { flex-shrink: 0; padding: 8px clamp(22px, 3vw, 55px) 14px; background: #fff; }
.composer-box { max-width: 950px; margin: 0 auto; padding: 12px 15px 10px; border: 1px solid #dce5d5; border-radius: 14px; background: #fff; box-shadow: 0 3px 12px rgb(37 66 32 / 3%); transition: border-color .18s, box-shadow .18s; }
.composer-box:focus-within { border-color: #94b189; box-shadow: 0 0 0 3px rgb(99 139 79 / 7%); }
.composer-box :deep(.el-textarea__inner) { padding: 2px 2px 9px; box-shadow: none !important; background: transparent; color: var(--text); font-size: 13px; line-height: 1.8; }
.composer-box :deep(.el-textarea.is-disabled .el-textarea__inner) { background: transparent; color: #a4b099; }
.composer-box :deep(.el-textarea__inner::placeholder) { color: #a0ad96; }
.composer-actions { display: flex; align-items: center; gap: 14px; padding-top: 9px; border-top: 1px solid #f0f3eb; }
.topk { display: flex; align-items: center; gap: 10px; }
.topk-label { color: #8b9c7e; font-size: 12px; white-space: nowrap; }
.topk-slider { width: 75px; height: 28px; --el-slider-button-size: 10px; --el-slider-height: 3px; }
.topk-value { min-width: 17px; color: #718661; font-size: 12px; font-weight: 550; font-variant-numeric: tabular-nums; }
.composer-mode { display: inline-flex; align-items: center; gap: 6px; color: #8b9c7e; font-size: 12px; white-space: nowrap; }
.input-hint { margin-left: auto; color: #a5b098; font-size: 10px; white-space: nowrap; }
.input-hint kbd { font-family: inherit; font-size: inherit; }
.composer-btn { height: 32px; margin-left: auto; border-radius: 8px; font-size: 12px; }
.input-hint + .composer-btn { margin-left: 0; }
.send-icon { margin: 0 0 0 7px !important; font-size: 13px; }
.composer-disclaimer { margin: 10px 0 0; color: var(--text-3); font-size: 10px; line-height: 1.6; text-align: center; }
.sidebar-backdrop { display: none; }
@media (max-width: 1280px) { .sidebar { width: 226px; } .chat-head { padding-inline: 23px; } .message-count, .input-hint { display: none; } .input-hint + .composer-btn { margin-left: auto; } .suggestion { padding-inline: 12px; } }
@media (max-width: 1050px) and (min-width: 901px) { .sidebar { width: 214px; } .sidebar-head { padding-inline: 16px; } .kb-picker { padding-inline: 13px; } .welcome h2 { font-size: 21px; } .suggestions { gap: 7px; } .suggestion-top { gap: 5px; } .suggestion-arrow { display: none; } .suggestion-text { font-size: 10px; } .mode-badge { padding-inline: 6px; font-size: 10px; } .chat-heading-icon { display: none; } }
@media (max-width: 900px) {
  .chat-page { min-height: 360px; }
  .sidebar { position: absolute; z-index: 10; inset: 0 auto 0 0; width: min(286px, calc(100% - 42px)); border-right: 1px solid var(--line); transform: translateX(-100%); transition: transform .2s; }
  .sidebar.is-open { transform: translateX(0); box-shadow: 8px 0 24px rgb(26 50 26 / 8%); }
  .sidebar-backdrop { position: absolute; z-index: 8; display: block; inset: 0; width: 100%; height: 100%; border: 0; background: rgb(30 49 28 / 28%); backdrop-filter: blur(2px); }
  .sidebar-close, .sidebar-toggle { display: grid; }
  .sidebar-head { padding-top: 21px; }
  .chat-head { min-height: 71px; padding: 14px 21px; }
  .chat-heading-icon { display: none; }
  .conv-del { opacity: .75; }
  .chat-title { max-width: none; }
  .messages { padding: 24px 24px 17px; }
  .composer { padding: 8px 24px 13px; }
  .welcome h2 { font-size: 26px; }
  .input-hint { display: inline; }
  .input-hint + .composer-btn { margin-left: 0; }
}
@media (max-width: 600px) {
  .chat-page { min-height: min(410px, calc(100dvh - 88px)); border-radius: 13px; }
  .chat-head { gap: 10px; min-height: 66px; padding: 12px 14px; }
  .chat-heading { gap: 9px; }
  .chat-title { font-size: 12px; }
  .chat-context { max-width: 170px; font-size: 10px; }
  .chat-meta { gap: 0; }
  .mode-badge { padding: 5px 7px; font-size: 10px; }
  .mode-badge > .el-icon { display: none; }
  .messages { padding: 18px 15px 12px; }
  .welcome { padding: 12px 0 18px; }
  .welcome-mark { width: 48px; height: 50px; margin-bottom: 17px; border-radius: 15px; }
  .welcome-mark .el-icon { font-size: 24px !important; }
  .welcome-eyebrow { font-size: 10px; }
  .welcome h2 { margin: 11px 0 9px; font-size: 22px; }
  .welcome > p { padding-inline: 4px; font-size: 12px; }
  .suggestions { grid-template-columns: 1fr; gap: 8px; max-width: 350px; margin-top: 22px; }
  .suggestion { display: flex; align-items: center; gap: 12px; padding: 13px 12px; }
  .suggestion-top { flex-shrink: 0; gap: 6px; margin: 0; }
  .suggestion-top strong { font-size: 12px; }
  .suggestion-arrow { display: none; }
  .suggestion-text { font-size: 12px; }
  .message { gap: 9px; margin-bottom: 22px; }
  .bubble-wrap { max-width: calc(100% - 41px); }
  .message.user .bubble-wrap { max-width: calc(100% - 41px); }
  .message-author { font-size: 10px; }
  .bubble, .bubble :deep(.markdown-body) { font-size: 14px; }
  .message.user .bubble { padding: 10px 13px; }
  .citation-item { padding: 10px; }
  .citation-head { gap: 5px; }
  .citation-name { max-width: calc(100% - 25px); }
  .citation-score { margin-left: 0; }
  .composer { padding: 6px 13px 10px; }
  .composer-box { padding: 10px 11px 8px; border-radius: 11px; }
  .composer-box :deep(.el-textarea__inner) { font-size: 16px; }
  .composer-actions { gap: 8px; }
  .topk { gap: 7px; }
  .topk-slider { width: 66px; }
  .input-hint { display: none; }
  .input-hint + .composer-btn { margin-left: auto; }
  .composer-disclaimer { margin-top: 8px; font-size: 10px; }
}
@media (hover: none) { .conv-del { opacity: .75; } }
</style>
