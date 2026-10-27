<template>
  <div class="chat-page" :lang="locale">
    <button v-if="isNarrow && sidebarOpen" class="sidebar-backdrop" type="button" :aria-label="t('chat.sidebar.close')" @click="closeSidebar"></button>
    <aside ref="sidebarRef" class="sidebar" :class="{ 'is-open': sidebarOpen }" :inert="isNarrow && !sidebarOpen" :aria-hidden="isNarrow && !sidebarOpen ? true : undefined" :role="isNarrow ? 'dialog' : undefined" :aria-modal="isNarrow && sidebarOpen ? true : undefined" :aria-label="t('chat.sidebar.aria')" @keydown="onSidebarKeydown">
      <div class="sidebar-head">
        <div><strong>{{ t('chat.sidebar.title') }}</strong><span>{{ t('chat.sidebar.subtitle') }}</span></div>
        <button class="sidebar-close icon-button" type="button" :aria-label="t('chat.sidebar.close')" @click="closeSidebar"><el-icon><ArrowLeft /></el-icon></button>
      </div>
      <div class="kb-picker">
        <span class="field-label">{{ t('chat.mode.label') }}</span>
        <el-radio-group v-model="chatMode" size="small" class="mode-switch" :aria-label="t('chat.mode.label')" :disabled="controlsLocked" @change="onModeChange">
          <el-radio-button value="rag">{{ t('chat.mode.rag') }}</el-radio-button>
          <el-radio-button value="chat">{{ t('chat.mode.chat') }}</el-radio-button>
        </el-radio-group>
        <label class="field-label" for="chat-knowledge-base">{{ t('chat.kb.label') }}</label>
        <el-select id="chat-knowledge-base" v-model="kbId" :placeholder="t('chat.kb.placeholder')" class="kb-select" :disabled="chatMode === 'chat' || controlsLocked" @change="onKbChange">
          <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id">
            <span>{{ kb.name }}</span><span class="kb-count">{{ t('chat.kb.docCount', { count: formatNumber(kb.document_count) }, kb.document_count) }}</span>
          </el-option>
        </el-select>
        <el-button class="new-chat" :disabled="controlsLocked || (chatMode === 'rag' && !kbId)" @click="newConversation"><el-icon><Plus /></el-icon>{{ t('chat.newChat') }}</el-button>
      </div>

      <div class="conv-list">
        <div class="conv-title"><span>{{ t('chat.recent.title') }}</span><span class="conv-num">{{ formatNumber(conversations.length) }}</span></div>
        <div v-for="conv in conversations" :key="conv.id" class="conv-item" :class="{ active: conv.id === currentConversationId }">
          <button class="conv-select" type="button" :disabled="controlsLocked" :aria-current="conv.id === currentConversationId ? 'true' : undefined" :title="conv.title" @click="loadConversation(conv.id)">
            <el-icon class="conv-icon" :size="15"><ChatLineRound /></el-icon><span class="conv-name">{{ conv.title }}</span>
          </button>
          <button class="conv-del" type="button" :disabled="controlsLocked" :aria-label="t('chat.recent.deleteAria', { title: conv.title })" @click="removeConversation(conv.id)"><el-icon :size="14"><Delete /></el-icon></button>
        </div>
        <div v-if="!conversations.length" class="conv-empty"><el-icon :size="25"><ChatLineRound /></el-icon><span>{{ t('chat.recent.empty') }}</span><small>{{ t('chat.recent.emptyHint') }}</small></div>
        </div>
      <div class="sidebar-note"><el-icon><InfoFilled /></el-icon><span>{{ t('chat.sidebar.note') }}</span></div>
    </aside>

    <section class="chat-main" :inert="isNarrow && sidebarOpen">
      <header class="chat-head">
        <div class="chat-heading">
          <button class="sidebar-toggle icon-button" type="button" :aria-label="t('chat.sidebar.open')" :aria-expanded="sidebarOpen" @click="toggleSidebar"><el-icon :size="19"><Expand /></el-icon></button>
          <span class="chat-heading-icon"><el-icon :size="19"><ChatDotRound /></el-icon></span>
          <div class="chat-heading-copy"><h1 class="chat-title">{{ currentTitle }}</h1><span class="chat-context">{{ chatMode === 'chat' ? t('chat.head.freeChat') : currentKbName || t('chat.head.selectKb') }}</span></div>
        </div>
        <div class="chat-meta"><span class="mode-badge"><el-icon><DocumentCopy v-if="chatMode === 'rag'" /><ChatLineRound v-else /></el-icon>{{ chatMode === 'chat' ? t('chat.mode.chat') : t('chat.mode.rag') }}</span><span v-if="messages.length" class="message-count">{{ t('chat.head.messageCount', { count: formatNumber(messages.length) }, messages.length) }}</span></div>
      </header>

      <div v-if="chatMode === 'rag' && kbId" class="scope-bar" role="group" :aria-label="t('chat.scope.label')">
        <div class="scope-row">
          <div class="scope-summary">
            <span class="scope-label"><el-icon><DocumentCopy /></el-icon>{{ t('chat.scope.label') }}</span>
            <template v-if="documentScope.length">
              <span v-for="item in documentScope" :key="item.document_id" class="scope-chip">
                <span class="scope-chip-name" :title="item.filename">{{ item.filename }}</span>
                <button
                  class="scope-chip-remove"
                  type="button"
                  :disabled="scopeLocked"
                  :aria-label="t('chat.scope.removeAria', { name: item.filename })"
                  @click="removeScopeDocument(item.document_id)"
                ><el-icon :size="11"><CircleClose /></el-icon></button>
              </span>
            </template>
            <span v-else class="scope-all"><el-icon :size="12"><Files /></el-icon>{{ t('chat.scope.all') }}</span>
          </div>
          <div class="scope-actions">
            <span v-if="scopeSaving" class="scope-saving" role="status">{{ t('chat.scope.saving') }}</span>
            <el-button size="small" :disabled="scopeLocked" :title="scopeLocked ? t('chat.scope.lockedHint') : undefined" @click="openScopeDialog">{{ documentScope.length ? t('chat.scope.change') : t('chat.scope.choose') }}</el-button>
            <el-button v-if="documentScope.length" size="small" text :disabled="scopeLocked" @click="clearDocumentScope">{{ t('chat.scope.clear') }}</el-button>
          </div>
        </div>
        <el-alert v-if="scopeError" class="scope-alert" :title="scopeError" type="error" :closable="true" show-icon @close="scopeError = ''" />
      </div>

      <div v-if="chatMode === 'rag' && !kbId" class="placeholder">
        <el-empty :description="kbs.length ? t('chat.empty.selectKb') : t('chat.empty.noKb')" />
        <el-button v-if="isNarrow" plain type="primary" @click="toggleSidebar">{{ t('chat.empty.choose') }}</el-button>
        <router-link v-else to="/knowledge" class="empty-link">{{ t('chat.empty.goKnowledge') }}<el-icon><ArrowRight /></el-icon></router-link>
      </div>

      <template v-else>
        <div ref="scrollRef" class="messages" :aria-label="t('chat.messagesAria')" :aria-busy="busy" @scroll.passive="onScroll">
          <div v-if="!messages.length && !streaming" class="welcome">
            <span class="welcome-mark"><el-icon :size="29"><MagicStick /></el-icon></span>
            <span class="welcome-eyebrow">{{ t('chat.welcome.eyebrow') }}</span>
            <h2>{{ t('chat.welcome.title') }}</h2>
            <p v-if="chatMode === 'rag'">{{ t('chat.welcome.ragLead') }}<br />{{ t('chat.welcome.ragKb', { kb: currentKbName }) }}</p>
            <p v-else>{{ t('chat.welcome.chatLead') }}<br />{{ t('chat.welcome.chatSub') }}</p>
            <p v-if="chatMode === 'rag' && documentScope.length" class="welcome-scope">
              <el-icon :size="13"><DocumentCopy /></el-icon>
              {{ t('chat.scope.restrictedHint', { count: formatNumber(documentScope.length) }, documentScope.length) }}
            </p>
            <div class="suggestions">
              <button v-for="s in activeSuggestions" :key="s.text" class="suggestion" type="button" @click="askSuggestion(s.text)">
                <span class="suggestion-top"><el-icon><component :is="s.icon" /></el-icon><strong>{{ s.title }}</strong><el-icon class="suggestion-arrow"><ArrowRight /></el-icon></span>
                <span class="suggestion-text">{{ s.text }}</span>
              </button>
            </div>
          </div>

          <div v-for="msg in messages" :key="msg.id" class="message" :class="msg.role">
            <el-avatar :size="32" :class="msg.role === 'user' ? 'user-avatar' : 'ai-avatar'"><el-icon v-if="msg.role === 'user'"><User /></el-icon><el-icon v-else><MagicStick /></el-icon></el-avatar>
            <div class="bubble-wrap">
              <div class="message-author">{{ msg.role === 'user' ? t('chat.author.you') : t('chat.author.assistant') }}<span v-if="msg.streaming" class="generating-label">{{ t('chat.generating') }}</span><span v-else-if="msg.status && msg.status !== 'completed'" class="generating-label">{{ requestStatusLabel(msg.status) }}</span></div>
              <div class="bubble">
                <div v-if="msg.role === 'assistant' && msg.streaming && !msg.content" class="thinking" role="status"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span class="thinking-text">{{ chatMode === 'rag' ? t('chat.thinking.rag') : t('chat.thinking.chat') }}</span></div>
                <div v-else-if="msg.role === 'assistant'" class="markdown-body" :class="{ streaming: msg.streaming }" v-html="renderMarkdown(msg.content, !!msg.streaming)"></div>
                <div v-else class="plain-text">{{ msg.content }}</div>
              </div>
              <div v-if="msg.citations?.length" class="citations">
                <el-collapse><el-collapse-item>
                  <template #title><span class="citation-title"><el-icon><DocumentCopy /></el-icon>{{ t('chat.citations.title') }}<span class="citation-count">{{ formatNumber(msg.citations.length) }}</span></span></template>
                    <div v-for="(c, i) in msg.citations" :key="i" class="citation-item">
                    <div class="citation-head"><span class="citation-index">{{ formatNumber(c.source_index ?? (i + 1)) }}</span><span class="citation-name" :title="c.filename || t('chat.citations.untitled')">{{ c.filename || t('chat.citations.untitled') }}</span><span v-if="c.chunk_index !== null" class="citation-meta">{{ t('chat.citations.chunk', { index: formatNumber(c.chunk_index! + 1) }) }}</span><span v-if="c.score !== null" class="citation-score">{{ t('chat.citations.score', { percent: formatNumber(Math.round(c.score! * 100)) }) }}</span></div>
                      <div v-if="sourceLocationLabels(c, t).length" class="citation-location">{{ sourceLocationLabels(c, t).join(' · ') }}</div>
                      <div class="citation-snippet">{{ c.snippet }}</div>
                      <div class="citation-actions">
                        <el-button v-if="c.document_id && c.chunk_index != null" link type="primary" @click="viewSource(c)">{{ t('chat.citations.view') }}</el-button>
                        <el-button v-if="chatMode === 'rag' && kbId && c.document_id && !isSoleScopeDocument(c.document_id)" link type="primary" :disabled="scopeLocked" @click="restrictToCitation(c)">{{ t('chat.scope.onlyThis') }}</el-button>
                      </div>
                    </div>
                </el-collapse-item></el-collapse>
              </div>
            </div>
          </div>
          <div v-if="errorMessage" class="error-tip"><el-alert :title="errorMessage" type="error" :closable="false" show-icon /></div>
        </div>

        <div class="composer">
          <div class="composer-box">
            <el-input v-model="question" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" resize="none" :aria-label="t('chat.composer.aria')" :placeholder="chatMode === 'rag' ? t('chat.composer.placeholderRag') : t('chat.composer.placeholderChat')" :disabled="busy" @keydown="onComposerKeydown" />
            <div class="composer-actions">
              <div v-if="chatMode === 'rag'" class="topk"><span class="topk-label">{{ t('chat.composer.topk') }}</span><el-slider v-model="topK" :min="1" :max="12" size="small" :aria-label="t('chat.composer.topkAria')" :disabled="busy" class="topk-slider" /><span class="topk-value">{{ formatNumber(topK) }}</span></div>
              <span v-else class="composer-mode"><el-icon><ChatLineRound /></el-icon>{{ t('chat.mode.chat') }}</span>
              <span class="input-hint"><kbd>Enter</kbd> {{ t('chat.composer.hintSend') }} · <kbd>Shift + Enter</kbd> {{ t('chat.composer.hintNewline') }}</span>
              <el-button v-if="busy" class="composer-btn" type="danger" plain :loading="stopping" :disabled="stopping" @click="stopStreaming"><el-icon><VideoPause /></el-icon>{{ stopping ? t('chat.composer.stopping') : t('chat.composer.stop') }}</el-button>
              <el-button v-else class="composer-btn" type="primary" :disabled="!question.trim() || scopeSaving" @click="send">{{ t('chat.composer.send') }}<el-icon class="send-icon"><Promotion /></el-icon></el-button>
              </div>
            </div>
          <p class="composer-disclaimer">{{ chatMode === 'rag' ? t('chat.composer.disclaimerRag') : t('chat.composer.disclaimerChat') }}</p>
        </div>
      </template>
    </section>
    <SourcePreviewDrawer v-model="sourceOpen" :kb-id="kbId" :citation="selectedCitation" />
    <DocumentScopeDialog
      v-model="scopeDialogOpen"
      :kb-id="kbId"
      :selected="documentScope"
      :max="MAX_SCOPE_DOCUMENTS"
      :saving="scopeSaving"
      :error="scopeError"
      @confirm="applyDocumentScope"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/common'
import 'highlight.js/styles/github.css'
import { useResponsiveDrawer } from '@/composables/useResponsiveDrawer'
import { formatNumber } from '@/i18n/format'
import { getCompletedCitations, type ChatCompletion } from '@/utils/citations'
import { createChatRequestId } from '@/utils/chatRequest'
import { resolveTopK } from '@/utils/retrievalTopK'
import {
  MAX_SCOPE_DOCUMENTS,
  applyScopeUpdate,
  buildScopeSendOptions,
  conversationScope,
  normalizeDocumentScope,
  planScopeChange,
  resolveScopePatchResponse,
  scopeItemFromDocument,
  type DocumentScopeItem
} from '@/utils/documentScope'
import { createSessionEpoch } from '@/utils/sessionEpoch'
import { sourceLocationLabels } from '@/utils/sourceLocation'
import SourcePreviewDrawer from '@/components/SourcePreviewDrawer.vue'
import DocumentScopeDialog from '@/components/DocumentScopeDialog.vue'
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
  status?: 'processing' | 'completed' | 'failed' | 'cancelled'
}

const { t, locale } = useI18n()

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
/** 滑块初值来自服务端 RETRIEVAL_TOP_K；取不到时为 null，此时退回显式发送滑块值。 */
const defaultTopK = ref<number | null>(null)
const topK = ref(6)
const streaming = ref(false)
const stopping = ref(false)
const busy = computed(() => streaming.value || stopping.value)
/** 当前限定的文档范围；空数组表示整个知识库。无会话时这是待发送的草稿。 */
const documentScope = ref<DocumentScopeItem[]>([])
const scopeRevision = ref(0)
const scopeDialogOpen = ref(false)
const scopeSaving = ref(false)
const scopeError = ref('')
/** 生成回答或保存范围期间，禁止切换依据以及会话/知识库/模式。 */
const scopeLocked = computed(() => busy.value || scopeSaving.value)
const controlsLocked = scopeLocked
const sourceOpen = ref(false)
const selectedCitation = ref<Citation | null>(null)
let activeRequestId: string | null = null
const errorMessage = ref('')
const scrollRef = ref<HTMLElement>()
let controller: AbortController | null = null
let disposed = false
/**
 * 会话详情/生成/新建这些动作都会改写消息与范围。谁最后开始谁说了算：
 * 慢回来的旧详情不能覆盖已经开始的生成。
 */
const sessionEpoch = createSessionEpoch()

/**
 * 建议问题存 key 而不是文案：语言切换时由 computed 重新取 t()，
 * 已经发出去的消息不受影响。
 */
const suggestionSets = {
  rag: [
    { text: 'chat.suggestions.ragContents', title: 'chat.suggestions.ragContentsTitle', icon: 'Files' },
    { text: 'chat.suggestions.ragSummary', title: 'chat.suggestions.ragSummaryTitle', icon: 'EditPen' },
    { text: 'chat.suggestions.ragProcess', title: 'chat.suggestions.ragProcessTitle', icon: 'Search' }
  ],
  chat: [
    { text: 'chat.suggestions.chatIntro', title: 'chat.suggestions.chatIntroTitle', icon: 'Files' },
    { text: 'chat.suggestions.chatEmail', title: 'chat.suggestions.chatEmailTitle', icon: 'EditPen' },
    { text: 'chat.suggestions.chatVector', title: 'chat.suggestions.chatVectorTitle', icon: 'Search' }
  ]
} as const

const activeSuggestions = computed(() =>
  suggestionSets[chatMode.value].map((item) => ({
    text: t(item.text),
    title: t(item.title),
    icon: item.icon
  }))
)
const currentKbName = computed(() => kbs.value.find((kb) => kb.id === kbId.value)?.name || '')

const currentTitle = computed(() => {
  if (!messages.value.length) return t('chat.newConversation')
  const first = messages.value.find((m) => m.role === 'user')
  return first?.content.slice(0, 24) || t('chat.newConversation')
})

function resetDocumentScope() {
  documentScope.value = []
  scopeRevision.value = 0
  scopeDialogOpen.value = false
  scopeSaving.value = false
  scopeError.value = ''
}

function isSoleScopeDocument(documentId: string) {
  return documentScope.value.length === 1 && documentScope.value[0]?.document_id === documentId
}

function restrictToCitation(citation: Citation) {
  if (scopeLocked.value || chatMode.value !== 'rag') return
  const item = scopeItemFromDocument(citation.document_id, citation.filename, citation.index_version)
  if (item) void applyDocumentScope([item])
}

function openScopeDialog() {
  if (scopeLocked.value || chatMode.value !== 'rag' || !kbId.value) return
  sessionEpoch.begin()
  scopeError.value = ''
  scopeDialogOpen.value = true
}

function removeScopeDocument(documentId: string) {
  if (scopeLocked.value) return
  void applyDocumentScope(documentScope.value.filter((item) => item.document_id !== documentId))
}

function clearDocumentScope() {
  if (scopeLocked.value) return
  void applyDocumentScope([])
}

/**
 * 保存范围：
 * - 无会话：只更新本地草稿，第一次 send 时随 document_ids 提交；指纹相同直接跳过
 * - 已有会话：即使文档 ID 完全相同也 PATCH。文档可能已经重新索引，本地缓存的
 *   index_version 是旧的（文档列表也不带最新代次），只有服务端能重新校验并绑定
 *   到最新代次，所以"用户再选一次同一篇"必须真的发出去。
 * - 响应不完整：保持原范围并报错，绝不把请求里的 next 当成已生效结果，
 *   否则清空/扩大范围会在界面上"成功"，服务端却仍是旧范围。
 * - 失败：当前 UI 范围原样保留，错误在范围条与弹窗内可见
 */
async function applyDocumentScope(items: DocumentScopeItem[]) {
  if (scopeLocked.value) return
  // 较早发出的会话详情也不能在本次范围确认期间改变会话或依据。
  sessionEpoch.begin()
  const next = normalizeDocumentScope(items)
  const conversationId = currentConversationId.value
  const action = planScopeChange(documentScope.value, next, !!conversationId)

  if (action === 'skip') {
    scopeDialogOpen.value = false
    scopeError.value = ''
    return
  }

  if (!conversationId) {
    documentScope.value = next
    scopeRevision.value = 0
    scopeError.value = ''
    scopeDialogOpen.value = false
    return
  }

  const previous = { items: documentScope.value, revision: scopeRevision.value }
  scopeSaving.value = true
  scopeError.value = ''
  try {
    const { data } = await chatApi.setDocumentScope(
      conversationId,
      next.map((item) => item.document_id),
      scopeRevision.value
    )
    // 保存期间会话切换已被禁用；这里仍兜底，避免响应落到别的会话上
    if (currentConversationId.value !== conversationId) return
    const resolution = resolveScopePatchResponse(previous, data)
    if (resolution.status === 'applied') {
      documentScope.value = resolution.snapshot.items
      scopeRevision.value = resolution.snapshot.revision
      scopeDialogOpen.value = false
      return
    }
    // 服务端没给出完整范围，或回的是更旧的版本：一律退回确认前的范围
    documentScope.value = previous.items
    scopeRevision.value = previous.revision
    scopeError.value = resolution.status === 'stale'
      ? t('chat.scope.conflict')
      : t('chat.scope.incomplete')
  } catch (err: any) {
    if (currentConversationId.value !== conversationId) return
    documentScope.value = previous.items
    scopeRevision.value = previous.revision
    const status = err?.response?.status
    const serverMessage = typeof err?.response?.data?.message === 'string'
      ? err.response.data.message.trim()
      : ''
    scopeError.value = serverMessage
      || (status === 409 ? t('chat.scope.conflict') : err?.message || t('chat.scope.saveFailed'))
  } finally {
    scopeSaving.value = false
  }
}

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
  // 滑块初值必须与服务端默认一致，否则显示的条数和实际检索条数会对不上。
  // 取不到（老后端）时 defaultTopK 保持 null，发送时退回显式传滑块值的行为。
  // 与知识库列表并行：两者互不依赖，串起来等于多一次首屏往返。
  const [config, kbList] = await Promise.all([
    chatApi.retrievalConfig().then((r) => r.data).catch(() => null),
    kbApi.list()
  ])
  if (config?.top_k) {
    defaultTopK.value = config.top_k
    topK.value = config.top_k
  }
  const data = kbList.data
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
  if (controlsLocked.value) return
  sourceOpen.value = false
  // 切换知识库会清空视图：在途的会话详情不得再把旧内容放回来
  sessionEpoch.begin()
  resetDocumentScope()
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
  // 流式或范围保存中禁止切换（单选框也置灰了，这里是兜底）；重复点同一个不做事
  if (controlsLocked.value || next === appliedMode) return
  appliedMode = next
  sourceOpen.value = false
  sessionEpoch.begin()
  resetDocumentScope()
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
  if (controlsLocked.value) return
  sourceOpen.value = false
  const loadEpoch = sessionEpoch.begin()
  const { data } = await chatApi.conversation(id)
  // 载入期间用户可能已经发了新问题、新建了会话或点了另一条会话：
  // 这份详情已经过期，不能覆盖已经开始的生成与新的消息列表。
  if (disposed || !sessionEpoch.isCurrent(loadEpoch)) return
  closeSidebar()
  currentConversationId.value = data.id
  // 历史会话以服务端返回的范围为准；旧响应缺失字段时归一化为全库
  const savedScope = conversationScope(data)
  documentScope.value = savedScope.items
  scopeRevision.value = savedScope.revision
  scopeError.value = ''
  scopeDialogOpen.value = false
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
    id: m.id,
    role: m.role,
    status: m.status,
    content: m.content,
    citations: m.citations || undefined
  }))
  await scrollToBottom()
  // 模式或知识库变了，侧栏列表也要跟着换；
  // 不能走 onModeChange，那会把刚打开的消息清掉
  if (switched) await loadConversations()
}

function newConversation() {
  if (controlsLocked.value) return
  closeSidebar()
  sourceOpen.value = false
  // 新建会话后，之前点开的详情即使慢回来也不能再把旧消息填进来
  sessionEpoch.begin()
  resetDocumentScope()
  currentConversationId.value = null
  messages.value = []
  question.value = ''
  errorMessage.value = ''
}

async function removeConversation(id: string) {
  if (controlsLocked.value) return
  try {
    await ElMessageBox.confirm(t('chat.deleteConfirm'), t('common.tip'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel')
    })
  } catch {
    return
  }
  await chatApi.removeConversation(id)
  if (currentConversationId.value === id) newConversation()
  await loadConversations()
  ElMessage.success(t('common.deleted'))
}

function viewSource(citation: Citation) {
  selectedCitation.value = citation
  sourceOpen.value = true
}

function requestStatusLabel(status: string) {
  if (status === 'processing') return t('chat.requestStatus.processing')
  if (status === 'cancelled') return t('chat.requestStatus.cancelled')
  return t('chat.requestStatus.failed')
}

async function stopStreaming() {
  if (stopping.value) return
  const requestId = activeRequestId
  stopping.value = true
  controller?.abort()
  controller = null
  activeRequestId = null
  const last = messages.value[messages.value.length - 1]
  if (last?.streaming) last.streaming = false
  try {
    if (requestId) await chatApi.cancelRequest(requestId)
  } catch {
    // 断连清理与租约超时仍会兜底；上游停止计费取决于模型服务商。
  } finally {
    streaming.value = false
    stopping.value = false
    if (!disposed) await loadConversations()
  }
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
  // 普通对话不需要知识库，只有 RAG 模式才要求先选库；范围保存中避免新旧状态交叉
  if (!text || busy.value || scopeSaving.value || (chatMode.value === 'rag' && !kbId.value)) return

  const mode = chatMode.value
  const hadConversation = !!currentConversationId.value
  // 这一次生成开始后，在途的会话详情载入全部过期：不能让它把刚推入的问答覆盖掉
  sessionEpoch.begin()
  const scopeOptions = buildScopeSendOptions({
    mode,
    items: documentScope.value,
    hasConversation: hadConversation,
    revision: scopeRevision.value
  })
  errorMessage.value = ''
  question.value = ''
  messages.value.push({ id: newMessageId(), role: 'user', content: text, status: 'processing' })
  const userMessage = messages.value[messages.value.length - 1]!
  const requestId = createChatRequestId()
  activeRequestId = requestId

  messages.value.push({ id: newMessageId(), role: 'assistant', content: '', streaming: true })
  // 必须经由数组代理拿到响应式对象：push 进去的普通对象再改属性不会触发视图更新
  const assistant = messages.value[messages.value.length - 1]
  streaming.value = true
  const myController = new AbortController()
  controller = myController
  // 持有信号引用：stopStreaming 会把 controller 置空，但信号上的 aborted 仍然有效
  const signal = myController.signal
  // 最终引用只信任 done，meta 中的旧版检索候选不能当成回答依据。
  let completion: ChatCompletion | undefined
  /** 这条流最终归属的会话；用于阻止过期响应把范围写到别的会话上。 */
  let streamConversationId = currentConversationId.value
  /**
   * 只有真的收到 document_scope / scope_revision 才更新；
   * revision 更小的过期流会被 applyScopeUpdate 丢弃。
   */
  const syncStreamScope = (data: { document_scope?: unknown; scope_revision?: unknown }) => {
    if (mode !== 'rag') return
    if (streamConversationId && currentConversationId.value !== streamConversationId) return
    const updated = applyScopeUpdate({ items: documentScope.value, revision: scopeRevision.value }, data)
    if (!updated) return
    documentScope.value = updated.items
    scopeRevision.value = updated.revision
    scopeError.value = ''
  }
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
        request_id: requestId,
        conversation_id: currentConversationId.value,
        // 滑块没动过就不传，让服务端用 RETRIEVAL_TOP_K 决定（见 utils/retrievalTopK.ts）。
        top_k: resolveTopK({ mode, topK: topK.value, defaultTopK: defaultTopK.value }),
        mode,
        ...scopeOptions
      },
      {
        onMeta(data) {
          if (signal.aborted || disposed || controller !== myController) return
          currentConversationId.value = data.conversation_id
          streamConversationId = data.conversation_id
          if (data.user_message_id) userMessage.id = data.user_message_id
          syncStreamScope(data)
        },
        onDelta(token) {
          if (signal.aborted || disposed || controller !== myController) return
          pending += token
          if (!rafId) rafId = requestAnimationFrame(flush)
        },
        onDone(data) {
          if (signal.aborted || disposed || controller !== myController) return
          completion = data
          if (data.message_id) assistant.id = data.message_id
          userMessage.status = 'completed'
          assistant.streaming = false
          syncStreamScope(data)
        },
        onError(message) {
          completion = undefined
          assistant.streaming = false
          // 已被新请求接管时不要把旧请求的错误顶上去
          if (controller === myController) errorMessage.value = message
        }
      },
      signal
    )
  } catch (err: any) {
    completion = undefined
    assistant.streaming = false
    if (controller === myController && !disposed && err?.name !== 'AbortError') {
      errorMessage.value = err?.message || t('errors.request')
    }
  } finally {
    // 收尾必须把缓冲冲干净，否则最后几个字符会丢
    if (rafId) {
      cancelAnimationFrame(rafId)
      rafId = 0
    }
    flush()

    const aborted = signal.aborted
    userMessage.status = completion ? 'completed' : aborted ? 'cancelled' : 'failed'
    assistant.streaming = false
    // 只收拾自己的状态：用户点停止后可能在这次 finally 之前又发了新请求，
    // 那时 streaming / controller 已经属于新请求，不能被这条旧流重置。
    const isCurrent = controller === myController
    if (isCurrent) {
      streaming.value = false
      controller = null
      activeRequestId = null
    }
    // 先记下是否真的产出了回答：下面的兜底文案不是回答，不能给它配引用来源。
    // 否则模型报错时会同时出现「引用来源 6」和一句"抱歉，模型未返回内容"，
    // 看起来像"明明检索到了却不回答"，自相矛盾。
    const answered = !!assistant.content
    // 一个 token 都没收到时也要有明确交代，不能留空气泡
    if (!assistant.content) {
      assistant.content = aborted
        ? t('chat.emptyReply.stopped')
        : mode === 'rag'
          ? t('chat.emptyReply.rag')
          : t('chat.emptyReply.chat')
    }
    // 收到完整 done 后才展示最终引用；拒答、空回答、报错或中途停止均不挂候选资料。
    assistant.citations = getCompletedCitations(completion, answered, aborted)
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
  const requestId = activeRequestId
  controller?.abort()
  controller = null
  activeRequestId = null
  if (requestId) void chatApi.cancelRequest(requestId).catch(() => {})
})
</script>

<style scoped>
.chat-page { position: relative; isolation: isolate; display: flex; height: calc(100vh - var(--app-header-height, 74px) - var(--main-pad-y, 48px)); height: calc(100dvh - var(--app-header-height, 74px) - var(--main-pad-y, 48px)); min-height: 440px; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius-lg); background: var(--surface); box-shadow: var(--shadow-sm); }
.sidebar { display: flex; flex-direction: column; flex-shrink: 0; width: 246px; min-height: 0; border-right: 1px solid var(--line); background: var(--surface-soft); }
.sidebar-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 25px 21px 0; }
.sidebar-head > div { display: flex; flex-direction: column; gap: 7px; }
.sidebar-head strong { color: var(--text); font-size: 14px; font-weight: 600; }
.sidebar-head > div > span { color: var(--text-3); font-size: 12px; }
.icon-button { display: grid; place-items: center; width: 32px; height: 32px; flex-shrink: 0; padding: 0; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--text-2); cursor: pointer; }
.icon-button:hover { border-color: var(--line-strong); color: var(--brand); background: var(--brand-tint); }
.sidebar-close, .sidebar-toggle { display: none; }
.kb-picker { padding: 24px 18px 20px; border-bottom: 1px solid var(--line); }
.field-label { display: block; margin-bottom: 9px; color: var(--text-3); font-size: 12px; font-weight: 500; letter-spacing: .03em; }
.mode-switch { display: flex; width: 100%; margin-bottom: 21px; padding: 4px; border: 1px solid var(--line); border-radius: 9px; background: var(--brand-soft); }
.mode-switch :deep(.el-radio-button) { flex: 1; min-width: 0; }
.mode-switch :deep(.el-radio-button__inner) { width: 100%; padding: 9px 0; border: 0 !important; border-radius: 0 !important; background: transparent; color: var(--text-3); box-shadow: none !important; font-size: 12px; line-height: 1.35; white-space: normal; }
/* 分段控件：只保留整组外侧的圆角，相邻按钮之间的接缝必须是直角。 */
.mode-switch :deep(.el-radio-button:first-child .el-radio-button__inner) { border-radius: 6px 0 0 6px !important; }
.mode-switch :deep(.el-radio-button:last-child .el-radio-button__inner) { border-radius: 0 6px 6px 0 !important; }
.mode-switch :deep(.el-radio-button.is-active .el-radio-button__inner) { color: var(--brand-dark); background: var(--surface); box-shadow: var(--shadow-sm) !important; font-weight: 550; }
.mode-switch :deep(.el-radio-button.is-disabled .el-radio-button__inner) { color: var(--placeholder); cursor: not-allowed; }
.kb-select { width: 100%; }
.kb-select :deep(.el-select__wrapper) { background: var(--surface); font-size: 12px; }
.kb-count { float: right; margin-left: 12px; color: var(--text-3); font-size: 12px; white-space: nowrap; }
.new-chat { width: 100%; height: 38px; margin-top: 14px; border-color: var(--accent-border); color: var(--brand-dark); background: var(--brand-soft); font-size: 12px; }
.new-chat:not(.is-disabled):hover { border-color: var(--brand); color: var(--brand-dark); background: color-mix(in srgb, var(--brand) 14%, var(--surface)); }
.conv-list { flex: 1; min-height: 0; overflow-y: auto; padding: 21px 11px 15px; }
.conv-title { display: flex; align-items: center; justify-content: space-between; margin-bottom: 13px; padding: 0 10px; color: var(--text-3); font-size: 12px; }
.conv-num { min-width: 19px; padding: 1px 5px; border-radius: 5px; background: var(--brand-soft); color: var(--text-3); text-align: center; font-size: 10px; font-variant-numeric: tabular-nums; }
.conv-item { display: flex; align-items: center; min-width: 0; margin-bottom: 5px; padding-right: 5px; border: 1px solid transparent; border-radius: 8px; transition: background .15s, border-color .15s; }
.conv-item:hover { background: color-mix(in srgb, var(--brand) 7%, var(--surface)); }
.conv-item.active { border-color: var(--accent-border); background: var(--brand-soft); }
.conv-select { display: flex; align-items: center; flex: 1; min-width: 0; gap: 9px; padding: 11px 9px; border: 0; border-radius: 7px; background: transparent; color: var(--text-2); font-size: 12px; text-align: left; cursor: pointer; }
.conv-select:disabled, .conv-del:disabled { cursor: not-allowed; opacity: .5; }
.conv-item.active .conv-select { color: var(--brand-dark); font-weight: 550; }
.conv-icon { flex-shrink: 0; color: var(--text-3); }
.conv-item.active .conv-icon { color: var(--brand); }
.conv-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-del { display: grid; place-items: center; flex-shrink: 0; width: 25px; height: 27px; padding: 0; border: 0; border-radius: 6px; background: transparent; color: var(--text-3); opacity: 0; cursor: pointer; transition: opacity .15s, color .15s; }
.conv-item:hover .conv-del, .conv-item:focus-within .conv-del { opacity: 1; }
.conv-del:hover { background: var(--danger-soft); color: var(--danger); }
.conv-empty { display: flex; flex-direction: column; align-items: center; gap: 9px; padding: 39px 0; color: var(--text-3); font-size: 12px; }
.conv-empty .el-icon { margin-bottom: 3px; color: var(--placeholder); }
.conv-empty small { color: var(--text-3); font-size: 12px; }
.sidebar-note { display: flex; align-items: flex-start; gap: 7px; margin: 0 18px; padding: 15px 0 19px; border-top: 1px solid var(--line); color: var(--text-3); font-size: 12px; line-height: 1.8; }
.sidebar-note .el-icon { flex-shrink: 0; margin-top: 4px; font-size: 12px; }
.chat-main { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; background: var(--surface); }
.chat-head { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 16px; min-height: 77px; padding: 15px 29px; border-bottom: 1px solid var(--line); }
.chat-heading { display: flex; align-items: center; gap: 12px; min-width: 0; }
.chat-heading-icon { display: grid; place-items: center; width: 37px; height: 39px; flex-shrink: 0; border: 1px solid var(--accent-border); border-radius: 10px; color: var(--brand); background: var(--brand-soft); }
.chat-heading-copy { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.chat-title { overflow: hidden; max-width: 360px; margin: 0; color: var(--text); font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.chat-context { overflow: hidden; color: var(--text-3); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.chat-meta { display: flex; align-items: center; flex-shrink: 0; gap: 14px; }
.mode-badge { display: inline-flex; align-items: center; gap: 6px; padding: 6px 9px; border: 1px solid var(--accent-border); border-radius: 6px; background: var(--brand-tint); color: var(--brand-dark); font-size: 12px; white-space: nowrap; }
.mode-badge .el-icon { font-size: 12px; }
.message-count { color: var(--text-3); font-size: 12px; white-space: nowrap; }
.scope-bar { display: flex; flex-direction: column; flex-shrink: 0; gap: 8px; padding: 10px clamp(15px, 3vw, 29px); border-bottom: 1px solid var(--line); background: var(--surface-soft); }
.scope-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; min-width: 0; }
.scope-summary { display: flex; align-items: center; flex-wrap: wrap; flex: 1 1 260px; gap: 6px; min-width: 0; }
.scope-label { display: inline-flex; align-items: center; flex-shrink: 0; gap: 5px; color: var(--text-3); font-size: 11px; white-space: nowrap; }
.scope-all { display: inline-flex; align-items: center; gap: 5px; padding: 3px 8px; border: 1px dashed var(--line-strong); border-radius: 6px; background: var(--surface); color: var(--text-2); font-size: 11px; }
.scope-chip { display: inline-flex; align-items: center; max-width: 240px; border: 1px solid var(--accent-border); border-radius: 6px; background: var(--brand-soft); color: var(--brand-dark); }
.scope-chip-name { overflow: hidden; max-width: 200px; padding: 3px 0 3px 8px; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.scope-chip-remove { display: grid; place-items: center; flex-shrink: 0; width: 24px; height: 24px; padding: 0; border: 0; border-radius: 0 5px 5px 0; background: transparent; color: var(--brand-dark); cursor: pointer; opacity: .65; }
.scope-chip-remove:not(:disabled):hover { background: color-mix(in srgb, var(--brand) 16%, transparent); opacity: 1; }
.scope-chip-remove:disabled { cursor: not-allowed; opacity: .4; }
.scope-chip-remove:focus-visible { outline: 2px solid var(--brand); outline-offset: -2px; }
.scope-actions { display: flex; align-items: center; flex-shrink: 0; gap: 6px; margin-left: auto; }
.scope-actions .el-button { margin-left: 0; font-size: 11px; }
.scope-saving { color: var(--text-3); font-size: 11px; white-space: nowrap; }
.scope-alert { width: 100%; }
.scope-alert :deep(.el-alert__title) { font-size: 12px; line-height: 1.6; }
.welcome-scope { display: inline-flex; align-items: center; gap: 6px; margin-top: 10px !important; padding: 4px 9px; border: 1px solid var(--accent-border); border-radius: 6px; background: var(--brand-soft); color: var(--brand-dark) !important; font-size: 11px !important; }
.citation-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; margin-top: 4px; }
.citation-actions .el-button { margin-left: 0; }
.placeholder { display: flex; flex: 1; flex-direction: column; align-items: center; justify-content: center; gap: 3px; padding: 24px; text-align: center; }
.empty-link { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; text-decoration: none; }
.messages { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 29px clamp(22px, 3vw, 55px) 22px; scroll-padding-bottom: 24px; }
.welcome { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100%; padding: 22px 0 32px; text-align: center; }
.welcome-mark { display: grid; place-items: center; width: 64px; height: 66px; margin-bottom: 21px; border: 1px solid var(--accent-border); border-radius: 19px; background: var(--brand-soft); color: var(--brand); transform: rotate(-6deg); }
.welcome-mark .el-icon { transform: rotate(6deg); }
.welcome-eyebrow { color: var(--text-3); font-size: 12px; letter-spacing: .13em; }
.welcome h2 { margin: 13px 0 12px; color: var(--text); font-size: clamp(22px, 2.1vw, 29px); line-height: 1.5; font-weight: 550; letter-spacing: -.04em; }
.welcome > p { max-width: 480px; margin: 0; color: var(--text-3); font-size: 12px; line-height: 1.9; overflow-wrap: anywhere; }
.suggestions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; width: 100%; max-width: 608px; margin-top: 31px; }
.suggestion { min-width: 0; padding: 17px 15px 16px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-soft); color: var(--text-2); text-align: left; cursor: pointer; transition: background .18s, border-color .18s, transform .18s; }
.suggestion:hover { transform: translateY(-3px); border-color: var(--brand); background: color-mix(in srgb, var(--brand) 6%, var(--surface-soft)); }
.suggestion-top { display: flex; align-items: center; gap: 7px; margin-bottom: 11px; color: var(--brand); }
.suggestion-top > .el-icon:first-child { font-size: 15px; }
.suggestion-top strong { flex: 1; color: var(--text); font-size: 12px; font-weight: 550; }
.suggestion-arrow { color: var(--text-3); font-size: 12px; }
.suggestion-text { display: block; color: var(--text-3); font-size: 12px; line-height: 1.85; overflow-wrap: anywhere; }
.message { display: flex; gap: 12px; max-width: 950px; margin: 0 auto 29px; }
.message.user { flex-direction: row-reverse; }
.user-avatar { flex-shrink: 0; border: 1px solid var(--line-strong); background: var(--slate-soft); color: var(--slate-text); border-radius: 10px; }
.ai-avatar { flex-shrink: 0; border: 1px solid var(--success-border); background: var(--success-soft); color: var(--success); border-radius: 10px; }
.bubble-wrap { max-width: 88%; min-width: 0; flex: 1; }
.message-author { display: flex; align-items: center; gap: 9px; margin: 2px 0 7px; color: var(--text-2); font-size: 12px; font-weight: 550; }
.generating-label { color: var(--text-3); font-size: 10px; font-weight: 400; }
.message.user .bubble-wrap { display: flex; flex: 0 1 auto; flex-direction: column; align-items: flex-end; max-width: 77%; }
.bubble { padding: 3px 0; color: var(--text); font-size: 14px; line-height: 1.85; }
.message.user .bubble { padding: 12px 17px; border: 1px solid var(--accent-border); border-radius: 12px 3px 12px 12px; background: var(--brand-soft); color: var(--text); }
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
.dot { width: 5px; height: 5px; border-radius: 50%; background: var(--brand); animation: bounce 1.2s infinite ease-in-out; }
.dot:nth-child(2) { animation-delay: .15s; }
.dot:nth-child(3) { animation-delay: .3s; }
@keyframes bounce { 0%, 60%, 100% { transform: translateY(0); opacity: .5; } 30% { transform: translateY(-3px); opacity: 1; } }
/* 减少动态效果时动画会停住，三点会定格成三个半透明灰点、看不出"还在处理"。
   改成静态实心点，配合旁边的「生成中」文字，状态依然明确。 */
@media (prefers-reduced-motion: reduce) {
  .dot { animation: none !important; opacity: 1; }
  .markdown-body.streaming :deep(> :last-child):is(p, h1, h2, h3, h4, h5, h6, blockquote, li)::after { animation: none !important; opacity: 1; }
}
.citations { margin-top: 14px; padding: 2px 13px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-soft); }
.citations :deep(.el-collapse) { border: 0; background: transparent; --el-collapse-header-height: 37px; --el-collapse-border-color: transparent; }
.citations :deep(.el-collapse-item__header) { padding: 0; border: 0; background: transparent; color: var(--text-2); font-size: 12px; }
.citations :deep(.el-collapse-item__wrap) { border: 0; background: transparent; }
.citations :deep(.el-collapse-item__content) { padding-bottom: 12px; }
.citation-title { display: flex; align-items: center; gap: 7px; font-weight: 500; }
.citation-title > .el-icon { color: var(--brand); font-size: 13px; }
.citation-count { display: inline-grid; place-items: center; min-width: 18px; height: 17px; margin-left: 2px; padding: 0 5px; border-radius: 4px; background: var(--brand-soft); color: var(--brand-dark); font-size: 10px; line-height: 1; }
.citation-item { margin-bottom: 8px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
.citation-item:last-child { margin-bottom: 0; }
.citation-head { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-bottom: 7px; }
.citation-index { display: grid; place-items: center; width: 18px; height: 18px; flex-shrink: 0; border-radius: 4px; background: var(--brand-soft); color: var(--brand-dark); font-size: 12px; font-weight: 600; }
.citation-name { overflow: hidden; max-width: 100%; color: var(--text); font-size: 12px; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }
.citation-meta, .citation-score { color: var(--text-3); font-size: 12px; white-space: nowrap; }
.citation-score { margin-left: auto; padding: 1px 6px; border-radius: 4px; background: var(--surface-soft); color: var(--text-3); }
.citation-location { margin: 6px 0; color: var(--text-3); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.citation-snippet { display: -webkit-box; overflow: hidden; -webkit-line-clamp: 3; -webkit-box-orient: vertical; color: var(--text-2); font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; }
.error-tip { max-width: 950px; margin: 0 auto 14px; }
.composer { flex-shrink: 0; padding: 8px clamp(22px, 3vw, 55px) 14px; background: var(--surface); }
.composer-box { max-width: 950px; margin: 0 auto; padding: 12px 15px 10px; border: 1px solid var(--line-strong); border-radius: 14px; background: var(--surface); box-shadow: var(--shadow-sm); transition: border-color .18s, box-shadow .18s; }
.composer-box:focus-within { border-color: var(--brand); box-shadow: 0 0 0 3px color-mix(in srgb, var(--brand) 14%, transparent); }
.composer-box :deep(.el-textarea__inner) { padding: 2px 2px 9px; box-shadow: none !important; background: transparent; color: var(--text); font-size: 13px; line-height: 1.8; }
.composer-box :deep(.el-textarea.is-disabled .el-textarea__inner) { background: transparent; color: var(--placeholder); }
.composer-box :deep(.el-textarea__inner::placeholder) { color: var(--placeholder); }
.composer-actions { display: flex; align-items: center; gap: 14px; padding-top: 9px; border-top: 1px solid var(--line); }
.topk { display: flex; align-items: center; gap: 10px; min-width: 0; }
.topk-label { color: var(--text-3); font-size: 12px; white-space: nowrap; }
.topk-slider { width: 75px; height: 28px; --el-slider-button-size: 10px; --el-slider-height: 3px; }
.topk-value { min-width: 17px; color: var(--text-2); font-size: 12px; font-weight: 550; font-variant-numeric: tabular-nums; }
.composer-mode { display: inline-flex; align-items: center; gap: 6px; color: var(--text-3); font-size: 12px; white-space: nowrap; }
.input-hint { margin-left: auto; color: var(--text-3); font-size: 10px; white-space: nowrap; }
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
  .sidebar.is-open { transform: translateX(0); box-shadow: var(--shadow-md); }
  .sidebar-backdrop { position: absolute; z-index: 8; display: block; inset: 0; width: 100%; height: 100%; border: 0; background: var(--overlay); backdrop-filter: blur(2px); }
  .sidebar-close, .sidebar-toggle { display: grid; }
  .sidebar-head { padding-top: 21px; }
  .chat-head { min-height: 71px; padding: 14px 21px; }
  .scope-bar { padding-inline: 21px; }
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
  .scope-bar { padding: 8px 14px; }
  .scope-summary { flex-basis: 100%; }
  .scope-chip { max-width: 100%; }
  .scope-chip-name { max-width: 180px; }
  .scope-actions { width: 100%; margin-left: 0; justify-content: flex-end; }
  .welcome-scope { max-width: 100%; font-size: 10px !important; line-height: 1.6; }
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
