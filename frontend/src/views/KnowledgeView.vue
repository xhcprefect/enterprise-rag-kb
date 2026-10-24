<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>知识库管理</h2>
        <p>每个知识库对应一套独立的向量集合，文档互不干扰</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="keyword"
          placeholder="搜索知识库"
          clearable
          class="search"
          :prefix-icon="Search"
        />
        <el-button v-if="userStore.isAdmin" type="primary" @click="openCreate">
          <el-icon><Plus /></el-icon>新建知识库
        </el-button>
      </div>
    </div>

    <!-- 概览 -->
    <div class="stats">
      <div class="stat">
        <div class="stat-icon blue"><el-icon><Files /></el-icon></div>
        <div class="stat-body">
          <span class="stat-label">知识库</span>
          <strong class="stat-value">{{ kbs.length }}</strong>
        </div>
      </div>
      <div class="stat">
        <div class="stat-icon green"><el-icon><Document /></el-icon></div>
        <div class="stat-body">
          <span class="stat-label">文档总数</span>
          <strong class="stat-value">{{ totalDocuments }}</strong>
        </div>
      </div>
      <div class="stat">
        <div class="stat-icon amber"><el-icon><Unlock /></el-icon></div>
        <div class="stat-body">
          <span class="stat-label">公开知识库</span>
          <strong class="stat-value">{{ publicCount }}</strong>
        </div>
      </div>
      <div class="stat">
        <div class="stat-icon slate"><el-icon><Lock /></el-icon></div>
        <div class="stat-body">
          <span class="stat-label">私有知识库</span>
          <strong class="stat-value">{{ kbs.length - publicCount }}</strong>
        </div>
      </div>
    </div>

    <div v-loading="loading" class="kb-grid">
      <article
        v-for="kb in filteredKbs"
        :key="kb.id"
        class="kb-card"
        @click="goDocuments(kb)"
      >
        <header class="kb-top">
          <div class="kb-mark"><el-icon :size="20"><Folder /></el-icon></div>

          <el-dropdown
            v-if="userStore.isAdmin"
            trigger="click"
            @command="(cmd: string) => onCommand(cmd, kb)"
          >
            <button class="kb-more" type="button" @click.stop>
              <el-icon><MoreFilled /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="edit">
                  <el-icon><EditPen /></el-icon>编辑
                </el-dropdown-item>
                <el-dropdown-item command="delete" divided>
                  <el-icon><Delete /></el-icon>删除
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </header>

        <h3 class="kb-name">{{ kb.name }}</h3>
        <p class="kb-desc">{{ kb.description || '暂无描述' }}</p>

        <div class="kb-meta">
          <span class="kb-stat">
            <el-icon :size="13"><Document /></el-icon>{{ kb.document_count }} 篇文档
          </span>
          <el-tag size="small" :type="kb.is_public ? 'success' : 'warning'" effect="light" round>
            {{ kb.is_public ? '公开' : '私有' }}
          </el-tag>
        </div>

        <div class="kb-actions">
          <el-button size="small" @click.stop="goDocuments(kb)">管理文档</el-button>
          <el-button size="small" type="primary" plain @click.stop="goChat(kb)">开始问答</el-button>
        </div>
      </article>

      <button v-if="userStore.isAdmin" class="kb-card kb-new" type="button" @click="openCreate">
        <el-icon :size="26"><Plus /></el-icon>
        <span>新建知识库</span>
      </button>
    </div>

    <el-empty
      v-if="!loading && !filteredKbs.length"
      :description="keyword ? '没有匹配的知识库' : '还没有知识库，先创建一个吧'"
    />

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑知识库' : '新建知识库'" width="520px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="例如：产品手册库" maxlength="128" show-word-limit />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            resize="none"
            placeholder="简要说明知识库用途，便于其他成员识别"
            maxlength="2000"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="可见范围">
          <div class="switch-row">
            <el-switch v-model="form.is_public" />
            <span>{{ form.is_public ? '公开：所有登录用户可见' : '私有：仅创建者与管理员可见' }}</span>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { kbApi, type KnowledgeBase } from '@/api'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const kbs = ref<KnowledgeBase[]>([])
const keyword = ref('')
const dialogVisible = ref(false)
const submitting = ref(false)
const editing = ref<KnowledgeBase | null>(null)
const formRef = ref<FormInstance>()

const form = reactive({ name: '', description: '', is_public: true })

const rules: FormRules = {
  name: [{ required: true, message: '请输入知识库名称', trigger: 'blur' }]
}

const filteredKbs = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return kbs.value
  return kbs.value.filter(
    (kb) =>
      kb.name.toLowerCase().includes(kw) || (kb.description || '').toLowerCase().includes(kw)
  )
})

const totalDocuments = computed(() => kbs.value.reduce((sum, kb) => sum + kb.document_count, 0))
const publicCount = computed(() => kbs.value.filter((kb) => kb.is_public).length)

async function load() {
  loading.value = true
  try {
    const { data } = await kbApi.list()
    kbs.value = data
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, { name: '', description: '', is_public: true })
  dialogVisible.value = true
}

function openEdit(kb: KnowledgeBase) {
  editing.value = kb
  Object.assign(form, { name: kb.name, description: kb.description, is_public: kb.is_public })
  dialogVisible.value = true
}

async function submit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    if (editing.value) {
      await kbApi.update(editing.value.id, form)
      ElMessage.success('已更新')
    } else {
      await kbApi.create(form)
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await load()
  } finally {
    submitting.value = false
  }
}

async function onCommand(cmd: string, kb: KnowledgeBase) {
  if (cmd === 'edit') {
    openEdit(kb)
    return
  }
  if (cmd !== 'delete') return

  try {
    await ElMessageBox.confirm(
      `删除知识库「${kb.name}」将同时删除其下所有文档和向量数据，且不可恢复。确定继续？`,
      '危险操作',
      { type: 'warning', confirmButtonText: '确定删除', confirmButtonClass: 'el-button--danger' }
    )
  } catch {
    return
  }
  await kbApi.remove(kb.id)
  ElMessage.success('已删除')
  await load()
}

function goDocuments(kb: KnowledgeBase) {
  router.push(`/documents/${kb.id}`)
}

function goChat(kb: KnowledgeBase) {
  router.push({ path: '/chat', query: { kb: kb.id } })
}

onMounted(load)
</script>

<style scoped>
.search {
  width: 220px;
}

/* ---------- 概览卡片 ---------- */
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 14px;
}

.stat {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px 18px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
}

.stat-icon {
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  border-radius: 11px;
  display: grid;
  place-items: center;
  font-size: 19px;
}

.stat-icon.blue {
  background: #eef1ff;
  color: #4c6ef5;
}

.stat-icon.green {
  background: #eaf6f1;
  color: #067647;
}

.stat-icon.amber {
  background: #fdf3ea;
  color: #b54708;
}

.stat-icon.slate {
  background: #f1f3f8;
  color: #7b8598;
}

.stat-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.stat-label {
  font-size: 12.5px;
  color: var(--text-3);
}

.stat-value {
  font-size: 22px;
  font-weight: 650;
  line-height: 1.15;
  color: var(--text);
  letter-spacing: -0.01em;
}

/* ---------- 知识库卡片 ---------- */
.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
  align-items: start;
  gap: 16px;
  min-height: 120px;
}

.kb-card {
  position: relative;
  display: flex;
  flex-direction: column;
  min-height: 218px;
  padding: 18px 18px 16px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  cursor: pointer;
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
}

.kb-card:hover {
  transform: translateY(-2px);
  border-color: #cfd9f5;
  box-shadow: var(--shadow-md);
}

.kb-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.kb-mark {
  width: 38px;
  height: 38px;
  border-radius: 11px;
  display: grid;
  place-items: center;
  background: linear-gradient(135deg, #eef1ff 0%, #e4e9ff 100%);
  color: var(--brand);
}

.kb-more {
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  display: grid;
  place-items: center;
  transition: background 0.15s ease, color 0.15s ease;
}

.kb-more:hover {
  background: #f2f4f9;
  color: var(--text-2);
}

.kb-name {
  margin: 0 0 6px;
  font-size: 15.5px;
  font-weight: 650;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-desc {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-3);
  height: 42px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.kb-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 14px 0 12px;
}

.kb-stat {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12.5px;
  color: var(--text-3);
}

.kb-actions {
  display: flex;
  gap: 8px;
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

.kb-actions .el-button {
  flex: 1;
  margin: 0;
}

/* 新建卡片：与同一行的知识库卡片等高，底边对齐 */
.kb-new {
  align-self: stretch;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 218px;
  border: 1.5px dashed #d3daea;
  background: transparent;
  box-shadow: none;
  color: var(--text-3);
  font-family: inherit;
  font-size: 13.5px;
}

.kb-new:hover {
  border-color: var(--brand);
  color: var(--brand);
  background: rgba(76, 110, 245, 0.03);
  box-shadow: none;
}

.switch-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--text-3);
}

@media (max-width: 640px) {
  .search {
    width: 140px;
  }
}
</style>
