<template>
  <el-dialog
    :model-value="modelValue"
    :title="dialogTitle"
    width="min(780px, 96vw)"
    append-to-body
    :close-on-click-modal="false"
    @update:model-value="onVisibilityChange"
    @closed="onClosed"
  >
    <template v-if="!canManage">
      <el-alert
        type="error"
        :title="t('knowledge.members.noPermission')"
        :closable="false"
        show-icon
        data-testid="members-no-permission"
      />
    </template>

    <template v-else>
      <div class="members-head">
        <p class="members-intro">{{ t('knowledge.members.intro') }}</p>
        <span class="revision-chip" data-testid="members-revision">
          {{ revisionText }}
        </span>
      </div>
      <p v-if="isProjectKb(props.kb)" class="inline-hint project-hint">
        {{ t('knowledge.members.projectHint') }}
      </p>

      <section class="members-add" :aria-label="t('knowledge.members.addTitle')">
        <h4 class="section-label">{{ t('knowledge.members.addTitle') }}</h4>
        <div class="add-row">
          <el-select
            v-model="selectedUserId"
            class="user-select"
            filterable
            remote
            :remote-method="onSearchInput"
            :loading="searching"
            :placeholder="t('knowledge.members.userSearchPlaceholder')"
            :aria-label="t('knowledge.members.userSearchAria')"
            :no-data-text="searchEmptyText"
            :disabled="mutationPending"
            data-testid="members-user-select"
          >
            <el-option
              v-for="user in userResults"
              :key="user.id"
              :label="userLabel(user)"
              :value="user.id"
              :disabled="targetState(user) !== 'ready'"
            >
              <span class="option-name">{{ userLabel(user) }}</span>
              <span class="option-username">@{{ user.username }}</span>
              <span v-if="targetState(user) !== 'ready'" class="option-hint">
                {{ targetHint(user) }}
              </span>
            </el-option>
          </el-select>

          <el-select
            v-model="selectedRole"
            class="role-select"
            :aria-label="t('knowledge.members.roleLabel')"
            :disabled="mutationPending"
          >
            <el-option value="reader" :label="t('knowledge.members.reader')" />
            <el-option value="maintainer" :label="t('knowledge.members.maintainer')" />
          </el-select>

          <el-button
            type="primary"
            :loading="busy === 'add'"
            :disabled="!canAdd"
            @click="addMember"
          >
            <el-icon><Plus /></el-icon>{{ t('knowledge.members.add') }}
          </el-button>
        </div>
        <p v-if="searchError" class="inline-error" role="alert" data-testid="members-search-error">
          {{ searchError }}
          <el-button link type="primary" size="small" :disabled="searching" @click="retrySearch">
            {{ t('knowledge.members.searchRetry') }}
          </el-button>
        </p>
        <p v-else-if="searching" class="inline-hint">{{ t('knowledge.members.searchLoading') }}</p>
        <p v-else-if="searched && !userResults.length" class="inline-hint">
          {{ t('knowledge.members.searchEmpty') }}
        </p>
      </section>

      <section class="members-list" :aria-label="t('knowledge.members.listTitle')">
        <div class="list-head">
          <h4 class="section-label">{{ t('knowledge.members.listTitle') }}</h4>
          <span class="list-total">
            {{ t('knowledge.members.total', { total: formatNumber(total) }, total) }}
          </span>
        </div>

        <div v-loading="loading" class="list-body">
          <div v-if="listError" class="inline-error" role="alert" data-testid="members-load-error">
            {{ listError }}
            <el-button link type="primary" size="small" :disabled="loading" @click="retryList">
              {{ t('knowledge.members.retry') }}
            </el-button>
          </div>
          <el-empty
            v-else-if="!loading && !members.length"
            :description="t('knowledge.members.empty')"
            :image-size="64"
          />
          <ul v-else-if="members.length" class="member-rows">
            <li
              v-for="member in members"
              :key="member.user_id"
              class="member-row"
              :data-user-id="member.user_id"
            >
              <div class="member-identity">
                <span class="member-avatar" aria-hidden="true">{{ initialOf(member) }}</span>
                <div class="member-copy">
                  <span class="member-name">{{ memberLabel(member) }}</span>
                  <span class="member-username">@{{ member.username }}</span>
                </div>
                <el-tag v-if="!member.is_active" size="small" type="info" effect="plain">
                  {{ t('knowledge.members.inactive') }}
                </el-tag>
              </div>

              <el-select
                :key="`${member.user_id}-${roleReset}`"
                :model-value="member.role"
                size="small"
                class="member-role"
                :aria-label="t('knowledge.members.roleAria', { name: memberLabel(member) })"
                :disabled="mutationPending"
                @change="(role: unknown) => changeRole(member, role)"
              >
                <el-option value="reader" :label="t('knowledge.members.reader')" />
                <el-option value="maintainer" :label="t('knowledge.members.maintainer')" />
              </el-select>

              <el-button
                link
                type="danger"
                size="small"
                :disabled="mutationPending"
                @click="confirmRemove(member)"
              >
                {{ t('knowledge.members.remove') }}
              </el-button>
            </li>
          </ul>
        </div>

        <el-pagination
          class="members-pagination"
          :current-page="page"
          :page-size="pageSize"
          :page-sizes="[20, 50, 100]"
          :total="total"
          :pager-count="5"
          layout="total, sizes, prev, pager, next"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </section>
    </template>

    <template #footer>
      <el-button @click="close">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { userApi, isProjectKb, type KnowledgeBase, type User } from '@/api'
import { kbMembersApi, type KbMember, type KbMemberRole } from '@/api/kbMembers'
import { formatNumber } from '@/i18n/format'
import { useUserStore } from '@/stores/user'
import { canManageMembers } from '@/utils/kbCapabilities'
import {
  MEMBER_PAGE_SIZE,
  canSubmitCapturedRemoval,
  classifyMemberError,
  isMemberRequestCurrent,
  isKbMemberRole,
  memberErrorKey,
  memberTargetState,
  normalizeMemberPage,
  normalizeMemberPageSize,
  normalizeMemberRevision,
  pageAfterRemoval,
  shouldInvalidateMemberState,
  shouldRefreshCandidatesAfterMemberError,
  shouldStopMemberManagement
} from '@/utils/kbMembers'

const props = defineProps<{
  modelValue: boolean
  kb: KnowledgeBase | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  revoked: []
}>()

const { t } = useI18n()
const userStore = useUserStore()

const members = ref<KbMember[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(MEMBER_PAGE_SIZE)
const revision = ref<number | null>(null)
const loading = ref(false)
const listError = ref('')

const userResults = ref<User[]>([])
const selectedUserId = ref('')
const selectedRole = ref<KbMemberRole>('reader')
const searchKeyword = ref('')
const searching = ref(false)
const searched = ref(false)
const searchError = ref('')

const busy = ref('')
// 角色提交失败后递增，强制重建行内下拉，回到服务端当前角色而不是失败前的乐观选择。
const roleReset = ref(0)
let generation = 0
let disposed = false
let listRequest = 0
let searchRequest = 0
let mutationRequest = 0
let listController: AbortController | null = null
let searchController: AbortController | null = null
let mutationController: AbortController | null = null
let searchTimer: number | undefined

const canManage = computed(() => canManageMembers(props.kb))
const mutationPending = computed(() => busy.value !== '')
const dialogTitle = computed(() =>
  props.kb ? t('knowledge.members.title', { name: props.kb.name }) : t('knowledge.members.menu')
)
const revisionText = computed(() =>
  revision.value === null
    ? t('knowledge.members.revisionUnknown')
    : t('knowledge.members.revisionValue', { revision: revision.value })
)
const searchEmptyText = computed(() =>
  searched.value ? t('knowledge.members.searchEmpty') : t('knowledge.members.selectUser')
)
const existingIds = computed(() => new Set(members.value.map((member) => member.user_id)))
const selectedUser = computed(
  () => userResults.value.find((user) => user.id === selectedUserId.value) || null
)
const canAdd = computed(
  () =>
    canManage.value &&
    !mutationPending.value &&
    revision.value !== null &&
    !!selectedUser.value &&
    memberTargetState(selectedUser.value, existingIds.value) === 'ready'
)

function userLabel(user: Pick<User, 'username' | 'display_name'>): string {
  return user.display_name?.trim() || user.username
}

function memberLabel(member: Pick<KbMember, 'username' | 'display_name'>): string {
  return member.display_name?.trim() || member.username
}

function initialOf(member: Pick<KbMember, 'username' | 'display_name'>): string {
  return memberLabel(member).slice(0, 1).toUpperCase()
}

function targetState(user: User) {
  return memberTargetState(user, existingIds.value)
}

function targetHint(user: User): string {
  const state = targetState(user)
  if (state === 'inactive') return t('knowledge.members.searchInactive')
  if (state === 'existing') return t('knowledge.members.searchExisting')
  return ''
}

function isAlive(session: number, kbId: string): boolean {
  return (
    !disposed &&
    generation === session &&
    props.modelValue &&
    props.kb?.id === kbId
  )
}

function stillCurrent(session: number, kbId: string, requestId: number, counter: number): boolean {
  return (
    isMemberRequestCurrent(
      { session, kbId, requestId },
      {
        session: generation,
        kbId: props.kb?.id ?? null,
        open: props.modelValue,
        disposed
      },
      counter
    ) && isAlive(session, kbId)
  )
}

function invalidate() {
  generation += 1
  listController?.abort()
  searchController?.abort()
  mutationController?.abort()
  listController = null
  searchController = null
  mutationController = null
  loading.value = false
  searching.value = false
  busy.value = ''
  if (searchTimer !== undefined) {
    window.clearTimeout(searchTimer)
    searchTimer = undefined
  }
}

function resetState() {
  members.value = []
  total.value = 0
  page.value = 1
  pageSize.value = MEMBER_PAGE_SIZE
  revision.value = null
  listError.value = ''
  userResults.value = []
  selectedUserId.value = ''
  selectedRole.value = 'reader'
  searchKeyword.value = ''
  searched.value = false
  searchError.value = ''
}

function openSession() {
  invalidate()
  resetState()
  if (!props.modelValue || !props.kb) return
  if (!canManage.value) {
    listError.value = t('knowledge.members.noPermission')
    return
  }
  // 用户搜索接口仅管理员可用；能力 true 且能进入此对话框时仍需服务端再次鉴权。
  if (!userStore.isAdmin) {
    listError.value = t('knowledge.members.noPermission')
    return
  }
  void loadMembers(1)
  void searchUsers('')
}

async function loadMembers(targetPage: unknown = page.value) {
  const kbId = props.kb?.id
  if (!kbId || !canManage.value || disposed) return
  const session = generation
  const requestId = ++listRequest
  const requestedPage = normalizeMemberPage(targetPage)
  const requestedSize = normalizeMemberPageSize(pageSize.value)
  listController?.abort()
  const controller = new AbortController()
  listController = controller
  loading.value = true
  listError.value = ''
  try {
    const { data } = await kbMembersApi.list(
      kbId,
      { page: requestedPage, page_size: requestedSize },
      controller.signal
    )
    if (!stillCurrent(session, kbId, requestId, listRequest)) return
    const nextRevision = normalizeMemberRevision(data.revision)
    if (nextRevision === null) {
      // 版本未知时不允许继续提交；连同一批成员也视为不可信，避免旧权限残留。
      members.value = []
      total.value = 0
      revision.value = null
      listError.value = t('knowledge.members.revisionUnknownHint')
      return
    }
    members.value = Array.isArray(data.items) ? data.items : []
    total.value = Number.isFinite(data.total) ? Math.max(0, data.total) : 0
    revision.value = nextRevision
    page.value = requestedPage
    pageSize.value = requestedSize
  } catch (err: unknown) {
    if (!stillCurrent(session, kbId, requestId, listRequest)) return
    const kind = classifyMemberError(err)
    if (kind === 'cancelled') return
    // 列表失败时绝不保留旧成员或旧 revision。
    members.value = []
    total.value = 0
    revision.value = null
    listError.value = t(memberErrorKey(kind))
    if (kind === 'unauthenticated' || kind === 'forbidden' || kind === 'not_found') {
      emit('revoked')
    }
  } finally {
    if (stillCurrent(session, kbId, requestId, listRequest)) loading.value = false
  }
}

function retryList() {
  if (!props.kb || loading.value) return
  void loadMembers(page.value)
}

async function searchUsers(keyword = '') {
  const kbId = props.kb?.id
  if (!kbId || !canManage.value || disposed || !userStore.isAdmin) return
  const session = generation
  const requestId = ++searchRequest
  searchController?.abort()
  const controller = new AbortController()
  searchController = controller
  searching.value = true
  searchError.value = ''
  try {
    const { data } = await userApi.list({
      page: 1,
      page_size: 20,
      keyword: keyword.trim() || undefined
    })
    if (!stillCurrent(session, kbId, requestId, searchRequest)) return
    userResults.value = Array.isArray(data.items) ? data.items : []
    searched.value = true
  } catch (err: unknown) {
    if (!stillCurrent(session, kbId, requestId, searchRequest)) return
    const kind = classifyMemberError(err)
    if (kind === 'cancelled') return
    userResults.value = []
    searched.value = true
    searchError.value = t(memberErrorKey(kind))
    if (kind === 'unauthenticated' || kind === 'forbidden') emit('revoked')
  } finally {
    if (stillCurrent(session, kbId, requestId, searchRequest)) searching.value = false
  }
}

function onSearchInput(keyword: string) {
  searchKeyword.value = keyword
  if (searchTimer !== undefined) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    searchTimer = undefined
    void searchUsers(searchKeyword.value)
  }, 250)
}

function retrySearch() {
  if (searching.value) return
  void searchUsers(searchKeyword.value)
}

function ensureRevision(): boolean {
  if (revision.value === null) {
    ElMessage.warning(t('knowledge.members.revisionUnknownHint'))
    return false
  }
  return true
}

/**
 * 写入结果未知或版本已可疑：旧成员与旧 revision 一律不能再当作事实。
 * 只允许用户通过"重试"重新读取，绝不自动重放任何写请求。
 */
function invalidateMemberState(message: string) {
  members.value = []
  total.value = 0
  revision.value = null
  listError.value = message
}

function applyRevision(value: unknown): boolean {
  const next = normalizeMemberRevision(value)
  if (next === null) {
    invalidateMemberState(t('knowledge.members.revisionUnknownHint'))
    return false
  }
  revision.value = next
  return true
}

function refreshAfterMutation(kind: ReturnType<typeof classifyMemberError>) {
  if (shouldRefreshCandidatesAfterMemberError(kind)) {
    void searchUsers(searchKeyword.value)
  }
}

function showMutationError(kind: ReturnType<typeof classifyMemberError>) {
  if (kind === 'cancelled') return
  if (kind === 'conflict') {
    ElMessage.warning(t('knowledge.members.errors.conflict'))
    // 刷新期间立即收回旧 revision：在拿到新快照前不允许再次提交旧版本。
    invalidateMemberState(t('knowledge.members.revisionUnknownHint'))
    void loadMembers(page.value)
    return
  }
  if (shouldStopMemberManagement(kind)) {
    ElMessage.error(t('knowledge.members.errors.forbidden'))
    emit('revoked')
    return
  }
  if (kind === 'not_found') {
    ElMessage.warning(t('knowledge.members.errors.notFound'))
    refreshAfterMutation(kind)
    invalidateMemberState(t('knowledge.members.revisionUnknownHint'))
    void loadMembers(page.value)
    return
  }
  if (kind === 'invalid') {
    ElMessage.warning(t('knowledge.members.errors.invalid'))
    refreshAfterMutation(kind)
    void loadMembers(page.value)
    return
  }
  if (shouldInvalidateMemberState(kind)) {
    // 网络中断/超时/5xx：写入可能已提交而响应丢失，旧列表和旧 revision 都不可信。
    ElMessage.error(t('knowledge.members.errors.generic'))
    invalidateMemberState(t('knowledge.members.errors.generic'))
    return
  }
  ElMessage.error(t('knowledge.members.errors.generic'))
}

async function addMember() {
  const kbId = props.kb?.id
  if (!kbId || !canManage.value || !canAdd.value || mutationPending.value) return
  const user = selectedUser.value
  if (!user || !ensureRevision()) return
  const session = generation
  const requestId = ++mutationRequest
  mutationController?.abort()
  const controller = new AbortController()
  mutationController = controller
  busy.value = 'add'
  try {
    const { data } = await kbMembersApi.setRole(
      kbId,
      user.id,
      { role: selectedRole.value, expected_revision: revision.value as number },
      controller.signal
    )
    if (!stillCurrent(session, kbId, requestId, mutationRequest)) return
    if (!applyRevision(data.revision)) {
      await loadMembers(page.value)
      return
    }
    selectedUserId.value = ''
    ElMessage.success(t('knowledge.members.toast.added'))
    await loadMembers(page.value)
    if (isAlive(session, kbId)) void searchUsers(searchKeyword.value)
  } catch (err: unknown) {
    if (!stillCurrent(session, kbId, requestId, mutationRequest)) return
    showMutationError(classifyMemberError(err))
  } finally {
    if (stillCurrent(session, kbId, requestId, mutationRequest)) busy.value = ''
    if (mutationController === controller) mutationController = null
  }
}

async function changeRole(member: KbMember, role: unknown) {
  const kbId = props.kb?.id
  if (!kbId || !canManage.value || mutationPending.value) return
  if (!isKbMemberRole(role) || role === member.role) return
  if (!ensureRevision()) return
  const session = generation
  const requestId = ++mutationRequest
  mutationController?.abort()
  const controller = new AbortController()
  mutationController = controller
  busy.value = member.user_id
  try {
    const { data } = await kbMembersApi.setRole(
      kbId,
      member.user_id,
      { role, expected_revision: revision.value as number },
      controller.signal
    )
    if (!stillCurrent(session, kbId, requestId, mutationRequest)) return
    if (!applyRevision(data.revision)) {
      void loadMembers(page.value)
      return
    }
    if (data.member && data.member.user_id === member.user_id && isKbMemberRole(data.member.role)) {
      member.role = data.member.role
      member.is_active = data.member.is_active
    } else {
      void loadMembers(page.value)
    }
    ElMessage.success(t('knowledge.members.toast.roleChanged'))
  } catch (err: unknown) {
    if (!stillCurrent(session, kbId, requestId, mutationRequest)) return
    roleReset.value += 1
    showMutationError(classifyMemberError(err))
  } finally {
    if (stillCurrent(session, kbId, requestId, mutationRequest)) busy.value = ''
    if (mutationController === controller) mutationController = null
  }
}

async function confirmRemove(member: KbMember) {
  const kbId = props.kb?.id
  if (!kbId || !canManage.value || mutationPending.value) return
  if (!ensureRevision()) return
  // 确认框打开前固定会话、KB 与 revision。等待期间关闭并重开同一 KB 会得到新的
  // generation/revision，旧确认不能借用它们发 DELETE。
  const session = generation
  const expectedRevision = revision.value as number
  try {
    await ElMessageBox.confirm(
      t('knowledge.members.removeConfirm', { name: memberLabel(member) }),
      t('knowledge.members.removeTitle'),
      {
        type: 'warning',
        confirmButtonText: t('knowledge.members.removeConfirmButton'),
        cancelButtonText: t('common.cancel'),
        confirmButtonClass: 'el-button--danger'
      }
    )
  } catch {
    return
  }
  // 原会话必须仍有效、确认期间没有其它 mutation、revision 未被替换。
  const captured = { session, kbId, expectedRevision }
  const canSubmit = canSubmitCapturedRemoval(captured, {
    session: generation,
    kbId: props.kb?.id ?? null,
    open: props.modelValue,
    disposed,
    revision: revision.value,
    busy: mutationPending.value
  }) && canManage.value
  if (!canSubmit) {
    // revision 已被刷新：交回列表让用户重新确认，绝不用新版本静默发送旧操作。
    if (isAlive(session, kbId) && revision.value !== expectedRevision) {
      ElMessage.warning(t('knowledge.members.errors.conflict'))
    }
    return
  }
  const requestId = ++mutationRequest
  mutationController?.abort()
  const controller = new AbortController()
  mutationController = controller
  busy.value = member.user_id
  try {
    const { data } = await kbMembersApi.remove(
      kbId,
      member.user_id,
      expectedRevision,
      controller.signal
    )
    if (!stillCurrent(session, kbId, requestId, mutationRequest)) return
    if (!applyRevision(data.revision)) {
      await loadMembers(page.value)
      return
    }
    ElMessage.success(t('knowledge.members.toast.removed'))
    const targetPage = pageAfterRemoval(page.value, Math.max(0, total.value - 1), pageSize.value)
    await loadMembers(targetPage)
  } catch (err: unknown) {
    if (!stillCurrent(session, kbId, requestId, mutationRequest)) return
    showMutationError(classifyMemberError(err))
  } finally {
    if (stillCurrent(session, kbId, requestId, mutationRequest)) busy.value = ''
    if (mutationController === controller) mutationController = null
  }
}

function onPageChange(value: number) {
  if (!canManage.value) return
  page.value = normalizeMemberPage(value)
  void loadMembers(page.value)
}

function onSizeChange(value: number) {
  if (!canManage.value) return
  pageSize.value = normalizeMemberPageSize(value)
  page.value = 1
  void loadMembers(1)
}

function onVisibilityChange(value: boolean) {
  emit('update:modelValue', value)
  if (!value) invalidate()
}

function onClosed() {
  invalidate()
}

function close() {
  emit('update:modelValue', false)
  invalidate()
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) openSession()
    else invalidate()
  }
)

watch(
  () => props.kb?.id,
  () => {
    if (props.modelValue) openSession()
  }
)

onMounted(() => {
  if (props.modelValue) openSession()
})

onBeforeUnmount(() => {
  disposed = true
  invalidate()
})
</script>

<style scoped>
.members-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px 16px;
  margin-bottom: 16px;
}

.members-intro {
  flex: 1 1 320px;
  margin: 0;
  color: var(--text-3);
  font-size: 13px;
  line-height: 1.65;
}

.revision-chip {
  flex-shrink: 0;
  padding: 3px 10px;
  border: 1px solid var(--accent-border);
  border-radius: 999px;
  background: var(--brand-tint);
  color: var(--brand-dark);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.section-label {
  margin: 0;
  color: var(--text);
  font-size: 13.5px;
  font-weight: 650;
  line-height: 1.5;
}

.members-add {
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
}

.add-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 10px;
}

.user-select {
  flex: 1 1 260px;
  min-width: 180px;
}

.role-select {
  flex: 0 0 128px;
}

.option-name {
  color: var(--text);
}

.option-username {
  margin-left: 8px;
  color: var(--text-3);
  font-size: 12px;
}

.option-hint {
  margin-left: 8px;
  color: var(--warning);
  font-size: 12px;
}

.role-hint,
.inline-hint {
  margin: 8px 0 0;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.6;
}

.inline-error {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0 0;
  color: var(--danger);
  font-size: 12.5px;
  line-height: 1.6;
}

.members-list {
  margin-top: 18px;
}

.list-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.list-total {
  color: var(--text-3);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.list-body {
  min-height: 90px;
}

.member-rows {
  margin: 0;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  list-style: none;
}

.member-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 14px;
  padding: 11px 14px;
}

.member-row + .member-row {
  border-top: 1px solid var(--line);
}

.member-identity {
  display: flex;
  align-items: center;
  flex: 1 1 240px;
  gap: 10px;
  min-width: 0;
}

.member-avatar {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 13px;
  font-weight: 650;
}

.member-copy {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.member-name {
  overflow: hidden;
  color: var(--text);
  font-size: 13.5px;
  font-weight: 550;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.member-username {
  color: var(--text-3);
  font-size: 12px;
}

.member-role {
  flex: 0 0 120px;
}

.members-pagination {
  margin-top: 12px;
}

@media (max-width: 560px) {
  .role-select,
  .member-role {
    flex: 1 1 120px;
  }

  .member-row {
    align-items: flex-start;
  }

  /* 390px 下保留页码与总数，收起每页条数选择，避免横向溢出。 */
  .members-pagination :deep(.el-pagination__sizes) {
    display: none;
  }
}
</style>