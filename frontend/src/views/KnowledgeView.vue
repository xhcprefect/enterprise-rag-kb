<template>
  <div class="page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">{{ t('knowledge.eyebrow') }}</span>
        <h2>{{ t('knowledge.title') }}</h2>
        <p>{{ t('knowledge.subtitle') }}</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="keyword"
          :placeholder="t('knowledge.searchPlaceholder')"
          clearable
          class="search"
          :prefix-icon="Search"
          :aria-label="t('knowledge.searchAria')"
        />
        <el-button v-if="userStore.isAdmin" type="primary" @click="openCreate">
          <el-icon><Plus /></el-icon>{{ t('knowledge.create') }}
        </el-button>
      </div>
    </div>

    <!-- 概览：只汇总当前列表的真实数据 -->
    <section class="overview" :aria-label="t('knowledge.overviewAria')">
      <div class="overview-item">
        <span class="overview-icon"><el-icon><Files /></el-icon></span>
        <div class="overview-copy">
          <span class="overview-label">{{ t('knowledge.overview.bases') }}</span>
          <strong class="overview-value">{{ formatNumber(kbs.length) }}</strong>
        </div>
      </div>
      <div class="overview-item">
        <span class="overview-icon moss"><el-icon><Document /></el-icon></span>
        <div class="overview-copy">
          <span class="overview-label">{{ t('knowledge.overview.documents') }}</span>
          <strong class="overview-value">{{ formatNumber(totalDocuments) }}</strong>
        </div>
      </div>
      <div class="overview-item">
        <span class="overview-icon warm"><el-icon><Unlock /></el-icon></span>
        <div class="overview-copy">
          <span class="overview-label">{{ t('knowledge.overview.publicBases') }}</span>
          <strong class="overview-value">{{ formatNumber(publicCount) }}</strong>
        </div>
      </div>
      <div class="overview-item">
        <span class="overview-icon neutral"><el-icon><Lock /></el-icon></span>
        <div class="overview-copy">
          <span class="overview-label">{{ t('knowledge.overview.privateBases') }}</span>
          <strong class="overview-value">{{ formatNumber(kbs.length - publicCount) }}</strong>
        </div>
      </div>
    </section>

    <section class="library" aria-labelledby="library-title">
      <div class="section-head">
        <div>
          <h3 id="library-title">{{ t('knowledge.listTitle') }}</h3>
          <p v-if="keyword">
            {{ t('knowledge.listMatch', { count: formatNumber(filteredKbs.length) }, filteredKbs.length) }}
          </p>
          <p v-else>{{ t('knowledge.listHint') }}</p>
        </div>
        <span class="count-badge">{{ formatNumber(filteredKbs.length) }}</span>
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
                  :aria-label="t('knowledge.actionsAria', { name: kb.name })"
                  @click.stop
                >
                  <el-icon><MoreFilled /></el-icon>
                </button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="edit">
                      <el-icon><EditPen /></el-icon>{{ t('common.edit') }}
                    </el-dropdown-item>
                    <el-dropdown-item command="delete" divided>
                      <el-icon><Delete /></el-icon>{{ t('common.delete') }}
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
            <p class="kb-desc">{{ kb.description || t('common.noDescription') }}</p>

            <div class="kb-meta">
              <span class="kb-stat">
                <el-icon :size="13"><Document /></el-icon>
                <span>
                  {{ t('knowledge.documentCount', { count: formatNumber(kb.document_count) }, kb.document_count) }}
                </span>
              </span>
              <el-tag size="small" :type="kb.is_public ? 'success' : 'warning'" effect="light" round>
                {{ kb.is_public ? t('common.public') : t('common.private') }}
              </el-tag>
            </div>

            <div class="kb-actions">
              <el-button size="small" @click.stop="goDocuments(kb)">{{ t('knowledge.manage') }}</el-button>
              <el-button size="small" type="primary" plain @click.stop="goChat(kb)">
                {{ t('knowledge.chat') }}
              </el-button>
            </div>
          </article>

          <button v-if="userStore.isAdmin" class="kb-card kb-new" type="button" @click="openCreate">
            <span class="kb-new-icon"><el-icon :size="22"><Plus /></el-icon></span>
            <span class="kb-new-title">{{ t('knowledge.newTitle') }}</span>
            <span class="kb-new-hint">{{ t('knowledge.newHint') }}</span>
          </button>
        </div>

        <div v-else-if="!loading" class="library-empty">
          <el-empty
            :description="keyword ? t('knowledge.emptyMatch') : t('knowledge.empty')"
            :image-size="84"
          >
            <el-button v-if="keyword" @click="keyword = ''">{{ t('knowledge.clearSearch') }}</el-button>
            <el-button v-else-if="userStore.isAdmin" type="primary" @click="openCreate">
              <el-icon><Plus /></el-icon>{{ t('knowledge.create') }}
            </el-button>
          </el-empty>
        </div>
      </div>
    </section>

    <el-dialog
      v-model="dialogVisible"
      :title="editing ? t('knowledge.dialogEditTitle') : t('knowledge.dialogCreateTitle')"
      width="520px"
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        :validate-on-rule-change="false"
      >
        <el-form-item :label="t('knowledge.form.name')" prop="name">
          <el-input
            v-model="form.name"
            :placeholder="t('knowledge.form.namePlaceholder')"
            maxlength="128"
            show-word-limit
          />
        </el-form-item>
        <el-form-item :label="t('knowledge.form.description')">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            resize="none"
            :placeholder="t('knowledge.form.descriptionPlaceholder')"
            maxlength="2000"
            show-word-limit
          />
        </el-form-item>
        <el-form-item :label="t('knowledge.form.visibility')">
          <div class="switch-row">
            <el-switch v-model="form.is_public" />
            <span>
              {{ form.is_public ? t('knowledge.form.publicHint') : t('knowledge.form.privateHint') }}
            </span>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { kbApi, type KnowledgeBase } from '@/api'
import { useUserStore } from '@/stores/user'
import { formatNumber } from '@/i18n/format'

const router = useRouter()
const userStore = useUserStore()
const { t, locale } = useI18n()

const loading = ref(false)
const kbs = ref<KnowledgeBase[]>([])
const keyword = ref('')
const dialogVisible = ref(false)
const submitting = ref(false)
const editing = ref<KnowledgeBase | null>(null)
const formRef = ref<FormInstance>()

const form = reactive({ name: '', description: '', is_public: true })

// 规则随语言重建；关闭规则的自动重校验，切语言不会触发验证或阻断已填内容。
const rules = computed<FormRules>(() => ({
  name: [{ required: true, message: t('knowledge.form.nameRequired'), trigger: 'blur' }]
}))

// 已显示的旧语言错误在切换语言后清除，避免提示与当前语言不一致。
watch(locale, () => {
  if (dialogVisible.value) formRef.value?.clearValidate()
})

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
      ElMessage.success(t('common.updated'))
    } else {
      await kbApi.create(form)
      ElMessage.success(t('common.created'))
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
      t('knowledge.deleteConfirm', { name: kb.name }),
      t('common.danger'),
      {
        type: 'warning',
        confirmButtonText: t('knowledge.deleteConfirmButton'),
        cancelButtonText: t('common.cancel'),
        confirmButtonClass: 'el-button--danger'
      }
    )
  } catch {
    return
  }
  await kbApi.remove(kb.id)
  ElMessage.success(t('common.deleted'))
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
  background: var(--success-soft);
  color: var(--success);
}

.overview-icon.warm {
  background: var(--warning-soft);
  color: var(--warning);
}

.overview-icon.neutral {
  background: var(--slate-soft);
  color: var(--slate-text);
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
  border-color: var(--accent-border);
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
  border: 1px solid var(--line);
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
  min-width: 0;
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
  white-space: normal;
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
  max-width: 22em;
  color: var(--text-3);
  font-size: 12px;
  font-weight: 400;
  text-align: center;
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
