<template>
  <div class="page">
    <div class="page-head">
      <div class="title-group">
        <el-button class="back-btn" circle @click="router.push('/knowledge')">
          <el-icon><ArrowLeft /></el-icon>
        </el-button>
        <div>
          <h2>{{ kb?.name || '文档管理' }}</h2>
          <p>{{ kb?.description || '上传文档后系统会自动解析、切分并向量化入库' }}</p>
        </div>
      </div>
      <div class="page-actions">
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>刷新
        </el-button>
        <el-upload
          v-if="userStore.isAdmin"
          :show-file-list="false"
          :before-upload="beforeUpload"
          :http-request="doUpload"
          accept=".pdf,.docx,.txt,.md,.xlsx,.csv"
        >
          <el-button type="primary" :loading="uploading">
            <el-icon><Upload /></el-icon>上传文档
          </el-button>
        </el-upload>
      </div>
    </div>

    <!-- 支持格式与续传说明 -->
    <div class="format-bar">
      <div class="format-tags">
        <span class="format-label">支持格式</span>
        <span v-for="fmt in FORMATS" :key="fmt" class="format-chip">{{ fmt }}</span>
      </div>
      <span class="format-tip">大文件分片上传，可暂停、可断点续传</span>
    </div>

    <!-- 未完成的上传：刷新/关闭页面后可从这里继续 -->
    <el-alert
      v-if="resumeInfo && !uploadTask"
      type="warning"
      :closable="false"
      show-icon
      class="resume-alert"
    >
      <template #title>
        <div class="resume-title">
          <span>
            检测到未完成的上传「<strong>{{ resumeInfo.filename }}</strong>」（已传
            {{ resumeInfo.percent }}%）。点击右上角「上传文档」重新选择同一文件即可从断点继续。
          </span>
          <el-button link type="danger" size="small" @click="discardResume">放弃该上传</el-button>
        </div>
      </template>
    </el-alert>

    <!-- 上传进度 -->
    <el-card v-if="uploadTask" class="upload-card" shadow="never">
      <div class="upload-head">
        <div class="upload-file">
          <div class="upload-icon" :class="uploadTask.phase">
            <el-icon :size="18">
              <CircleCheck v-if="uploadTask.phase === 'done'" />
              <CircleClose v-else-if="uploadTask.phase === 'failed'" />
              <VideoPause v-else-if="uploadTask.phase === 'paused'" />
              <Loading v-else-if="uploadTask.phase === 'ingesting'" />
              <UploadFilled v-else />
            </el-icon>
          </div>
          <div class="upload-meta">
            <span class="upload-name">{{ uploadTask.filename }}</span>
            <span class="upload-size">{{ formatSize(uploadTask.fileSize) }}</span>
          </div>
        </div>
        <div class="upload-actions">
          <el-button
            v-if="uploadTask.phase === 'uploading'"
            size="small"
            @click="pauseUpload()"
          >
            暂停
          </el-button>
          <el-button
            size="small"
            :type="uploadTask.phase === 'failed' || uploadTask.phase === 'done' ? 'info' : 'danger'"
            :plain="uploadTask.phase === 'failed'"
            @click="cancelUpload"
          >
            {{ cancelLabel }}
          </el-button>
        </div>
      </div>

      <el-progress
        :percentage="uploadTask.percent"
        :status="progressStatus"
        :stroke-width="8"
        class="upload-progress"
      />

      <div class="upload-foot">
        <span class="upload-hint">{{ uploadHint }}</span>
        <span v-if="uploadTask.totalChunks" class="upload-chunks">
          {{ uploadTask.chunksDone }} / {{ uploadTask.totalChunks }} 片
        </span>
      </div>
    </el-card>

    <!-- 文档表 -->
    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          文档列表
          <span class="count-badge">{{ total }}</span>
        </div>
        <el-select
          v-model="statusFilter"
          placeholder="全部状态"
          clearable
          size="small"
          class="status-filter"
          @change="onFilterChange"
        >
          <el-option label="已就绪" value="ready" />
          <el-option label="排队中" value="pending" />
          <el-option label="处理中" value="processing" />
          <el-option label="失败" value="failed" />
          <el-option label="已取消" value="cancelled" />
        </el-select>
      </div>

      <el-table v-loading="loading" :data="docs" class="table">
        <el-table-column label="文件名" min-width="280">
          <template #default="{ row }">
            <div class="file-cell">
              <span class="file-ext" :class="extClass(row.file_ext)">{{ extLabel(row.file_ext) }}</span>
              <span class="file-name" :title="row.filename">{{ row.filename }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="大小" width="100">
          <template #default="{ row }">
            <span class="num">{{ formatSize(row.file_size) }}</span>
          </template>
        </el-table-column>

        <!-- 状态与进度合并：就绪/失败/取消显示标签，排队与处理中直接显示进度条 -->
        <el-table-column label="状态" width="180">
          <template #default="{ row }">
            <el-progress
              v-if="row.status === 'processing' || row.status === 'pending'"
              :percentage="row.progress"
              :stroke-width="6"
              :text-inside="false"
            />
            <el-tooltip
              v-else-if="row.status === 'failed'"
              :content="row.error_message || '处理失败'"
              placement="top"
            >
              <el-tag type="danger" size="small" effect="light">失败</el-tag>
            </el-tooltip>
            <el-tooltip
              v-else-if="row.status === 'cancelled'"
              content="已取消入库，可重新上传或重建索引"
              placement="top"
            >
              <el-tag type="info" size="small" effect="light">已取消</el-tag>
            </el-tooltip>
            <span v-else-if="row.status === 'ready'" class="ready-mark">
              <el-icon><CircleCheckFilled /></el-icon>已就绪
            </span>
            <el-tag v-else :type="statusType(row.status)" size="small" effect="light">
              {{ statusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="chunk_count" label="分块" width="80" align="center">
          <template #default="{ row }">
            <span class="num">{{ row.chunk_count }}</span>
          </template>
        </el-table-column>

        <el-table-column label="字数" width="100" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.char_count.toLocaleString() }}</span>
          </template>
        </el-table-column>

        <el-table-column label="上传时间" width="170">
          <template #default="{ row }">
            <span class="time">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column v-if="userStore.isAdmin" label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'pending' || row.status === 'processing'"
              link
              type="warning"
              size="small"
              @click="cancelIngest(row)"
            >
              取消入库
            </el-button>
            <el-button link type="primary" size="small" @click="reindex(row)">重建索引</el-button>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="还没有文档，点击右上角「上传文档」开始" :image-size="80" />
        </template>
      </el-table>

      <div class="panel-foot">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="load"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type UploadRawFile, type UploadRequestOptions } from 'element-plus'
import {
  docApi,
  kbApi,
  uploadApi,
  type DocumentItem,
  type KnowledgeBase,
  type UploadSession
} from '@/api'
import { useUserStore } from '@/stores/user'
import { hashFile } from '@/utils/upload'

// 上传任务的两段式进度：先传文件（0-100），再等后台入库（0-100）
type UploadPhase = 'uploading' | 'paused' | 'ingesting' | 'done' | 'cancelled' | 'failed'

interface UploadTask {
  docId: string
  filename: string
  fileSize: number
  phase: UploadPhase
  percent: number
  uploading: boolean
  // 分片上传状态（入库阶段后保留，便于展示）
  uploadId: string
  chunkSize: number
  totalChunks: number
  chunksDone: number
}

// 只用于"刷新后提示可续传"的轻量记录，真正的进度以服务端为准
interface ResumeInfo {
  uploadId: string
  filename: string
  fileSize: number
  percent: number
}

const RESUME_KEY = (kbId: string) => `rag_upload_resume_${kbId}`
// 同一时间最多 3 片在飞：提升带宽利用率，又不会压垮后端
const UPLOAD_CONCURRENCY = 3
// 单片失败重试次数（网络抖动）
const CHUNK_RETRIES = 3
const FORMATS = ['PDF', 'Word', 'TXT', 'Markdown', 'Excel', 'CSV']

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const kbId = route.params.kbId as string
const kb = ref<KnowledgeBase | null>(null)
const docs = ref<DocumentItem[]>([])
const loading = ref(false)
const uploading = ref(false)
const hashing = ref(false)
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const statusFilter = ref('')

// 当前上传任务（同一时间只允许一个，避免进度显示混乱）
const uploadTask = ref<UploadTask | null>(null)
const uploadAbort = ref<AbortController | null>(null)
const resumeInfo = ref<ResumeInfo | null>(null)

const uploadHint = computed(() => {
  const t = uploadTask.value
  if (!t) return ''
  if (hashing.value && t.phase === 'uploading') return '正在计算文件摘要，确保续传的是同一文件…'
  if (t.phase === 'uploading') return `正在上传… ${t.percent}%`
  if (t.phase === 'paused') return `已暂停（${t.percent}%），重新选择同一文件即可继续`
  if (t.phase === 'ingesting') return `文件已上传，正在解析并向量化入库… ${t.percent}%`
  if (t.phase === 'done') return '入库完成'
  if (t.phase === 'cancelled') return '已取消'
  return '上传或入库失败，可在列表中查看原因'
})

const progressStatus = computed(() => {
  const phase = uploadTask.value?.phase
  if (phase === 'done') return 'success'
  if (phase === 'cancelled' || phase === 'paused') return 'warning'
  if (phase === 'failed') return 'exception'
  return undefined
})

const cancelLabel = computed(() => {
  const phase = uploadTask.value?.phase
  if (phase === 'done' || phase === 'cancelled' || phase === 'failed') return '关闭'
  if (phase === 'ingesting') return '取消入库'
  return '取消上传'
})

let timer: number | undefined
let disposed = false
let pollInFlight = false
let loadSequence = 0

function statusType(status: string) {
  if (status === 'ready') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'cancelled') return 'info'
  return 'warning'
}

function statusText(status: string) {
  return (
    {
      pending: '排队中',
      processing: '处理中',
      ready: '已就绪',
      failed: '失败',
      cancelled: '已取消'
    }[status] || status
  )
}

const EXT_CLASS: Record<string, string> = {
  '.pdf': 'pdf',
  '.docx': 'doc',
  '.doc': 'doc',
  '.xlsx': 'xls',
  '.xls': 'xls',
  '.csv': 'csv',
  '.md': 'md',
  '.txt': 'txt'
}

function extLabel(ext: string) {
  return ext.replace('.', '').toUpperCase().slice(0, 4)
}

function extClass(ext: string) {
  return EXT_CLASS[ext] || 'txt'
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

async function load() {
  if (disposed) return
  const sequence = ++loadSequence
  loading.value = true
  try {
    const [kbRes, docRes] = await Promise.all([
      kbApi.list(),
      docApi.list(kbId, {
        page: page.value,
        page_size: pageSize.value,
        status: statusFilter.value || undefined
      })
    ])
    if (disposed || sequence !== loadSequence) return
    kb.value = kbRes.data.find((k) => k.id === kbId) || null
    docs.value = docRes.data.items
    total.value = docRes.data.total
    if (docs.value.some((d) => d.status === 'pending' || d.status === 'processing') || uploadTask.value?.phase === 'ingesting') startPolling()
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

function onFilterChange() {
  page.value = 1
  load()
}

function isCancel(err: any) {
  return err?.name === 'CanceledError' || err?.code === 'ERR_CANCELED' || err?.name === 'AbortError'
}

function beforeUpload(file: UploadRawFile) {
  if (uploadTask.value?.phase === 'uploading' || uploadTask.value?.phase === 'ingesting') {
    ElMessage.warning('已有文件正在处理中，请等待完成或先取消')
    return false
  }
  // 若存在未完成会话，必须选同一个文件（服务端按 名称+大小 匹配）
  const pending = resumeInfo.value
  if (pending && (pending.filename !== file.name || pending.fileSize !== file.size)) {
    ElMessage.warning(
      `未完成的上传是「${pending.filename}」，请选择同一文件继续，或先放弃该上传`
    )
    return false
  }
  return true
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function doUpload(options: UploadRequestOptions) {
  uploading.value = true
  const controller = new AbortController()
  uploadAbort.value = controller
  const file = options.file

  const task = reactive<UploadTask>({
    docId: '',
    filename: file.name,
    fileSize: file.size,
    phase: 'uploading',
    percent: 0,
    uploading: true,
    uploadId: '',
    chunkSize: 0,
    totalChunks: 0,
    chunksDone: 0
  })
  uploadTask.value = task

  try {
    // 1) 完整内容摘要识别文件，分块读取，不把整个大文件载入内存。
    hashing.value = true
    const digest = await hashFile(file, controller.signal)
    hashing.value = false
    const { data: session } = await uploadApi.createSession(kbId, file.name, file.size, digest)
    Object.assign(task, {
      uploadId: session.upload_id,
      chunkSize: session.chunk_size,
      totalChunks: session.total_chunks
    })

    const done = new Set<number>(session.received)
    task.chunksDone = done.size
    task.percent = session.file_size
      ? Math.min(99, Math.floor((session.received_bytes / session.file_size) * 100))
      : 0
    saveResume(task)

    // 2) 并发上传剩余分片
    await uploadMissingChunks(file, session, done, task, controller.signal)

    if (controller.signal.aborted) throw new DOMException('aborted', 'AbortError')

    // 3) 合并分片 -> 创建文档
    task.percent = 100
    const { data: completed } = await uploadApi.complete(kbId, session.upload_id)
    clearResume()
    if (disposed) return

    Object.assign(task, {
      docId: completed.document.id,
      phase: 'ingesting',
      percent: completed.document.progress,
      uploading: false
    })
    await load()
    startPolling()
  } catch (err: any) {
    if (isCancel(err)) {
      // 暂停时保留会话与 resumeInfo；取消时已由 cancelUpload 处理
      if (task.phase !== 'paused') uploadTask.value = null
      return
    }
    if (uploadTask.value) uploadTask.value = { ...task, phase: 'failed' }
    // 会话与已传分片仍在服务端：保留 resume 记录，刷新后可继续传（会话失效时 restoreResume 会自动清理）
  } finally {
    hashing.value = false
    uploading.value = false
    uploadAbort.value = null
  }
}

function updateUploadPercent(task: UploadTask, inflightLoaded: number) {
  if (!task.fileSize) return
  const uploaded = task.chunksDone * task.chunkSize + inflightLoaded
  task.percent = Math.min(99, Math.floor((uploaded / task.fileSize) * 100))
}

/** 按并发度上传所有缺失分片；任一片重试耗尽则抛错。 */
async function uploadMissingChunks(
  file: File,
  session: UploadSession,
  done: Set<number>,
  task: UploadTask,
  signal: AbortSignal
) {
  const queue: number[] = []
  for (let i = 0; i < session.total_chunks; i += 1) {
    if (!done.has(i)) queue.push(i)
  }

  // 每片实时进度（只用于让进度条平滑，不计入已完成集合）
  const inflight = new Map<number, number>()

  async function runOne(index: number) {
    let attempt = 0
    while (true) {
      if (signal.aborted) throw new DOMException('aborted', 'AbortError')
      const start = index * session.chunk_size
      const blob = file.slice(start, Math.min(start + session.chunk_size, file.size))
      try {
        await uploadApi.uploadChunk(kbId, session.upload_id, index, blob, signal, (loaded) => {
          inflight.set(index, loaded)
          if (task.phase === 'uploading') {
            let extra = 0
            inflight.forEach((v) => (extra += v))
            updateUploadPercent(task, extra)
          }
        })
        inflight.delete(index)
        done.add(index)
        task.chunksDone = done.size
        updateUploadPercent(task, [...inflight.values()].reduce((a, b) => a + b, 0))
        saveResume(task)
        return
      } catch (err) {
        inflight.delete(index)
        if (isCancel(err) || signal.aborted) throw err
        attempt += 1
        if (attempt >= CHUNK_RETRIES) throw err
        await sleep(500 * attempt) // 退避后重试同一片（服务端幂等）
      }
    }
  }

  let cursor = 0
  const workers = Array.from({ length: Math.min(UPLOAD_CONCURRENCY, queue.length) }, async () => {
    while (cursor < queue.length) {
      if (signal.aborted) throw new DOMException('aborted', 'AbortError')
      const index = queue[cursor]
      cursor += 1
      await runOne(index)
    }
  })
  await Promise.all(workers)
}

function saveResume(task: UploadTask) {
  if (!task.uploadId) return
  const info: ResumeInfo = {
    uploadId: task.uploadId,
    filename: task.filename,
    fileSize: task.fileSize,
    percent: task.percent
  }
  resumeInfo.value = info
  localStorage.setItem(RESUME_KEY(kbId), JSON.stringify(info))
}

function clearResume() {
  resumeInfo.value = null
  localStorage.removeItem(RESUME_KEY(kbId))
}

async function restoreResume() {
  const raw = localStorage.getItem(RESUME_KEY(kbId))
  if (!raw) return
  let saved: ResumeInfo
  try {
    saved = JSON.parse(raw)
  } catch {
    localStorage.removeItem(RESUME_KEY(kbId))
    return
  }
  try {
    // 以服务端为准：会话可能已过期或被别处完成
    const { data } = await uploadApi.getSession(kbId, saved.uploadId)
    if (data.status !== 'uploading') {
      clearResume()
      return
    }
    const percent = data.file_size
      ? Math.floor((data.received_bytes / data.file_size) * 100)
      : 0
    resumeInfo.value = { ...saved, percent }
  } catch {
    clearResume()
  }
}

async function discardResume() {
  const pending = resumeInfo.value
  clearResume()
  if (!pending) return
  try {
    await uploadApi.abort(kbId, pending.uploadId)
    ElMessage.success('已放弃未完成的上传')
  } catch {
    /* 会话可能已过期，忽略 */
  }
}

/** 暂停：中止在途请求但保留会话，稍后重新选同一文件继续。 */
async function pauseUpload(quiet = false) {
  const task = uploadTask.value
  if (!task || task.phase !== 'uploading') return
  // 就地改 phase：doUpload 的 catch 靠同一个对象判断"是被暂停的取消"
  task.phase = 'paused'
  task.uploading = false
  uploadAbort.value?.abort()
  if (!quiet) ElMessage.info('已暂停，可稍后重新选择同一文件继续')

  // 在途分片被掐断，服务端只保留完整落盘的那些：稍等请求收尾后以会话为准校准进度
  if (!task.uploadId) return
  await sleep(1200)
  try {
    const { data } = await uploadApi.getSession(kbId, task.uploadId)
    task.chunksDone = data.received.length
    task.percent = data.file_size
      ? Math.min(99, Math.floor((data.received_bytes / data.file_size) * 100))
      : 0
    saveResume(task)
  } catch {
    /* 断网时保留本地估算值 */
  }
}

// 浮层上的取消：上传阶段放弃会话；入库阶段走后端取消接口
async function cancelUpload() {
  const task = uploadTask.value
  if (!task) return
  if (task.phase === 'done' || task.phase === 'cancelled' || task.phase === 'failed') {
    uploadTask.value = null
    return
  }
  if (task.phase === 'uploading' || task.phase === 'paused') {
    const id = task.uploadId
    uploadAbort.value?.abort()
    uploadTask.value = null
    clearResume()
    if (id) {
      try {
        await uploadApi.abort(kbId, id)
      } catch {
        /* 已过期则无需清理 */
      }
    }
    ElMessage.info('已取消上传')
    return
  }
  if (task.phase === 'ingesting' && task.docId) {
    await cancelIngestById(task.docId, task.filename)
  }
}

async function cancelIngest(row: DocumentItem) {
  await cancelIngestById(row.id, row.filename)
}

async function cancelIngestById(docId: string, filename: string) {
  try {
    await ElMessageBox.confirm(
      `确定取消「${filename}」的入库吗？已写入的向量数据会被清理。`,
      '取消入库',
      { type: 'warning' }
    )
  } catch {
    return // 用户点了取消，不是错误
  }
  await docApi.cancel(kbId, docId)
  ElMessage.success('取消请求已提交，任务将在当前批次结束后停止')
  if (uploadTask.value?.docId === docId) {
    uploadTask.value = { ...uploadTask.value, phase: 'cancelled' }
  }
  await load()
  startPolling()
}

async function reindex(row: DocumentItem) {
  await docApi.reindex(kbId, row.id)
  ElMessage.success('已加入处理队列，请确认 worker 正在运行')
  await load()
  startPolling()
}

async function remove(row: DocumentItem) {
  try {
    await ElMessageBox.confirm(`确定删除「${row.filename}」及其向量数据吗？`, '提示', {
      type: 'warning'
    })
  } catch {
    return
  }
  await docApi.remove(kbId, row.id)
  ElMessage.success('已删除')
  await load()
}

function startPolling() {
  stopPolling()
  if (disposed) return
  timer = window.setInterval(async () => {
    if (pollInFlight || disposed) return
    pollInFlight = true
    const params = { page: page.value, page_size: pageSize.value, status: statusFilter.value || undefined }
    try {
      const { data } = await docApi.list(kbId, params)
      if (disposed) return
      if (params.page !== page.value || params.page_size !== pageSize.value || params.status !== (statusFilter.value || undefined)) return
      docs.value = data.items
      total.value = data.total
      const task = uploadTask.value
      if (task?.phase === 'ingesting' && task.docId) {
        const { data: active } = await docApi.get(kbId, task.docId)
        if (!disposed && uploadTask.value?.docId === active.id) syncUploadTask([active])
      }
      const busy = data.items.some((d) => d.status === 'pending' || d.status === 'processing')
      if (!busy && uploadTask.value?.phase !== 'ingesting') stopPolling()
    } catch (err: any) {
      if (err?.response?.status === 404) uploadTask.value = null
      // 网络抖动下一拍重试，确保同一时间只有一个轮询请求。
    } finally {
      pollInFlight = false
    }
  }, 1500)
}

// 入库阶段：把后端进度回写到浮层，并在终态时收口
function syncUploadTask(items: DocumentItem[]) {
  const task = uploadTask.value
  if (!task || task.phase !== 'ingesting' || !task.docId) return
  const row = items.find((d) => d.id === task.docId)
  if (!row) {
    uploadTask.value = null
    return
  }
  if (row.status === 'ready') {
    uploadTask.value = { ...task, percent: 100, phase: 'done' }
    ElMessage.success('入库完成')
    setTimeout(() => {
      if (uploadTask.value?.phase === 'done') uploadTask.value = null
    }, 2000)
  } else if (row.status === 'failed') {
    uploadTask.value = { ...task, phase: 'failed' }
  } else if (row.status === 'cancelled') {
    uploadTask.value = { ...task, phase: 'cancelled' }
    setTimeout(() => {
      if (uploadTask.value?.phase === 'cancelled') uploadTask.value = null
    }, 2000)
  } else {
    uploadTask.value = { ...task, percent: row.progress }
  }
}

function stopPolling() {
  if (timer) {
    clearInterval(timer)
    timer = undefined
  }
}

onMounted(async () => {
  await load()
  // 恢复"未完成上传"提示：本地只有轻量记录，进度以服务端会话为准
  restoreResume()
  if (docs.value.some((d) => d.status === 'pending' || d.status === 'processing')) {
    startPolling()
  }
})
onBeforeUnmount(() => {
  disposed = true
  stopPolling()
  // 离开页面：在途上传转为暂停并保留会话，回来后（或刷新后）可续传
  if (uploadTask.value?.phase === 'uploading') pauseUpload(true)
})
</script>

<style scoped>
.title-group {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}

.back-btn {
  margin-top: 2px;
  flex-shrink: 0;
}

/* ---------- 支持格式条 ---------- */
.format-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding: 11px 16px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
}

.format-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.format-label {
  font-size: 12.5px;
  color: var(--text-3);
  margin-right: 4px;
}

.format-chip {
  padding: 2px 9px;
  border-radius: 6px;
  background: #f2f5fb;
  border: 1px solid #e6ebf5;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--text-2);
}

.format-tip {
  font-size: 12.5px;
  color: var(--text-3);
}

.resume-alert {
  margin: 0;
}

.resume-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: 100%;
}

/* ---------- 上传进度卡 ---------- */
.upload-card {
  border-radius: var(--radius);
  border-color: #d9e2fb;
  background: linear-gradient(180deg, #fbfcff 0%, #ffffff 100%);
}

.upload-card :deep(.el-card__body) {
  padding: 16px 18px;
}

.upload-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.upload-file {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.upload-icon {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  border-radius: 10px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
}

.upload-icon.done {
  background: #e9f9f1;
  color: var(--success);
}

.upload-icon.failed {
  background: #fdecea;
  color: var(--danger);
}

.upload-icon.paused {
  background: #fff5e8;
  color: var(--warning);
}

.upload-icon.ingesting :deep(.el-icon) {
  animation: spin 1.4s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.upload-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.upload-name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.upload-size {
  font-size: 12px;
  color: var(--text-3);
}

.upload-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.upload-progress {
  margin-bottom: 2px;
}

.upload-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-3);
}

.upload-chunks {
  font-variant-numeric: tabular-nums;
  color: var(--text-3);
}

/* ---------- 表格 ---------- */
.count-badge {
  display: inline-block;
  margin-left: 6px;
  padding: 0 7px;
  border-radius: 999px;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 11.5px;
  font-weight: 600;
  line-height: 18px;
  vertical-align: 1px;
}

.status-filter {
  width: 130px;
}

.file-cell {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.file-ext {
  flex-shrink: 0;
  width: 40px;
  height: 24px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: #fff;
  background: #8d97a9;
}

.file-ext.pdf {
  background: #ef5350;
}

.file-ext.doc {
  background: #3b82f6;
}

.file-ext.xls,
.file-ext.csv {
  background: #12b76a;
}

.file-ext.md {
  background: #6366f1;
}

.file-ext.txt {
  background: #8d97a9;
}

.file-name {
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.num {
  font-variant-numeric: tabular-nums;
  color: var(--text-2);
}

.time {
  font-size: 12.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}

.ready-mark {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12.5px;
  color: var(--el-color-success);
}
</style>
