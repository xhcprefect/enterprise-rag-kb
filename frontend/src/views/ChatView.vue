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
        <div class="conv-title"><span>{{ t('chat.recent.title') }}</span><span class="conv-num">{{ formatNumber(conversationTotal) }}</span></div>
        <div class="conv-search">
          <el-input
            v-model="conversationQuery"
            size="small"
            :placeholder="t('chat.list.searchPlaceholder')"
            :aria-label="t('chat.list.searchAria')"
            :disabled="chatMode === 'rag' && !kbId"
            clearable
            @input="onConversationQueryInput"
            @clear="onConversationQueryInput"
          />
          <span class="conv-total" role="status">
            {{ conversationQuery.trim()
              ? t('chat.list.totalFiltered', { total: formatNumber(conversationTotal) }, conversationTotal)
              : t('chat.list.total', { total: formatNumber(conversationTotal) }, conversationTotal) }}
          </span>
        </div>
        <div v-if="listError" class="conv-error">
          <span>{{ listError }}</span>
          <el-button link size="small" type="primary" @click="loadConversations()">{{ t('chat.list.retry') }}</el-button>
        </div>
        <div v-for="conv in conversations" :key="conv.id" class="conv-item" :class="{ active: conv.id === currentConversationId, pinned: isConversationPinned(conv) }">
          <button class="conv-select" type="button" :disabled="controlsLocked" :aria-current="conv.id === currentConversationId ? 'true' : undefined" :title="conv.title" @click="loadConversation(conv.id)">
            <span v-if="isConversationPinned(conv)" class="conv-pin" aria-hidden="true">★</span><el-icon class="conv-icon" :size="15"><ChatLineRound /></el-icon><span class="conv-name">{{ conv.title }}</span>
          </button>
          <button class="conv-act" type="button" :aria-label="t('chat.list.renameAria', { title: conv.title })" :title="t('chat.list.rename')" @click="renameConversation(conv)"><el-icon :size="13"><EditPen /></el-icon></button>
          <button class="conv-act" type="button" :aria-label="isConversationPinned(conv) ? t('chat.list.unpinAria', { title: conv.title }) : t('chat.list.pinAria', { title: conv.title })" :title="isConversationPinned(conv) ? t('chat.list.unpin') : t('chat.list.pin')" @click="togglePin(conv)"><span class="pin-glyph" aria-hidden="true">{{ isConversationPinned(conv) ? '★' : '☆' }}</span></button>
          <button class="conv-del" type="button" :disabled="controlsLocked" :aria-label="t('chat.recent.deleteAria', { title: conv.title })" @click="removeConversation(conv.id)"><el-icon :size="14"><Delete /></el-icon></button>
        </div>
        <div v-if="listHasMore" class="conv-more">
          <el-button size="small" :loading="loadingMore" @click="loadMoreConversations">{{ loadingMore ? t('chat.list.loadingMore') : t('chat.list.loadMore') }}</el-button>
        </div>
        <div v-if="!conversations.length && !listLoading && !listError" class="conv-empty"><el-icon :size="25"><ChatLineRound /></el-icon><span>{{ t('chat.recent.empty') }}</span><small>{{ t('chat.recent.emptyHint') }}</small></div>
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

      <div v-if="chatMode === 'rag' && kbId && isProjectKb" class="scope-bar" role="group" :aria-label="t('chat.project.label')" data-testid="project-snapshot-bar">
        <div class="scope-row">
          <span class="scope-label">{{ t('chat.project.label') }}</span>
          <el-select
            :model-value="projectSnapshotId"
            :placeholder="t('chat.project.noReady')"
            :aria-label="t('chat.project.choose')"
            :loading="projectSnapshotsLoading"
            :disabled="scopeLocked || projectSnapshotsLoading"
            class="project-snapshot-select"
            @change="changeProjectSnapshot"
          >
            <el-option v-for="snapshot in projectSnapshots" :key="snapshot.id" :value="snapshot.id" :label="t('chat.project.snapshotLabel', { id: snapshot.id })" />
            <el-option v-if="projectSnapshotId && !projectSnapshots.some(s => s.id === projectSnapshotId)" :value="projectSnapshotId" :label="t('chat.project.snapshotLabel', { id: projectSnapshotId })" disabled />
          </el-select>
          <el-button size="small" :disabled="scopeLocked || projectSnapshotsLoading" @click="loadProjectSnapshots">{{ t('chat.list.retry') }}</el-button>
          <span v-if="switchingSnapshot" role="status">{{ t('chat.project.switching') }}</span>
        </div>
        <el-alert v-if="projectSnapshotsError" :title="projectSnapshotsError" type="error" :closable="false" show-icon />
        <span v-else-if="!projectSnapshotsLoading && !projectSnapshotId" role="status">{{ t('chat.project.noReady') }}</span>
      </div>
      <div v-if="chatMode === 'rag' && kbId && !isProjectKb" class="scope-bar" role="group" :aria-label="t('chat.scope.label')">
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

      <ConversationMemoryStatus
        v-if="chatMode === 'chat'"
        :conversation-id="currentConversationId"
        :busy="busy"
        :visible="chatMode === 'chat' && !!currentConversationId"
        :refresh-key="memoryRefreshKey"
      />

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
              <div class="message-author">{{ msg.role === 'user' ? t('chat.author.you') : t('chat.author.assistant') }}<span v-if="msg.streaming" class="generating-label">{{ t(`chat.phases.${msg.phase || 'generating'}`) }}</span><span v-else-if="msg.status && msg.status !== 'completed'" class="generating-label">{{ requestStatusLabel(msg.status) }}</span></div>
              <div class="bubble">
                <div v-if="msg.role === 'assistant' && msg.streaming && !msg.content" class="thinking" role="status"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span class="thinking-text">{{ t(`chat.phases.${msg.phase || 'generating'}`) }}</span></div>
                <div v-else-if="msg.role === 'assistant'" class="markdown-body" :class="{ streaming: msg.streaming }" v-html="renderMarkdown(msg.content, !!msg.streaming)" @click="onMarkdownClick"></div>
                <div v-else class="plain-text">{{ msg.content }}</div>
              </div>
              <div v-if="msg.role === 'assistant' && !msg.streaming && msg.content" class="message-actions">
                <button class="message-action" type="button" :aria-label="t('chat.list.copyAnswerAria')" :title="t('chat.list.copyAnswer')" @click="copyAnswer(msg)"><el-icon :size="13"><DocumentCopy /></el-icon><span>{{ t('chat.list.copyAnswer') }}</span></button>
                <button
                  v-if="canRetryMessage(msg)"
                  class="message-action"
                  type="button"
                  :disabled="busy"
                  :aria-label="t('chat.list.retryAria')"
                  :title="t('chat.list.retryAria')"
                  @click="retryMessage(msg)"
                ><el-icon :size="13"><Refresh /></el-icon><span>{{ t('chat.list.retrySend') }}</span></button>
              </div>
              <el-alert v-if="msg.truncated" class="truncated-tip" :title="t('chat.truncated')" type="warning" :closable="false" show-icon />
              <div v-if="msg.citations?.length" class="citations">
                <el-collapse><el-collapse-item>
                  <template #title><span class="citation-title"><el-icon><DocumentCopy /></el-icon>{{ t('chat.citations.title') }}<span class="citation-count">{{ formatNumber(msg.citations.length) }}</span></span></template>
                    <div v-for="(c, i) in msg.citations" :key="i" class="citation-item">
                    <div class="citation-head"><span class="citation-index">{{ formatNumber(c.source_index ?? (i + 1)) }}</span><span class="citation-name" :title="c.filename || t('chat.citations.untitled')">{{ c.filename || t('chat.citations.untitled') }}</span><span v-if="c.chunk_index !== null" class="citation-meta">{{ t('chat.citations.chunk', { index: formatNumber(c.chunk_index! + 1) }) }}</span><span v-if="c.score !== null" class="citation-score">{{ t('chat.citations.score', { percent: formatNumber(Math.round(c.score! * 100)) }) }}</span></div>
                      <div v-if="sourceLocationLabels(c, t).length" class="citation-location">{{ sourceLocationLabels(c, t).join(' · ') }}</div>
                      <div class="citation-snippet">{{ c.snippet }}</div>
                      <div class="citation-actions">
                        <el-button v-if="c.chunk_index != null && (c.source_kind === 'project' ? c.source_id && c.snapshot_id && c.file_version_id : c.document_id)" link type="primary" @click="viewSource(c)">{{ t('chat.citations.view') }}</el-button>
                        <el-button v-if="chatMode === 'rag' && kbId && !isProjectKb && c.source_kind !== 'project' && c.document_id && !isSoleScopeDocument(c.document_id)" link type="primary" :disabled="scopeLocked" @click="restrictToCitation(c)">{{ t('chat.scope.onlyThis') }}</el-button>
                      </div>
                    </div>
                </el-collapse-item></el-collapse>
              </div>
            </div>
          </div>
          <div v-if="errorMessage" class="error-tip"><el-alert :title="errorMessage" type="error" :closable="false" show-icon /></div>
          <div v-if="recoveryTarget" class="recovery-actions" data-testid="recovery-actions">
            <el-button size="small" :loading="recoveryChecking" :disabled="stopping" data-testid="recovery-check" @click="checkRequestResult">
              {{ recoveryChecking ? t('chat.recovery.checking') : t('chat.recovery.check') }}
            </el-button>
            <el-button size="small" text :disabled="busy" data-testid="recovery-cancel" @click="cancelRecovery">{{ t('chat.recovery.cancel') }}</el-button>
            <span>{{ t('chat.recovery.noResubmit') }}</span>
          </div>
        </div>

        <div class="composer">
          <div class="composer-box">
            <el-input v-model="question" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" resize="none" :aria-label="t('chat.composer.aria')" :placeholder="chatMode === 'rag' ? t('chat.composer.placeholderRag') : t('chat.composer.placeholderChat')" :disabled="busy" @keydown="onComposerKeydown" />
            <div class="composer-actions">
              <div v-if="chatMode === 'rag'" class="topk"><span class="topk-label">{{ t('chat.composer.topk') }}</span><el-slider v-model="topK" :min="1" :max="12" size="small" :aria-label="t('chat.composer.topkAria')" :disabled="busy" class="topk-slider" /><span class="topk-value">{{ formatNumber(topK) }}</span></div>
              <span v-else class="composer-mode"><el-icon><ChatLineRound /></el-icon>{{ t('chat.mode.chat') }}</span>
              <span class="input-hint"><kbd>Enter</kbd> {{ t('chat.composer.hintSend') }} · <kbd>Shift + Enter</kbd> {{ t('chat.composer.hintNewline') }}</span>
              <el-button v-if="busy" class="composer-btn" type="danger" plain :loading="stopping" :disabled="stopping" @click="stopStreaming"><el-icon><VideoPause /></el-icon>{{ stopping ? t('chat.composer.stopping') : t('chat.composer.stop') }}</el-button>
              <el-button
                v-else
                class="composer-btn"
                type="primary"
                :disabled="!question.trim() || scopeLocked || projectSnapshotsLoading || (isProjectKb && !projectSnapshotId)"
                :title="recoveryLocked ? t('chat.recovery.lockedHint') : undefined"
                data-testid="composer-send"
                @click="send()"
              >{{ t('chat.composer.send') }}<el-icon class="send-icon"><Promotion /></el-icon></el-button>
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
import {
  conversationFilterKey,
  createConversationListGuard,
  createConversationPager,
  isConversationPinned,
  mergeConversationPages,
  normalizeConversationQuery,
  normalizeConversationTitle,
  updateLocalConversation
} from '@/utils/conversationList'
import {
  bindRetrySnapshot,
  buildRetryPayload,
  codeTextFromTarget,
  copyPlainText,
  freezeRetryContext,
  freezeRetrySnapshot,
  resolveRetry,
  sameRetryContext,
  type RetryContext,
  type RetrySnapshot
} from '@/utils/chatActions'
import { recoverRequest, recoveredCompletion, type ChatRequestState } from '@/utils/requestRecovery'
import { createStreamingMarkdown } from '@/utils/streamingMarkdown'
import type { StreamPhase } from '@/utils/stream'
import { sourceLocationLabels } from '@/utils/sourceLocation'
import { projectApi, type ProjectSnapshot } from '@/api/projects'
import SourcePreviewDrawer from '@/components/SourcePreviewDrawer.vue'
import DocumentScopeDialog from '@/components/DocumentScopeDialog.vue'
import ConversationMemoryStatus from '@/components/ConversationMemoryStatus.vue'
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
  phase?: StreamPhase
  truncated?: boolean
  status?: 'processing' | 'completed' | 'failed' | 'cancelled'
  /** 失败/取消那一刻冻结的发送快照；只有存在它才可能重新提问。 */
  retry?: RetrySnapshot | null
  /** 这条回答对应的原请求 id，用于重试前只读核验。 */
  requestId?: string | null
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
const streamingMarkdown = createStreamingMarkdown(text => mdPlain.render(text || ''))
let renderCacheChars = 0

function renderMarkdown(text: string, streaming = false) {
  if (streaming) return streamingMarkdown.render(text || '')

  const cached = renderCache.get(text)
  if (cached !== undefined) return cached

  const html = md.render(text || '')
  // Bound both count and retained text/HTML size; long answers must not grow history caches indefinitely.
  const cost = text.length + html.length
  if (renderCache.size >= 100 || renderCacheChars + cost > 2 * 1024 * 1024) {
    renderCache.clear()
    renderCacheChars = 0
  }
  if (cost <= 2 * 1024 * 1024) {
    renderCache.set(text, html)
    renderCacheChars += cost
  }
  return html
}

const route = useRoute()
const { isNarrow, isOpen: sidebarOpen, panelRef: sidebarRef, close: closeSidebar, toggle: toggleSidebar, onKeydown: onSidebarKeydown } = useResponsiveDrawer()
const kbs = ref<KnowledgeBase[]>([])
const kbId = ref<string>('')
const chatMode = ref<ChatMode>('rag')
const conversations = ref<Conversation[]>([])
/** 当前筛选下的会话总数（服务端 total），未必等于已加载条数。 */
const conversationTotal = ref(0)
const conversationQuery = ref('')
const CONVERSATION_PAGE_SIZE = 20
const loadingMore = ref(false)
const listLoading = ref(false)
const listError = ref('')
/**
 * 会话列表自己的代次：与生成/详情用的 sessionEpoch 完全隔离。
 * 加载或翻页列表绝不能打断正在生成的回答，也不能清掉恢复目标。
 */
const listGuard = createConversationListGuard()
/**
 * 已成功取回的页码。不能按去重后的条数反推：跨页出现重复 ID 时，
 * 合并结果会少于服务端 total，按条数推算会反复请求同一页。
 */
const listPager = createConversationPager()
/** 是否还可能有下一页；只由页码/末页判定，不看去重后的条数。 */
const listHasMore = ref(false)
let listController: AbortController | null = null
let queryTimer: ReturnType<typeof setTimeout> | undefined
const currentConversationId = ref<string | null>(null)
const messages = ref<ChatMessage[]>([])
const question = ref('')
/** 滑块初值来自服务端 RETRIEVAL_TOP_K；取不到时为 null，此时退回显式发送滑块值。 */
const defaultTopK = ref<number | null>(null)
const topK = ref(6)
const streaming = ref(false)
const stopping = ref(false)
const busy = computed(() => streaming.value || stopping.value)
/** 摘要状态的外部刷新信号：一轮 chat 完整结束后递增，让摘要条重新读取（在途/滞后才轮询）。 */
const memoryRefreshKey = ref(0)
/** 当前限定的文档范围；空数组表示整个知识库。无会话时这是待发送的草稿。 */
const documentScope = ref<DocumentScopeItem[]>([])
const scopeRevision = ref(0)
/** 知识库问答是否使用项目索引（普通对话不受所选知识库和快照约束）。 */
const isProjectKb = computed(() => {
  if (chatMode.value !== 'rag') return false
  const kb = kbs.value.find((item) => item.id === kbId.value)
  return !!kb && kb.kind === 'project'
})
/**
 * 当前会话/草稿绑定的项目快照。已有项目会话由详情给出且固定，
 * 只能通过 CAS（PATCH project-snapshot）改绑；新会话是待发送的草稿快照。
 */
const projectSnapshotId = ref<string | null>(null)
const projectSnapshots = ref<ProjectSnapshot[]>([])
let snapshotController: AbortController | null = null
const projectSnapshotsLoading = ref(false)
const projectSnapshotsError = ref('')
const switchingSnapshot = ref(false)
const scopeDialogOpen = ref(false)
const scopeSaving = ref(false)
const scopeError = ref('')
/** 生成回答或保存范围期间，禁止切换依据以及会话/知识库/模式。 */
const scopeLocked = computed(() => busy.value || scopeSaving.value || switchingSnapshot.value || recoveryLocked.value)
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
interface RecoveryTarget {
  requestId: string
  conversationId: string | null
  epoch: number
  assistant: ChatMessage
  userMessage: ChatMessage
}
const recoveryTarget = ref<RecoveryTarget | null>(null)
/** 正在只读检查原请求（短时）；此间保持恢复入口可见。 */
const recoveryChecking = ref(false)
/**
 * 恢复态锁：只要原请求还挂着（processing 或正在检查），用户只能"再次检查"或
 * "停止检查"。发送/清空/切换上下文被挡住，避免重复提问，也避免把唯一能定位
 * 原请求的入口清掉。
 */
const recoveryLocked = computed(() => recoveryChecking.value || !!recoveryTarget.value)

function clearRecovery() {
  // 内部清理：入口（发送/新建/切换）已由 recoveryLocked 挡住，
  // 走到这里说明本次动作本就有权替换恢复态。
  recoveryTarget.value = null
  recoveryChecking.value = false
  streamingMarkdown.clear()
}

/** 确认原请求结束后才能释放恢复锁，取消失败仍保留检查入口。 */
async function cancelRecovery() {
  const target = recoveryTarget.value
  if (!target || busy.value || disposed) return
  stopping.value = true
  try {
    await chatApi.cancelRequest(target.requestId)
    const response = await chatApi.requestState(target.requestId)
    if (disposed || recoveryTarget.value !== target || !sessionEpoch.isCurrent(target.epoch)) return
    const state = response.status === 200 ? response.data : null
    if (state?.status === 'completed') restoreCompletedRequest(target, state)
    else if (state?.status === 'failed' || state?.status === 'cancelled') {
      target.userMessage.status = target.assistant.status = state.status
      target.assistant.streaming = false
      recoveryTarget.value = null
      errorMessage.value = t(`chat.recovery.${state.status}`)
    } else errorMessage.value = t('chat.recovery.failedCheck')
  } catch {
    if (!disposed) errorMessage.value = t('chat.recovery.failedCheck')
  } finally {
    stopping.value = false
  }
}

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
  clearRecovery()
  documentScope.value = []
  scopeRevision.value = 0
  scopeDialogOpen.value = false
  scopeSaving.value = false
  scopeError.value = ''
  snapshotController?.abort()
  snapshotController = null
  projectSnapshotId.value = null
  projectSnapshots.value = []
  projectSnapshotsLoading.value = false
  projectSnapshotsError.value = ''
}

async function loadProjectSnapshots() {
  snapshotController?.abort()
  projectSnapshots.value = []
  projectSnapshotsError.value = ''
  if (chatMode.value !== 'rag' || !isProjectKb.value) { projectSnapshotsLoading.value = false; return }
  const request = new AbortController()
  snapshotController = request
  const selectedKb = kbId.value
  const epoch = sessionEpoch.current()
  projectSnapshotsLoading.value = true
  try {
    const { data: sources } = await projectApi.list(request.signal)
    const source = sources.find(item => item.kb_id === selectedKb && !item.revoked_at)
    if (!source) throw new Error(t('chat.project.denied'))
    const { data } = await projectApi.snapshots(source.id, request.signal)
    if (disposed || request.signal.aborted || snapshotController !== request
      || kbId.value !== selectedKb || !sessionEpoch.isCurrent(epoch)) return
    projectSnapshots.value = data.filter(item => item.status === 'ready')
    if (!currentConversationId.value && !projectSnapshotId.value) {
      projectSnapshotId.value = projectSnapshots.value.find(item => item.id === source.active_snapshot_id)?.id
        ?? projectSnapshots.value[0]?.id ?? null
    }
  } catch (err: any) {
    if (!disposed && !request.signal.aborted && snapshotController === request) {
      projectSnapshotsError.value = err?.message || t('chat.project.loadFailed')
    }
  } finally {
    if (snapshotController === request) { snapshotController = null; projectSnapshotsLoading.value = false }
  }
}

async function changeProjectSnapshot(snapshotId: string) {
  if (scopeLocked.value || !isProjectKb.value || snapshotId === projectSnapshotId.value
    || !projectSnapshots.value.some(item => item.id === snapshotId)) return
  const epoch = sessionEpoch.begin()
  const conversationId = currentConversationId.value
  projectSnapshotsError.value = ''
  sourceOpen.value = false
  if (!conversationId) { projectSnapshotId.value = snapshotId; return }
  switchingSnapshot.value = true
  try {
    const { data } = await chatApi.setProjectSnapshot(conversationId, snapshotId, scopeRevision.value)
    if (disposed || !sessionEpoch.isCurrent(epoch) || currentConversationId.value !== conversationId) return
    if (data.project_snapshot_id !== snapshotId || !Number.isInteger(data.scope_revision)
      || data.scope_revision < scopeRevision.value) throw new Error(t('chat.scope.incomplete'))
    projectSnapshotId.value = data.project_snapshot_id
    scopeRevision.value = data.scope_revision
    documentScope.value = []
    ElMessage.success(t('chat.project.switched'))
  } catch (err: any) {
    if (disposed || !sessionEpoch.isCurrent(epoch)) return
    const status = err?.response?.status
    if (status === 409) {
      projectSnapshotsError.value = t('chat.scope.conflict')
      try {
        const { data } = await chatApi.conversation(conversationId)
        if (!disposed && sessionEpoch.isCurrent(epoch) && data.id === currentConversationId.value) {
          projectSnapshotId.value = data.project_snapshot_id ?? null
          scopeRevision.value = conversationScope(data).revision
        }
      } catch { /* 保留旧绑定与冲突提示，不能把请求值当成保存结果。 */ }
    } else projectSnapshotsError.value = status === 410 ? t('chat.project.revoked')
      : status === 404 || status === 403 ? t('chat.project.denied')
      : err?.message || t('chat.scope.saveFailed')
  } finally {
    switchingSnapshot.value = false
  }
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
  if (scopeLocked.value || isProjectKb.value || chatMode.value !== 'rag' || !kbId.value) return
  sessionEpoch.begin()
  clearRecovery()
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
  if (scopeLocked.value || isProjectKb.value) return
  // 较早发出的会话详情也不能在本次范围确认期间改变会话或依据。
  sessionEpoch.begin()
  clearRecovery()
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
  await Promise.all([loadConversations(), loadProjectSnapshots()])
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
  await Promise.all([loadConversations(), loadProjectSnapshots()])
}

/** 当前筛选键：模式 + 知识库 + 去空格搜索词。 */
function currentFilterKey() {
  return conversationFilterKey({
    mode: chatMode.value,
    kbId: chatMode.value === 'rag' ? kbId.value : '',
    query: conversationQuery.value
  })
}

/**
 * 拉取一页会话。
 *
 * - reset=false 时按 ID 合并进现有列表（翻页），reset=true 时整体替换（筛选变化/首屏）。
 * - 响应落地前用列表代次 + 筛选快照双重确认，旧筛选的迟到响应不能覆盖新列表。
 * - 偏移分页在并发写入下不是快照：翻页合并只保证已见 ID 不重复，不承诺期间没有新会话漏掉。
 */
async function loadConversations(options: { reset?: boolean } = {}) {
  if (disposed) return
  const reset = options.reset !== false
  if (chatMode.value === 'rag' && !kbId.value) {
    // RAG 但没选库：没有可列出的会话，也不需要请求
    listController?.abort()
    listController = null
    conversations.value = []
    conversationTotal.value = 0
    listPager.reset()
    listHasMore.value = false
    listError.value = ''
    listLoading.value = false
    loadingMore.value = false
    return
  }
  const filter = currentFilterKey()
  // 页码来自独立跟踪的"已成功页码"，不按去重后的条数推算。
  const page = reset ? 1 : listPager.nextPage()
  // 是否还有下一页只看"页码是否已到服务端末尾"，不看合并去重后的条数。
  if (!reset && listPager.isExhausted()) return

  // 重置时先清空页码：即使这次请求失败，也不会沿用上一个筛选的页码。
  if (reset) listPager.reset()
  listController?.abort()
  const request = listGuard.begin(filter)
  const myController = new AbortController()
  listController = myController
  if (reset) listLoading.value = true
  else loadingMore.value = true
  listError.value = ''

  try {
    const { data } = await chatApi.conversationsPage({
      mode: chatMode.value,
      kbId: chatMode.value === 'rag' ? kbId.value : undefined,
      q: conversationQuery.value.trim() || undefined,
      page,
      pageSize: CONVERSATION_PAGE_SIZE,
      signal: myController.signal
    })
    // 双重确认：请求代次/筛选快照 + 此刻真实的筛选键。
    // 搜索框还在防抖窗口里时 UI 已经变了，但新请求尚未 begin，只比快照会收下旧结果。
    if (disposed || myController.signal.aborted
      || !listGuard.isCurrent(request, currentFilterKey())) return
    // 服务端 total/items 都以当前用户与筛选为范围；条数不能超过 total
    const items = Array.isArray(data?.items) ? data.items : []
    if (reset) {
      conversations.value = items
    } else {
      conversations.value = mergeConversationPages(conversations.value, items)
    }
    listPager.markLoaded(page, items.length, CONVERSATION_PAGE_SIZE)
    conversationTotal.value = typeof data?.total === 'number' && data.total >= 0 ? data.total : conversations.value.length
    // 还有下一页 = 本页取满且总数超过已加载条数；重复 ID 只会让"已加载"偏小，
    // 不会让按钮提前消失，最终由末页判定收敛。
    listHasMore.value = !listPager.isExhausted()
      && items.length >= CONVERSATION_PAGE_SIZE
      && conversations.value.length < conversationTotal.value
  } catch (err: any) {
    if (disposed || myController.signal.aborted || err?.name === 'AbortError'
      || !listGuard.isCurrent(request, currentFilterKey())) return
    listError.value = reset ? t('chat.list.loadFailed') : t('chat.list.loadMoreFailed')
  } finally {
    if (listController === myController) {
      listController = null
      listLoading.value = false
      loadingMore.value = false
    }
  }
}

/** 筛选（模式/知识库/搜索词）变化：回到第一页，但不清掉正在打开的会话与消息。 */
async function reloadConversationsFromFirstPage() {
  await loadConversations({ reset: true })
}

function loadMoreConversations() {
  if (loadingMore.value || listLoading.value || !listHasMore.value) return
  void loadConversations({ reset: false })
}

/** 搜索输入防抖：每敲一个字都发请求会把旧筛选的迟到响应放大成竞态。 */
function onConversationQueryInput() {
  conversationQuery.value = normalizeConversationQuery(conversationQuery.value)
  if (queryTimer) clearTimeout(queryTimer)
  queryTimer = setTimeout(() => {
    queryTimer = undefined
    if (!disposed) void reloadConversationsFromFirstPage()
  }, 220)
}

async function loadConversation(id: string) {
  if (controlsLocked.value) return
  sourceOpen.value = false
  const loadEpoch = sessionEpoch.begin()
  clearRecovery()
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
  // 项目会话的绑定来自服务端，界面只读展示，绝不本地推断。
  projectSnapshotId.value = typeof data.project_snapshot_id === 'string' ? data.project_snapshot_id : null
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
    truncated: m.truncated || m.finish_reason === 'length',
    citations: m.citations || undefined
  }))
  await scrollToBottom()
  // 模式或知识库变了，侧栏列表也要跟着换；
  // 不能走 onModeChange，那会把刚打开的消息清掉
  await Promise.all([switched ? loadConversations() : Promise.resolve(), loadProjectSnapshots()])
}

function newConversation() {
  // 恢复态下也不能直接开新会话：那会丢掉仍在处理里的原请求入口。
  if (controlsLocked.value || recoveryLocked.value) return
  closeSidebar()
  sourceOpen.value = false
  // 新建会话后，之前点开的详情即使慢回来也不能再把旧消息填进来
  sessionEpoch.begin()
  resetDocumentScope()
  currentConversationId.value = null
  messages.value = []
  question.value = ''
  errorMessage.value = ''
  void loadProjectSnapshots()
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

/**
 * 重命名：只改 title。
 * 成功后只更新列表项与（若命中）当前会话的标题数据，不拿响应里的详情覆盖消息。
 */
async function renameConversation(conv: Conversation) {
  let value: string
  try {
    const result = await ElMessageBox.prompt(conv.title, t('chat.list.renameTitle'), {
      confirmButtonText: t('chat.list.save'),
      cancelButtonText: t('chat.list.cancel'),
      inputPlaceholder: t('chat.list.renamePlaceholder'),
      inputValue: conv.title,
      // 与服务端一致：trim 后 1–200 个字符，不合法时不关闭弹窗。
      inputValidator: (raw: string) => {
        const trimmed = typeof raw === 'string' ? raw.trim() : ''
        if (!trimmed) return t('chat.list.renameEmpty')
        if (trimmed.length > 200) return t('chat.list.renameTooLong')
        return true
      }
    })
    value = normalizeConversationTitle(result.value) ?? ''
  } catch {
    return
  }
  if (!value) return
  if (value === conv.title) return
  try {
    const { data } = await chatApi.updateConversation(conv.id, { title: value })
    // 只认 title/pinned_at：即使响应里带了 messages，也不能覆盖正在流式的正文。
    conversations.value = updateLocalConversation(conversations.value, {
      ...conv,
      title: typeof data?.title === 'string' && data.title ? data.title : value,
      pinned_at: data?.pinned_at ?? conv.pinned_at ?? null
    })
    ElMessage.success(t('chat.list.rename'))
    // 服务端按标题做字面搜索：命中旧标题时，改名后这条仍留在"旧结果"里。
    // 回到第一页让筛选结果与服务端一致；只重置列表，不清当前会话与消息。
    await reloadConversationsFromFirstPage()
  } catch {
    // 失败时标题保持原样：不要先把 UI 改了再回滚，避免用户以为已保存。
    ElMessage.error(t('chat.list.renameFailed'))
  }
}

/** 置顶/取消置顶：成功后重置到第一页，避免本地顺序与服务端稳定排序漂移。 */
async function togglePin(conv: Conversation) {
  const pinned = !isConversationPinned(conv)
  try {
    const { data } = await chatApi.updateConversation(conv.id, { pinned })
    conversations.value = updateLocalConversation(conversations.value, {
      ...conv,
      title: typeof data?.title === 'string' && data.title ? data.title : conv.title,
      pinned_at: data?.pinned_at ?? (pinned ? new Date().toISOString() : null)
    })
  } catch {
    ElMessage.error(t('chat.list.pinFailed'))
    return
  }
  // 置顶会改变排序，本地插入的位置不可靠：重新拉第一页让服务端定序。
  await reloadConversationsFromFirstPage()
}

/** 复制回答原文；代码块由 onMarkdownClick 的事件委托处理。 */
async function copyAnswer(msg: ChatMessage) {
  const ok = await copyPlainText(msg.content)
  if (ok) ElMessage.success(t('chat.list.copied'))
  else ElMessage.error(t('chat.list.copyFailed'))
}

/** 事件委托：只读 pre > code 的 textContent，不解析 HTML、不执行任何脚本。 */
async function onMarkdownClick(event: MouseEvent) {
  const text = codeTextFromTarget(event.target)
  if (text === null) return
  const ok = await copyPlainText(text)
  if (ok) ElMessage.success(t('chat.list.copied'))
  else ElMessage.error(t('chat.list.copyFailed'))
}

/** 只有确实失败/被停止、且冻结了发送快照的回答才显示重试入口。 */
function canRetryMessage(msg: ChatMessage): boolean {
  return msg.role === 'assistant'
    && !msg.streaming
    && (msg.status === 'failed' || msg.status === 'cancelled')
    && !!msg.retry
}

function currentRetryContext(): RetryContext {
  return {
    mode: chatMode.value,
    kbId: chatMode.value === 'rag' ? kbId.value || null : null,
    conversationId: currentConversationId.value,
    scopeRevision: scopeRevision.value,
    documentScope: documentScope.value,
    project: isProjectKb.value,
    projectSnapshotId: projectSnapshotId.value
  }
}

/**
 * 重试：先只读核验原请求，确认确实失败/取消且上下文未变，才用新 request_id 重发。
 * 绝不因为 UI 上写着 failed 就直接再发一次模型请求。
 */
async function retryMessage(msg: ChatMessage) {
  if (scopeLocked.value) return
  const snapshot = msg.retry
  if (!snapshot) {
    ElMessage.warning(t('chat.list.retryUnavailable'))
    return
  }
  // 等待期间用户可能新建会话、切换模式/知识库或改动范围：先**拷贝**一份此刻的上下文
  // 与代次。必须拷贝而不是持有同一个响应式数组引用，否则等待期间的原地修改会连
  // "之前"那份一起改掉，守卫等于没做。迟到的 GET 结果既不能改界面，也不能再发请求。
  const contextBefore = freezeRetryContext(currentRetryContext())
  const guardEpoch = sessionEpoch.current()
  let status: 'processing' | 'completed' | 'failed' | 'cancelled' | 'unknown' = 'unknown'
  let original: ChatRequestState | null = null
  if (msg.requestId) {
    try {
      // 只查原请求：GET 失败、403/404/410、断连都拿不到终态证据，一律按 unknown 处理。
      const response = await chatApi.requestState(msg.requestId)
      if (response.status === 200 && response.data) {
        status = response.data.status
        original = response.data
      }
    } catch {
      status = 'unknown'
    }
  }
  if (disposed || busy.value || sessionEpoch.current() !== guardEpoch
    || !sameRetryContext(contextBefore, currentRetryContext())) {
    // 上下文已经变了：这份迟到结果不再代表用户此刻要重发的东西。
    return
  }
  // 决策链与测试共用同一实现：先按原请求状态判定，再做无副作用的绑定校验。
  // GET 给的是会话"当前"范围，不是失败那次的快照，证据不足一律 mismatch。
  const decision = resolveRetry({
    snapshot,
    status,
    server: original
      ? {
        conversationId: original.conversation_id,
        documentScope: original.document_scope,
        scopeRevision: original.scope_revision,
        projectSnapshotId: original.project_snapshot_id
      }
      : null,
    context: contextBefore
  })
  if (decision.action === 'readonly') {
    // 原请求还没结束或已经成功：只给提示/只读结果，绝不重新发送。
    ElMessage.info(decision.reason === 'processing' ? t('chat.recovery.processing') : t('chat.list.retryBlocked'))
    if (decision.reason === 'completed') await reloadConversationsFromFirstPage()
    return
  }
  if (decision.action === 'unknown') {
    ElMessage.warning(t('chat.list.retryUnknown'))
    return
  }
  if (decision.action === 'mismatch') {
    ElMessage.warning(t('chat.list.retryMismatch'))
    return
  }
  if (decision.action !== 'resend') {
    ElMessage.warning(t('chat.list.retryUnavailable'))
    return
  }
  // 校验全部通过后才写界面：新会话补上服务端生成的 ID 与可证一致的范围。
  if (!currentConversationId.value && decision.snapshot.conversationId) {
    currentConversationId.value = decision.snapshot.conversationId
  }
  if (decision.scope) {
    documentScope.value = decision.scope.items
    scopeRevision.value = decision.scope.revision
    scopeError.value = ''
  }
  // 重发：用校验后的冻结字段 + 新 request_id；失败的原回答保留在消息列表里。
  await send(decision.snapshot)
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

async function readOriginalRequest(target: RecoveryTarget, signal: AbortSignal) {
  return recoverRequest(target.requestId, async (id, readSignal) => {
    const response = await chatApi.requestState(id, readSignal)
    return response.status === 200 ? response.data : null
  }, { signal, conversationId: target.conversationId })
}

function restoreCompletedRequest(target: RecoveryTarget, state: ChatRequestState) {
  if (disposed || !sessionEpoch.isCurrent(target.epoch) || state.status !== 'completed' || !state.message) return
  const completion = recoveredCompletion(state)
  currentConversationId.value = state.conversation_id
  target.userMessage.id = state.user_message_id
  target.userMessage.status = 'completed'
  target.assistant.id = state.message.id
  target.assistant.content = state.message.content
  target.assistant.status = 'completed'
  target.assistant.streaming = false
  target.assistant.truncated = state.truncated || state.finish_reason === 'length'
  target.assistant.citations = getCompletedCitations(completion, true, false)
  if (chatMode.value === 'rag') {
    const updated = applyScopeUpdate({ items: documentScope.value, revision: scopeRevision.value }, state)
    if (updated) {
      documentScope.value = updated.items
      scopeRevision.value = updated.revision
      if (isProjectKb.value && typeof state.project_snapshot_id === 'string') projectSnapshotId.value = state.project_snapshot_id
    }
  }
  recoveryTarget.value = null
  errorMessage.value = ''
}

async function checkRequestResult() {
  const target = recoveryTarget.value
  if (!target || busy.value || disposed || !sessionEpoch.isCurrent(target.epoch)) return
  const myController = new AbortController()
  controller = myController
  activeRequestId = target.requestId
  streaming.value = true
  recoveryChecking.value = true
  target.assistant.streaming = true
  target.assistant.phase = 'recovering'
  try {
    const state = await readOriginalRequest(target, myController.signal)
    if (disposed || myController.signal.aborted || controller !== myController || !sessionEpoch.isCurrent(target.epoch)) return
    if (state?.status === 'completed') restoreCompletedRequest(target, state)
    else if (state?.status === 'processing') {
      target.assistant.status = 'processing'
      errorMessage.value = t('chat.recovery.processing')
    } else {
      const status = state?.status === 'cancelled' ? 'cancelled' : 'failed'
      target.userMessage.status = target.assistant.status = status
      errorMessage.value = state?.error_message || t(state ? `chat.recovery.${status}` : 'chat.recovery.unavailable')
      recoveryTarget.value = null
    }
  } catch {
    if (!myController.signal.aborted && controller === myController && !disposed) errorMessage.value = t('chat.recovery.failedCheck')
  } finally {
    target.assistant.streaming = false
    recoveryChecking.value = false
    if (controller === myController) {
      controller = null
      activeRequestId = null
      streaming.value = false
    }
    if (!disposed) await loadConversations()
  }
}

async function stopStreaming() {
  if (stopping.value) return
  if (recoveryTarget.value) {
    controller?.abort()
    controller = null
    activeRequestId = null
    streaming.value = false
    await cancelRecovery()
    return
  }
  const requestId = activeRequestId
  recoveryTarget.value = null
  stopping.value = true
  controller?.abort()
  controller = null
  activeRequestId = null
  const last = messages.value[messages.value.length - 1]
  if (last?.streaming) {
    last.streaming = false
    last.status = 'cancelled'
    const previous = messages.value[messages.value.length - 2]
    if (previous?.role === 'user') previous.status = 'cancelled'
  }
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

async function send(retry?: RetrySnapshot) {
  const text = (retry?.question ?? question.value).trim()
  // 普通对话不需要知识库，只有 RAG 模式才要求先选库；范围保存中避免新旧状态交叉。
  // 恢复态锁定：原请求仍在处理时，新问题会变成同一轮的重复提问。
  if (!text || scopeLocked.value || projectSnapshotsLoading.value
    || (chatMode.value === 'rag' && (!kbId.value || (isProjectKb.value && !projectSnapshotId.value)))) return

  const mode = chatMode.value
  const hadConversation = !!currentConversationId.value
  // 这一次生成开始后，在途的会话详情载入全部过期：不能让它把刚推入的问答覆盖掉
  const sendEpoch = sessionEpoch.begin()
  clearRecovery()
  const project = mode === 'rag' && isProjectKb.value
  const scopeOptions: { document_ids?: string[]; scope_revision?: number; project_snapshot_id?: string } = project
    ? hadConversation ? { scope_revision: scopeRevision.value } : { project_snapshot_id: projectSnapshotId.value! }
    : buildScopeSendOptions({
      mode,
      items: documentScope.value,
      hasConversation: hadConversation,
      revision: scopeRevision.value
    })
  errorMessage.value = ''
  question.value = ''
  /** 本次真正放进请求体的 top_k：重试沿用失败那次的决定，普通发送看用户是否动过滑块。 */
  const sendTopK = retry ? (retry.topK ?? undefined) : resolveTopK({ mode, topK: topK.value, defaultTopK: defaultTopK.value })
  messages.value.push({ id: newMessageId(), role: 'user', content: text, status: 'processing' })
  const userMessage = messages.value[messages.value.length - 1]!
  // 重试必须用新的 request_id：原请求已经失败/取消，复用会被当成同一轮。
  const requestId = createChatRequestId()
  activeRequestId = requestId

  messages.value.push({ id: newMessageId(), role: 'assistant', content: '', streaming: true, phase: mode === 'rag' ? 'retrieving' : 'generating', requestId })
  // 必须经由数组代理拿到响应式对象：push 进去的普通对象再改属性不会触发视图更新
  const assistant = messages.value[messages.value.length - 1]!
  // 冻结这次请求的完整上下文：失败后只有它能用于"原样重试"。
  // 新会话的 ID 与版本由服务端生成，收到可信 meta/原请求状态后再绑定（见 bindRetrySnapshot）。
  let retrySnapshot = freezeRetrySnapshot({
    question: text,
    mode,
    kbId: mode === 'rag' ? kbId.value : null,
    conversationId: currentConversationId.value,
    topK: sendTopK ?? null,
    documentIds: scopeOptions.document_ids ?? null,
    // 草稿明细也要冻结：同 ID 重建后 index_version 变了就不再是同一份依据。
    documentScope: mode === 'rag' && !project && !hadConversation ? documentScope.value : null,
    scopeRevision: scopeOptions.scope_revision ?? null,
    project,
    projectSnapshotId: projectSnapshotId.value
  })
  streaming.value = true
  const myController = new AbortController()
  controller = myController
  // 持有信号引用：stopStreaming 会把 controller 置空，但信号上的 aborted 仍然有效
  const signal = myController.signal
  // 最终引用只信任 done，meta 中的旧版检索候选不能当成回答依据。
  let completion: ChatCompletion | undefined
  let recoveryStatus: ChatRequestState['status'] | undefined
  /** 这条流最终归属的会话；用于阻止过期响应把范围写到别的会话上。 */
  let streamConversationId = currentConversationId.value
  /**
   * 只有真的收到 document_scope / scope_revision 才更新；
   * revision 更小的过期流会被 applyScopeUpdate 丢弃。
   */
  const syncStreamScope = (data: { document_scope?: unknown; scope_revision?: unknown; project_snapshot_id?: unknown }) => {
    if (mode !== 'rag') return
    if (streamConversationId && currentConversationId.value !== streamConversationId) return
    const updated = applyScopeUpdate({ items: documentScope.value, revision: scopeRevision.value }, data)
    if (!updated) return
    documentScope.value = updated.items
    scopeRevision.value = updated.revision
    if (project && typeof data.project_snapshot_id === 'string') projectSnapshotId.value = data.project_snapshot_id
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
      retry
        ? {
          // 重试：字段全部来自冻结快照（planRetry 已确认上下文一致），只换新的 request_id。
          ...buildRetryPayload(retry),
          request_id: requestId
        }
        : {
          // 普通对话不传 kb_id；undefined 在 JSON.stringify 时会被整条丢掉
          kb_id: mode === 'rag' ? kbId.value : undefined,
          question: text,
          request_id: requestId,
          conversation_id: currentConversationId.value,
          // 滑块没动过就不传，让服务端用 RETRIEVAL_TOP_K 决定（见 utils/retrievalTopK.ts）。
          top_k: sendTopK,
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
          // 新会话的真实 ID/版本由服务端在此给出；重试快照据此绑定，绝不靠界面猜。
          retrySnapshot = bindRetrySnapshot(retrySnapshot, {
            conversationId: data.conversation_id,
            scopeRevision: data.scope_revision,
            projectSnapshotId: data.project_snapshot_id
          })
        },
        onStatus(phase) {
          if (signal.aborted || disposed || controller !== myController) return
          assistant.phase = phase
        },
        onDelta(token) {
          if (signal.aborted || disposed || controller !== myController) return
          pending += token
          if (!rafId) rafId = requestAnimationFrame(flush)
        },
        onDone(data) {
          if (signal.aborted || disposed || controller !== myController) return
          completion = data
          assistant.truncated = data.truncated || data.finish_reason === 'length'
          if (data.message_id) assistant.id = data.message_id
          userMessage.status = 'completed'
          assistant.streaming = false
          syncStreamScope(data)
          // 一轮 chat 结束：摘要可能刚发布，提示摘要条重新读取（它也同时监听 busy 结束）。
          memoryRefreshKey.value += 1
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
    if (controller === myController && !disposed && !signal.aborted && err?.name !== 'AbortError') {
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0 }
      flush()
      assistant.phase = 'recovering'
      const target: RecoveryTarget = { requestId, conversationId: streamConversationId, epoch: sendEpoch, assistant, userMessage }
      errorMessage.value = err?.message || t('errors.request')
      try {
        // Query only the original request; never resubmit an interrupted generation.
        const state = await readOriginalRequest(target, signal)
        if (!signal.aborted && !disposed && controller === myController && sessionEpoch.isCurrent(sendEpoch)) {
          if (state?.status === 'completed') {
            restoreCompletedRequest(target, state)
            completion = recoveredCompletion(state)
            recoveryStatus = 'completed'
          } else if (state?.status === 'processing') {
            recoveryStatus = 'processing'
            recoveryTarget.value = target
            errorMessage.value = t('chat.recovery.processing')
          } else {
            recoveryStatus = state?.status === 'cancelled' ? 'cancelled' : 'failed'
            errorMessage.value = state?.error_message || t(state ? `chat.recovery.${recoveryStatus}` : 'chat.recovery.unavailable')
            // 这里**不**用 GET 返回的 scope_revision 改写快照：它给的是会话"当前"范围，
            // 不是失败那轮发送时的依据，采纳它就等于悄悄换了重发依据。
            // meta 到达时快照已由 onMeta 绑定；meta 丢失时保持原草稿快照，
            // 由 retryMessage 用 planRetryBinding 做无副作用的三方校验再决定能否补 ID。
          }
        }
      } catch {
        if (!signal.aborted && !disposed && controller === myController && sessionEpoch.isCurrent(sendEpoch)) {
          recoveryTarget.value = target
          errorMessage.value = t('chat.recovery.failedCheck')
        }
      }
    }
  } finally {
    // 收尾必须把缓冲冲干净，否则最后几个字符会丢
    if (rafId) {
      cancelAnimationFrame(rafId)
      rafId = 0
    }
    flush()

    const aborted = signal.aborted
    userMessage.status = completion ? 'completed' : aborted ? 'cancelled' : recoveryStatus ?? 'failed'
    assistant.status = userMessage.status
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
        : recoveryStatus === 'processing'
          ? t('chat.recovery.processing')
          : mode === 'rag'
          ? t('chat.emptyReply.rag')
          : t('chat.emptyReply.chat')
    }
    // 收到完整 done 后才展示最终引用；拒答、空回答、报错或中途停止均不挂候选资料。
    assistant.citations = getCompletedCitations(completion, answered, aborted)
    // 只有失败/被停止才留下重试入口；成功完成的回答不能"重新提问"。
    assistant.retry = userMessage.status === 'failed' || userMessage.status === 'cancelled' ? retrySnapshot : null
    // 新回答出现后列表顺序会变（updated_at 更新），回到第一页；不清掉当前打开的会话。
    if (!disposed && isCurrent) await loadConversations({ reset: true })
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
  if (queryTimer) clearTimeout(queryTimer)
  queryTimer = undefined
  listController?.abort()
  listController = null
  snapshotController?.abort()
  snapshotController = null
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
.conv-search { display: flex; flex-direction: column; gap: 7px; margin-bottom: 12px; padding: 0 4px; }
.conv-search :deep(.el-input__wrapper) { background: var(--surface); }
.conv-total { padding: 0 6px; color: var(--text-3); font-size: 11px; font-variant-numeric: tabular-nums; }
.conv-error { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 4px 10px; padding: 7px 9px; border: 1px solid color-mix(in srgb, var(--danger) 24%, transparent); border-radius: 7px; background: var(--danger-soft); color: var(--danger); font-size: 11px; line-height: 1.5; }
.conv-more { display: flex; justify-content: center; padding: 9px 0 4px; }
.conv-act { display: grid; place-items: center; flex-shrink: 0; width: 23px; height: 27px; padding: 0; border: 0; border-radius: 6px; background: transparent; color: var(--text-3); opacity: 0; cursor: pointer; transition: opacity .15s, color .15s; }
.conv-item:hover .conv-act, .conv-item:focus-within .conv-act { opacity: 1; }
.conv-act:hover { background: var(--brand-tint); color: var(--brand-dark); }
.pin-glyph { font-size: 12px; line-height: 1; }
.conv-pin { flex-shrink: 0; color: var(--brand); font-size: 10px; line-height: 1; }
.conv-item.pinned .conv-pin, .conv-item.pinned .conv-name { font-weight: 550; }
.message-actions { display: flex; gap: 6px; margin-top: 6px; }
.message-action { display: inline-flex; align-items: center; gap: 4px; padding: 4px 8px; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--text-3); font-size: 11px; cursor: pointer; transition: color .15s, background .15s, border-color .15s; }
.message-action:hover:not(:disabled) { border-color: var(--accent-border); background: var(--brand-tint); color: var(--brand-dark); }
.message-action:disabled { cursor: not-allowed; opacity: .5; }
.markdown-body :deep(pre) { position: relative; }
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
.project-snapshot-select { flex: 1 1 280px; min-width: 0; max-width: 520px; }
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
.truncated-tip { margin-top: 10px; }
.recovery-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; max-width: 950px; margin: 0 auto 14px; color: var(--text-3); font-size: 12px; }
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
