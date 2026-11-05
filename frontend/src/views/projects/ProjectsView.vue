<template>
  <div class="page projects-page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">{{ t('projects.eyebrow') }}</span>
        <h2>{{ t('projects.title') }}</h2>
        <p>{{ t('projects.subtitle') }}</p>
      </div>
      <div class="page-actions">
        <el-button :loading="sourcesLoading" @click="loadSources">
          <el-icon><Refresh /></el-icon>{{ t('common.refresh') }}
        </el-button>
        <el-button v-if="isAdmin" type="primary" :disabled="!roots.length" @click="openCreate">
          <el-icon><Plus /></el-icon>{{ t('projects.create') }}
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="isAdmin && rootsLoaded && !roots.length"
      class="root-alert"
      type="info"
      :closable="false"
      show-icon
      :title="t('projects.emptyRoots')"
      :description="t('projects.emptyRootsHint')"
    />

    <div class="projects-layout">
      <aside class="source-column" :aria-label="t('projects.listTitle')">
        <div class="column-head">
          <h3>{{ t('projects.listTitle') }}</h3>
          <span class="count-badge">{{ formatNumber(sources.length) }}</span>
        </div>
        <p class="column-hint">{{ t('projects.listHint') }}</p>

        <div v-loading="sourcesLoading" class="source-list">
          <button
            v-for="source in sources"
            :key="source.id"
            type="button"
            class="source-item"
            :class="{ active: source.id === selectedId }"
            :aria-current="source.id === selectedId ? 'true' : undefined"
            :data-source-id="source.id"
            @click="selectSource(source.id)"
          >
            <span class="source-mark"><el-icon :size="17"><FolderOpened /></el-icon></span>
            <span class="source-copy">
              <span class="source-name" :title="source.name">{{ source.name }}</span>
              <span class="source-path">{{ sourcePath(source) }}</span>
            </span>
            <el-tag
              v-if="source.revoked_at"
              size="small"
              type="info"
              effect="light"
              round
            >{{ t('projects.revokedBadge') }}</el-tag>
          </button>

          <div v-if="!sources.length && !sourcesLoading" class="source-empty">
            <span class="source-empty-icon"><el-icon :size="20"><FolderOpened /></el-icon></span>
            <p class="source-empty-title">{{ t('projects.empty') }}</p>
            <p v-if="isAdmin && roots.length" class="source-empty-hint">{{ t('projects.listHint') }}</p>
          </div>
        </div>
      </aside>

      <section v-if="selected" class="source-detail">
        <header class="detail-head">
          <div class="detail-title">
            <h3>{{ selected.name }}</h3>
            <span class="detail-path">{{ sourcePath(selected) }}</span>
          </div>
          <div class="detail-actions">
            <el-tag v-if="selected.revoked_at" type="info" effect="light" round>{{ t('projects.revokedBadge') }}</el-tag>
            <el-tag v-else :type="statusTagType" effect="light" round data-testid="source-status">{{ statusLabel }}</el-tag>
            <el-button
              v-if="isAdmin && !selected.revoked_at"
              size="small"
              type="primary"
              plain
              :loading="syncing"
              :disabled="!!activeSync"
              data-testid="sync-button"
              @click="requestSync"
            >{{ activeSync ? t('projects.activeSync') : t('projects.sync') }}</el-button>
            <el-button
              v-if="isAdmin && activeSync"
              size="small"
              :loading="cancelling"
              data-testid="cancel-sync-button"
              @click="cancelSync"
            >{{ t('projects.cancelSync') }}</el-button>
            <el-button
              v-if="isAdmin && !selected.revoked_at"
              size="small"
              type="danger"
              plain
              :loading="revoking"
              data-testid="revoke-button"
              @click="revoke"
            >{{ t('projects.revoke') }}</el-button>
          </div>
        </header>

        <el-alert v-if="pageError" class="detail-alert" :title="pageError" type="error" :closable="true" show-icon @close="pageError = ''" />
        <el-alert
          v-if="selected.revoked_at"
          class="detail-alert"
          :title="t('projects.errorRevoked')"
          type="warning"
          :closable="false"
          show-icon
        />

        <!-- 快照视图：只有 ready 才能浏览/搜索 -->
        <div class="snapshot-bar" role="group" :aria-label="t('projects.snapshotLabel')">
          <span class="snapshot-label">{{ t('projects.snapshotLabel') }}</span>
          <el-select
            v-if="readySnapshots.length"
            :model-value="view.snapshotId ?? ''"
            size="small"
            class="snapshot-select"
            data-testid="snapshot-select"
            @change="onSnapshotChange"
          >
            <el-option
              v-for="snap in readySnapshots"
              :key="snap.id"
              :value="snap.id"
              :label="snapshotLabel(snap)"
            />
          </el-select>
          <span v-else class="snapshot-none">{{ t('projects.noReady') }}</span>
          <span v-if="activeSync" class="snapshot-progress" data-testid="sync-progress">
            {{ t('projects.progress', { progress: formatNumber(activeSync.progress) + '%' }) }}
          </span>
          <span v-else-if="readySnapshots.length" class="snapshot-hint">{{ t('projects.hasReady') }}</span>
        </div>

        <!-- 快照历史：只有 ready 可点击切换，其余仅展示状态/进度 -->
        <div v-if="snapshots.length" class="snap-history" data-testid="snapshot-history">
          <div class="snap-history-head">{{ t('projects.history') }}</div>
          <ul>
            <li
              v-for="snap in snapshots.slice(0, 6)"
              :key="snap.id"
              :class="{ active: snap.id === view.snapshotId, clickable: snap.status === 'ready' }"
            >
              <el-button
                v-if="snap.status === 'ready'"
                text
                size="small"
                class="snap-row"
                :aria-current="snap.id === view.snapshotId ? 'true' : undefined"
                @click="onSnapshotChange(snap.id)"
              >
                <span class="snap-status is-ready">{{ t('projects.ready') }}</span>
                <span class="snap-time">{{ snapshotTime(snap) }}</span>
                <span class="snap-files">{{ t('projects.fileCount', { count: formatNumber(snap.file_count) }, snap.file_count) }}</span>
              </el-button>
              <span v-else class="snap-row">
                <span class="snap-status" :class="`is-${snap.status}`">{{ snapshotStatusLabel(snap.status) }}</span>
                <span class="snap-time">{{ snapshotTime(snap) }}</span>
                <span v-if="snap.error_code" class="snap-error" :title="snap.error_message || snap.error_code">{{ snap.error_code }}</span>
                <el-progress
                  v-if="snap.status === 'processing' || snap.status === 'pending'"
                  :percentage="Math.max(0, Math.min(100, snap.progress))"
                  :stroke-width="3"
                  :show-text="false"
                  class="snap-progress"
                />
              </span>
            </li>
          </ul>
        </div>

        <!-- 内容搜索 -->
        <div class="search-row">
          <el-input
            v-model="queryText"
            :placeholder="t('projects.searchPlaceholder')"
            :aria-label="t('projects.searchAria')"
            clearable
            :disabled="!view.ready || controlsBusy"
            data-testid="project-search"
            @keydown.enter.prevent="runSearch"
            @clear="clearSearch"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button type="primary" :disabled="!view.ready || controlsBusy" data-testid="project-search-run" @click="runSearch">
            {{ t('projects.searchRun') }}
          </el-button>
        </div>

        <div v-if="!view.ready && !selected.revoked_at" class="no-ready">
          <el-empty :description="t('projects.syncNow')" :image-size="64" />
        </div>

        <template v-else-if="view.ready">
          <div class="view-meta">
            <span v-if="view.kind === 'search'" data-testid="search-total">
              {{ t('projects.searchMatch', { count: formatNumber(view.total) }, view.total) }}
            </span>
            <span v-else data-testid="file-total">
              {{ t('projects.fileCount', { count: formatNumber(view.total) }, view.total) }}
            </span>
            <el-button
              v-if="view.kind === 'search'"
              size="small"
              text
              type="primary"
              @click="returnToFiles"
            >{{ t('projects.backToFiles') }}</el-button>
          </div>

          <div v-loading="view.status === 'loading'" class="result-area">
            <el-alert
              v-if="view.status === 'error'"
              :title="t('projects.loadFailed')"
              type="error"
              :closable="false"
              show-icon
            />

            <!-- 文件分页 -->
            <el-table
              v-else-if="view.kind === 'files' && view.items.length"
              :data="view.items"
              size="small"
              data-testid="project-files"
              @row-click="openFileRow"
            >
              <el-table-column :label="t('projects.colPath')" min-width="240">
                <template #default="{ row }">
                  <button type="button" class="file-link" :title="row.relative_path" @click.stop="openPreview(row.file_version_id)">
                    {{ row.relative_path }}
                  </button>
                  <div v-if="row.symbol" class="file-symbol">
                    {{ t('projects.symbol') }} · {{ row.symbol }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column :label="t('projects.colStatus')" width="110">
                <template #default="{ row }">
                  <el-tag size="small" :type="parseTagType(row.parse_status)" effect="light">
                    {{ parseLabel(row.parse_status) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column :label="t('projects.colSize')" width="100">
                <template #default="{ row }">{{ formatBytes(row.size) }}</template>
              </el-table-column>
              <el-table-column :label="t('projects.colChunks')" width="90">
                <template #default="{ row }">{{ formatNumber(row.chunk_count) }}</template>
              </el-table-column>
            </el-table>

            <!-- 内容搜索结果 -->
            <div v-else-if="view.kind === 'search' && view.items.length" class="search-results" data-testid="project-search-results">
              <article v-for="hit in searchHits" :key="`${hit.file_version_id}:${hit.chunk_index}`" class="search-hit">
                <div class="hit-head">
                  <button type="button" class="file-link" :title="hit.relative_path" @click="openHit(hit)">
                    {{ hit.relative_path }}
                  </button>
                  <span v-if="hit.line_start != null" class="hit-lines">
                    {{ hit.line_start === hit.line_end
                      ? t('projects.line', { line: hit.line_start })
                      : t('projects.lines', { start: hit.line_start, end: hit.line_end ?? hit.line_start }) }}
                  </span>
                  <span v-if="hit.score != null" class="hit-score">{{ formatNumber(Math.round(hit.score * 100)) }}%</span>
                </div>
                <div v-if="hit.symbol" class="hit-symbol">{{ t('projects.symbol') }} · {{ hit.symbol }}</div>
                <pre class="hit-snippet">{{ hit.snippet }}</pre>
              </article>
            </div>

            <el-empty
              v-else-if="view.status === 'ok'"
              :description="view.kind === 'search' ? t('projects.noResult') : t('projects.emptyFiles')"
              :image-size="64"
            />

            <div v-if="view.kind === 'files' && view.total > view.pageSize" class="pager">
              <el-pagination
                layout="prev, pager, next"
                :total="view.total"
                :page-size="view.pageSize"
                :current-page="view.page"
                @current-change="onPageChange"
              />
            </div>
          </div>
        </template>
      </section>

      <section v-else class="source-detail detail-empty">
        <el-empty :description="t('projects.selectSource')" :image-size="80" />
      </section>
    </div>

    <el-dialog v-model="createOpen" :title="t('projects.createTitle')" width="520px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" :validate-on-rule-change="false">
        <el-form-item :label="t('projects.form.name')" prop="name">
          <el-input v-model="form.name" :placeholder="t('projects.form.namePlaceholder')" maxlength="200" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('projects.form.rootKey')" prop="root_key">
          <el-select v-model="form.root_key" :placeholder="t('projects.form.rootKeyPlaceholder')" class="full">
            <el-option v-for="key in roots" :key="key" :label="key" :value="key" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('projects.form.relativeDir')" prop="relative_dir">
          <el-input v-model="form.relative_dir" :placeholder="t('projects.form.relativeDirPlaceholder')" maxlength="1024" />
          <div class="field-help">{{ t('projects.form.relativeDirHelp') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="previewOpen"
      :title="t('projects.previewTitle')"
      size="min(820px, 95vw)"
      data-testid="preview-drawer"
      @closed="onPreviewClosed"
    >
      <div v-if="previewLoading" class="preview-status" role="status">{{ t('common.loading') }}</div>
      <el-alert v-else-if="!preview" :title="t('projects.loadFailed')" type="error" :closable="false" show-icon />
      <template v-else-if="preview">
        <h3 class="preview-path">{{ preview.relative_path }}</h3>
        <p class="preview-meta">
          {{ preview.language || t('projects.unknownLanguage') }}
          <span v-if="preview.truncated"> · {{ t('projects.previewTruncated') }}</span>
        </p>
        <p v-if="preview.parse_status !== 'ready'" class="preview-warning">{{ t('projects.previewFallback') }}</p>
        <div v-for="chunk in preview.chunks" :key="chunk.chunk_index" class="preview-chunk">
          <div class="preview-chunk-head">
            {{ t('chat.citations.chunk', { index: chunk.chunk_index + 1 }) }}
            <span v-if="chunk.line_start != null">
              · {{ chunk.line_start === chunk.line_end
                ? t('projects.line', { line: chunk.line_start })
                : t('projects.lines', { start: chunk.line_start, end: chunk.line_end ?? chunk.line_start }) }}
            </span>
          </div>
          <pre>{{ chunk.content }}</pre>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { projectApi, type ProjectSearchHit, type ProjectSnapshot, type ProjectSource, type ProjectSourcePreview } from '@/api/projects'
import { formatDateTime, formatNumber } from '@/i18n/format'
import { useUserStore } from '@/stores/user'
import {
  ProjectController,
  type ProjectViewState
} from '@/utils/projectController'
import { isValidRelativeDir, normalizeQueryText, PROJECT_PAGE_SIZE_MAX } from '@/utils/projectControls'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const userStore = useUserStore()
const isAdmin = computed(() => userStore.isAdmin)

const sources = ref<ProjectSource[]>([])
const sourcesLoading = ref(false)
const roots = ref<string[]>([])
const rootsLoaded = ref(false)
const selectedId = ref<string | null>(null)
const selected = computed(() => sources.value.find((s) => s.id === selectedId.value) ?? null)

const snapshots = ref<ProjectSnapshot[]>([])
const view = ref<ProjectViewState>({
  sourceId: null,
  snapshotId: null,
  ready: false,
  page: 1,
  pageSize: 20,
  q: '',
  kind: 'files',
  status: 'idle',
  total: 0,
  items: [],
  error: null,
  hasActiveSync: false,
  latestReadySnapshotId: null
})
const preview = ref<ProjectSourcePreview | null>(null)
const previewLoading = ref(false)
const previewOpen = ref(false)

const syncing = ref(false)
const cancelling = ref(false)
const revoking = ref(false)
const pageError = ref('')
const queryText = ref('')

const createOpen = ref(false)
const creating = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({ name: '', root_key: '', relative_dir: '' })

const controlsBusy = computed(() => syncing.value || cancelling.value || revoking.value)

const rules = computed<FormRules>(() => ({
  name: [{ required: true, message: t('projects.form.nameRequired'), trigger: 'blur' }],
  root_key: [{ required: true, message: t('projects.form.rootKeyRequired'), trigger: 'change' }],
  relative_dir: [{
    validator: (_rule: unknown, value: string, callback: (error?: Error) => void) => {
      if (!isValidRelativeDir(value)) callback(new Error(t('projects.form.relativeDirInvalid')))
      else callback()
    },
    trigger: 'blur'
  }]
}))

const readySnapshots = computed(() => snapshots.value.filter((s) => s.status === 'ready'))
const activeSync = computed(() => snapshots.value.find((s) => s.status === 'pending' || s.status === 'processing') ?? null)
const searchHits = computed(() => view.value.items as ProjectSearchHit[])

/** 源级状态：活动同步 > 最新 ready > 最新 failed/cancelled > 未同步。 */
const statusKey = computed<'syncing' | 'ready' | 'failed' | 'cancelled' | 'noSync'>(() => {
  if (selected.value?.revoked_at) return 'noSync'
  if (activeSync.value) return 'syncing'
  if (readySnapshots.value.length) return 'ready'
  const latest = snapshots.value[0]
  if (latest?.status === 'failed') return 'failed'
  if (latest?.status === 'cancelled') return 'cancelled'
  return 'noSync'
})
const statusLabel = computed(() => t(`projects.${statusKey.value}`))
const statusTagType = computed(() => {
  if (statusKey.value === 'ready') return 'success'
  if (statusKey.value === 'syncing') return 'warning'
  if (statusKey.value === 'failed') return 'danger'
  return 'info'
})

function snapshotLabel(snap: ProjectSnapshot) {
  return `${t('projects.fileCount', { count: formatNumber(snap.file_count) }, snap.file_count)} · ${snapshotTime(snap)}`
}

function snapshotTime(snap: ProjectSnapshot) {
  const when = snap.finished_at || snap.started_at || snap.created_at
  return when ? formatDateTime(when) : snap.id.slice(0, 8)
}

function snapshotStatusLabel(status: ProjectSnapshot['status']) {
  const map: Record<ProjectSnapshot['status'], string> = {
    pending: 'pending',
    processing: 'syncing',
    ready: 'ready',
    failed: 'failed',
    cancelled: 'cancelled'
  }
  return t(`projects.${map[status]}`)
}

function sourcePath(source: ProjectSource) {
  return source.relative_dir ? `${source.root_key}/${source.relative_dir}` : source.root_key
}

function parseLabel(status: string) {
  if (status === 'ready') return t('projects.parseReady')
  if (status === 'failed') return t('projects.parseFailed')
  if (status === 'fallback') return t('projects.parseFallback')
  return t('projects.parsePending')
}

function parseTagType(status: string) {
  if (status === 'ready') return 'success'
  if (status === 'failed') return 'danger'
  return 'info'
}

function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 后端错误 → 可展示文案（404/410/409/403 分别对应不可用/撤销/未就绪/无权限）。 */
function errorText(err: any, fallbackKey = 'projects.loadFailed') {
  const status = err?.response?.status
  if (status === 410) return t('projects.errorRevoked')
  if (status === 404) return t('projects.errorNotFound')
  if (status === 409) return t('projects.error409')
  if (status === 403) return t('projects.errorForbidden')
  const message = typeof err?.response?.data?.message === 'string' ? err.response.data.message.trim() : ''
  return message || t(fallbackKey)
}

const controller = new ProjectController({
  listSnapshots: async (sourceId, signal) => (await projectApi.snapshots(sourceId, signal)).data,
  loadFiles: async (sourceId, opts) => {
    const { data } = await projectApi.files(sourceId, {
      snapshot_id: opts.snapshotId ?? undefined,
      offset: (opts.page - 1) * opts.pageSize,
      limit: Math.min(opts.pageSize, PROJECT_PAGE_SIZE_MAX),
      signal: opts.signal
    })
    return data
  },
  searchContent: async (sourceId, opts) => {
    const { data } = await projectApi.search(sourceId, {
      query: opts.query,
      snapshot_id: opts.snapshotId ?? undefined,
      top_k: opts.topK,
      signal: opts.signal
    })
    return data
  },
  loadPreview: async (sourceId, fileVersionId, opts) => {
    const { data } = await projectApi.preview(sourceId, fileVersionId, {
      snapshot_id: opts.snapshotId ?? undefined,
      signal: opts.signal
    })
    return data
  },
  syncSource: async (sourceId) => (await projectApi.sync(sourceId)).data,
  cancelSyncSource: async (sourceId) => (await projectApi.cancelSync(sourceId)).data,
  revokeSource: async (sourceId) => (await projectApi.revoke(sourceId)).data,
  onSnapshots: (_sourceId, snaps) => { snapshots.value = snaps },
  onView: (next) => { view.value = next },
  onPreview: (next) => {
    // 控制器在 closed / 迟到的空结果时回调 null：只清数据，不替用户关抽屉。
    preview.value = next
    if (next) {
      previewOpen.value = true
      previewLoading.value = false
    }
  },
  onPreviewLoading: (loading) => {
    previewLoading.value = loading
    if (loading) previewOpen.value = true
  },
  onPollExpired: () => { pageError.value = t('projects.pollExpired') }
})

async function loadSources() {
  sourcesLoading.value = true
  try {
    const { data } = await projectApi.list()
    sources.value = data
    // 路由参数优先；否则保留已选中的源；否则选第一个未撤销源。
    const wanted = typeof route.params.sourceId === 'string' ? route.params.sourceId : ''
    const exists = (id: string | null) => !!id && data.some((s) => s.id === id)
    if (exists(wanted)) selectedId.value = wanted
    else if (!exists(selectedId.value)) {
      selectedId.value = (data.find((s) => !s.revoked_at) ?? data[0])?.id ?? null
    }
    if (selectedId.value) controller.selectSource(selectedId.value)
  } catch (err) {
    pageError.value = errorText(err)
  } finally {
    sourcesLoading.value = false
  }
}

async function loadRoots() {
  if (!isAdmin.value) return
  try {
    const { data } = await projectApi.roots()
    roots.value = data
  } catch {
    roots.value = []
  } finally {
    rootsLoaded.value = true
  }
}

function selectSource(sourceId: string) {
  if (selectedId.value === sourceId) return
  selectedId.value = sourceId
  pageError.value = ''
  queryText.value = ''
  snapshots.value = []
  if (route.params.sourceId !== sourceId) void router.replace(`/projects/${sourceId}`)
  controller.selectSource(sourceId)
}

function onSnapshotChange(snapshotId: string) {
  queryText.value = ''
  controller.selectSnapshot(snapshotId, true)
}

function onPageChange(page: number) {
  controller.setPage(page)
}

function runSearch() {
  const text = normalizeQueryText(queryText.value)
  if (!text) return
  controller.setQuery(text)
}

function clearSearch() {
  queryText.value = ''
  controller.setQuery('')
}

function returnToFiles() {
  queryText.value = ''
  controller.setQuery('')
}

function openPreview(fileVersionId: string) {
  controller.openPreview(fileVersionId)
}

function openFileRow(row: { file_version_id: string }) {
  openPreview(row.file_version_id)
}

function openHit(hit: ProjectSearchHit) {
  controller.openPreview(hit.file_version_id)
}

function onPreviewClosed() {
  preview.value = null
  previewLoading.value = false
  controller.closePreview()
}

async function requestSync() {
  if (!isAdmin.value || syncing.value) return
  syncing.value = true
  pageError.value = ''
  try {
    const snap = await controller.requestSync()
    if (!snap) {
      ElMessage.error(t('projects.syncFailed', { message: t('projects.errorNotFound') }))
      return
    }
    ElMessage.success(t('projects.syncQueued'))
  } finally {
    syncing.value = false
  }
}

async function cancelSync() {
  if (!isAdmin.value || cancelling.value) return
  const frozen = controller.freezeConfirm('cancel-sync')
  if (!frozen) return
  try {
    await ElMessageBox.confirm(t('projects.cancelSyncConfirm'), t('projects.confirm'), {
      type: 'warning',
      confirmButtonText: t('projects.cancelSync'),
      cancelButtonText: t('common.cancel')
    })
  } catch {
    controller.abortConfirm()
    return
  }
  cancelling.value = true
  try {
    const outcome = await frozen.commit()
    if (outcome === 'stale') return
    ElMessage.success(t('projects.syncCancelled'))
    controller.refresh()
  } catch (err: any) {
    pageError.value = errorText(err, 'projects.cancelFailed')
  } finally {
    cancelling.value = false
  }
}

async function revoke() {
  if (!isAdmin.value || !selected.value || revoking.value) return
  const frozen = controller.freezeConfirm('revoke')
  if (!frozen) return
  try {
    await ElMessageBox.confirm(
      t('projects.revokeConfirm', { name: selected.value.name }),
      t('common.danger'),
      {
        type: 'warning',
        confirmButtonText: t('projects.revokeConfirmButton'),
        cancelButtonText: t('common.cancel'),
        confirmButtonClass: 'el-button--danger'
      }
    )
  } catch {
    controller.abortConfirm()
    return
  }
  revoking.value = true
  try {
    const outcome = await frozen.commit()
    if (outcome === 'stale') return
    ElMessage.success(t('projects.revoked'))
    await loadSources()
  } catch (err: any) {
    pageError.value = errorText(err, 'projects.revokeFailed')
  } finally {
    revoking.value = false
  }
}

function openCreate() {
  Object.assign(form, { name: '', root_key: roots.value[0] ?? '', relative_dir: '' })
  createOpen.value = true
}

async function submitCreate() {
  if (!formRef.value || creating.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  creating.value = true
  try {
    const { data } = await projectApi.create({
      name: form.name.trim(),
      root_key: form.root_key,
      relative_dir: form.relative_dir.trim()
    })
    createOpen.value = false
    ElMessage.success(t('projects.created'))
    await loadSources()
    selectSource(data.id)
  } catch (err: any) {
    ElMessage.error(errorText(err, 'projects.createFailed'))
  } finally {
    creating.value = false
  }
}

// 直接打开 /projects/:id（浏览器前进/后退）时同步选中项。
watch(() => route.params.sourceId, (value) => {
  if (typeof value !== 'string' || !value || value === selectedId.value) return
  if (!sources.value.some((s) => s.id === value)) return
  selectSource(value)
})

onMounted(async () => {
  await Promise.all([loadSources(), loadRoots()])
})
onBeforeUnmount(() => {
  controller.dispose()
})
</script>

<style scoped>
.projects-layout {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 18px;
  align-items: start;
}

.source-column {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 18px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.column-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.column-head h3 {
  margin: 0;
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
}

.count-badge {
  min-width: 20px;
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--brand-soft);
  color: var(--text-3);
  font-size: 11px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.column-hint {
  margin: 0 0 4px;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.6;
}

.source-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 80px;
}

.source-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-width: 0;
  padding: 10px 11px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: transparent;
  color: var(--text-2);
  text-align: left;
  cursor: pointer;
}

.source-item:hover {
  background: color-mix(in srgb, var(--brand) 7%, var(--surface));
}

.source-item.active {
  border-color: var(--accent-border);
  background: var(--brand-soft);
}

.source-mark {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--brand-soft);
  color: var(--brand);
}

.source-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 26px 10px;
  text-align: center;
}

.source-empty-icon {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--brand-soft);
  color: var(--brand);
}

.source-empty-title {
  margin: 0;
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.6;
}

.source-empty-hint {
  margin: 0;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.6;
}

.source-copy {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.source-name {
  overflow: hidden;
  color: var(--text);
  font-size: 13px;
  font-weight: 550;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-path {
  overflow: hidden;
  color: var(--text-3);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-detail {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
  padding: 20px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.detail-empty {
  align-items: center;
  justify-content: center;
  min-height: 320px;
}

.detail-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.detail-title h3 {
  margin: 0 0 4px;
  color: var(--text);
  font-size: 17px;
  font-weight: 620;
  overflow-wrap: anywhere;
}

.detail-path {
  color: var(--text-3);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.detail-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.snapshot-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 11px 13px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface-soft);
}

.snapshot-label {
  color: var(--text-3);
  font-size: 12px;
  font-weight: 500;
}

.snapshot-select {
  min-width: 260px;
}

.snapshot-none,
.snapshot-hint {
  color: var(--text-3);
  font-size: 12px;
}

.snapshot-progress {
  color: var(--warning);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.search-row {
  display: flex;
  gap: 10px;
}

.view-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-3);
  font-size: 12px;
}

.result-area {
  min-height: 120px;
}

.file-link {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--brand);
  font: inherit;
  text-align: left;
  overflow-wrap: anywhere;
  cursor: pointer;
}

.file-link:hover {
  text-decoration: underline;
}

.file-symbol,
.hit-symbol {
  color: var(--text-3);
  font-size: 11px;
}

.search-results {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.search-hit {
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface-soft);
}

.hit-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.hit-lines,
.hit-score {
  color: var(--text-3);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.hit-snippet {
  margin: 8px 0 0;
  color: var(--text);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.field-help {
  margin-top: 4px;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.6;
}

.full {
  width: 100%;
}

.preview-status,
.preview-meta {
  color: var(--text-3);
  font-size: 12px;
}

.preview-path {
  margin: 0 0 6px;
  color: var(--text);
  font-size: 16px;
  overflow-wrap: anywhere;
}

.preview-warning {
  color: var(--warning);
  font-size: 12px;
}

.preview-chunk {
  margin-top: 16px;
  padding: 13px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
}

.preview-chunk-head {
  color: var(--text-3);
  font-size: 11px;
}

.preview-chunk pre {
  margin: 8px 0 0;
  color: var(--text);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 900px) {
  .projects-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
