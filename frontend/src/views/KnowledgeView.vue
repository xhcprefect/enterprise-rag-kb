<template>
  <div class="page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">KNOWLEDGE BASE</span>
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
          aria-label="搜索知识库"
        />
        <el-button v-if="userStore.isAdmin" type="primary" @click="openCreate">
          <el-icon><Plus /></el-icon>新建知识库
        </el-button>
      </div>
    </div>

    <!-- 概览：只汇总当前列表的真实数据 -->
    <section class="overview" aria-label="知识库概览">
      <div class="overview-item">
        <span class="overview-icon"><el-icon><Files /></el-icon></span>
        <div class="overview-copy">
          <span class="overview-label">知识库</span>
          <strong class="overview-value">{{ kbs.length }}</strong>
        </div>
      </div>
      <div class="overview-item">
        <span class="overview-icon moss"><el-icon><Document /></el-icon></span>
        <div class="overview-copy">
          <span class="overview-label">文档总数</span>
          <strong class="overview-value">{{ totalDocuments }}</strong>
        </div>
      </div>
      <div class="overview-item">
        <span class="overview-icon warm"><el-icon><Unlock /></el-icon></span>
        <div class="overview-copy">
          <span class="overview-label">公开知识库</span>
          <strong class="overview-value">{{ publicCount }}</strong>
        </div>
      </div>
      <div class="overview-item">
        <span class="overview-icon neutral"><el-icon><Lock /></el-icon></span>
        <div class="overview-copy">
          <span class="overview-label">私有知识库</span>
          <strong class="overview-value">{{ kbs.length - publicCount }}</strong>
        </div>
      </div>
    </section>

    <section class="library" aria-labelledby="library-title">
      <div class="section-head">
        <div>
          <h3 id="library-title">全部知识库</h3>
          <p v-if="keyword">找到 {{ filteredKbs.length }} 个匹配的知识库</p>
          <p v-else>打开知识库管理文档，或直接进入问答</p>
        </div>
        <span class="count-badge">{{ filteredKbs.length }}</span>
      </div>

      <div v-loading="loading" class="kb-area">
        <div v-if="filteredKbs.length" class="kb-grid">
          <article
            v-for="kb in filteredKbs"
            :key="kb.id"
            class="kb-card"
            @click="goDocuments(kb)"
          >
            <header class="kb-top">
              <div class="kb-mark"><el-icon :size="19"><Folder /></el-icon></div>

              <el-dropdown
                v-if="userStore.isAdmin"
                trigger="click"
                @command="(cmd: string) => onCommand(cmd, kb)"
              >
                <button
                  class="kb-more"
                  type="button"
                  aria-haspopup="menu"
                  :aria-label="`知识库操作：${kb.name}`"
                  @click.stop
                >
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

            <h3 class="kb-name">
              <button
                type="button"
                class="kb-name-btn"
                :title="kb.name"
                @click.stop="goDocuments(kb)"
              >
                {{ kb.name }}
              </button>
            </h3>
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
            <span class="kb-new-icon"><el-icon :size="22"><Plus /></el-icon></span>
            <span class="kb-new-title">新建知识库</span>
            <span class="kb-new-hint">为新的资料集合建立独立空间</span>
          </button>
        </div>

        <div v-else-if="!loading" class="library-empty">
          <el-empty
            :description="keyword ? '没有匹配的知识库' : '还没有知识库，先创建一个吧'"
            :image-size="84"
          >
            <el-button v-if="keyword" @click="keyword = ''">清除搜索</el-button>
            <el-button v-else-if="userStore.isAdmin" type="primary" @click="openCreate">
              <el-icon><Plus /></el-icon>新建知识库
            </el-button>
          </el-empty>
        </div>
      </div>
    </section>

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

/* ---------- 概览 ---------- */
.overview {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  overflow: hidden;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.overview-item {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
  padding: 21px 22px;
}

.overview-item + .overview-item {
  border-left: 1px solid var(--line);
}

.overview-icon {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 39px;
  height: 39px;
  border-radius: 11px;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 18px;
}

.overview-icon.moss {
  background: #f2f6ef;
  color: #5c7551;
}

.overview-icon.warm {
  background: #fbf5eb;
  color: var(--warning);
}

.overview-icon.neutral {
  background: #f3f5f3;
  color: #6f7d76;
}

.overview-copy {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.overview-label {
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.5;
  white-space: nowrap;
}

.overview-value {
  color: var(--text);
  font-size: 23px;
  font-weight: 650;
  line-height: 1.25;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}

/* ---------- 列表 ---------- */
.library {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.section-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding: 2px 2px 0;
}

.section-head h3 {
  margin: 0;
  color: var(--text);
  font-size: 15px;
  font-weight: 650;
  line-height: 1.5;
}

.section-head p {
  margin: 4px 0 0;
  color: var(--text-3);
  font-size: 12.5px;
  line-height: 1.6;
}

.kb-area {
  min-height: 220px;
}

.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(285px, 1fr));
  align-items: stretch;
  gap: 16px;
}

.kb-card {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 234px;
  padding: 18px 19px 16px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  cursor: pointer;
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
}

.kb-card:hover {
  transform: translateY(-2px);
  border-color: #cddfd5;
  box-shadow: var(--shadow-md);
}

.kb-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.kb-mark {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border: 1px solid #e0ece5;
  border-radius: 11px;
  background: var(--brand-tint);
  color: var(--brand);
}

.kb-more {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.kb-more:hover {
  background: var(--brand-soft);
  color: var(--brand);
}

.kb-name {
  margin: 0 0 6px;
  min-width: 0;
  font-size: 16px;
  font-weight: 650;
  line-height: 1.45;
  letter-spacing: -0.01em;
}

.kb-name-btn {
  display: block;
  max-width: 100%;
  padding: 0;
  overflow: hidden;
  border: 0;
  background: transparent;
  color: var(--text);
  font: inherit;
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}

.kb-name-btn:hover {
  color: var(--brand);
}

.kb-desc {
  display: -webkit-box;
  height: 42px;
  margin: 0;
  overflow: hidden;
  color: var(--text-3);
  font-size: 13px;
  line-height: 1.62;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
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
  color: var(--text-3);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
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

/* 新建卡片 */
.kb-new {
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1.5px dashed var(--line-strong);
  background: transparent;
  box-shadow: none;
  color: var(--text-3);
  font-family: inherit;
  font-size: 13px;
}

.kb-new:hover {
  border-color: var(--brand);
  background: var(--brand-tint);
  color: var(--brand);
  box-shadow: none;
}

.kb-new-icon {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  margin-bottom: 2px;
  border-radius: 11px;
  background: var(--surface);
  border: 1px solid var(--line);
  color: var(--brand);
}

.kb-new-title {
  color: var(--text-2);
  font-size: 13.5px;
  font-weight: 550;
}

.kb-new:hover .kb-new-title {
  color: var(--brand);
}

.kb-new-hint {
  color: var(--text-3);
  font-size: 12px;
  font-weight: 400;
}

.library-empty {
  padding: 22px 16px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.switch-row {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-2);
  font-size: 13px;
}

@media (max-width: 1020px) {
  .overview {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .overview-item:nth-child(3) {
    border-left: 0;
  }

  .overview-item:nth-child(n + 3) {
    border-top: 1px solid var(--line);
  }
}

@media (max-width: 640px) {
  .search {
    width: 140px;
  }

  .overview-item {
    gap: 10px;
    padding: 15px 14px;
  }

  .overview-icon {
    width: 33px;
    height: 33px;
    font-size: 16px;
  }

  .overview-value {
    font-size: 21px;
  }

  .kb-card {
    min-height: 220px;
    padding: 16px;
  }

  .kb-grid {
    grid-template-columns: 1fr;
  }
}
</style>
