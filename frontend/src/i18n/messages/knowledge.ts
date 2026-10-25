export default {
  'zh-CN': {
    knowledge: {
      eyebrow: 'KNOWLEDGE BASE',
      title: '知识库管理',
      subtitle: '每个知识库对应一套独立的向量集合，文档互不干扰',
      searchPlaceholder: '搜索知识库',
      searchAria: '搜索知识库',
      create: '新建知识库',
      overviewAria: '知识库概览',
      overview: {
        bases: '知识库',
        documents: '文档总数',
        publicBases: '公开知识库',
        privateBases: '私有知识库'
      },
      listTitle: '全部知识库',
      listMatch: '找到 {count} 个匹配的知识库',
      listHint: '打开知识库管理文档，或直接进入问答',
      actionsAria: '知识库操作：{name}',
      documentCount: '{count} 篇文档',
      manage: '管理文档',
      chat: '开始问答',
      newTitle: '新建知识库',
      newHint: '为新的资料集合建立独立空间',
      emptyMatch: '没有匹配的知识库',
      empty: '还没有知识库，先创建一个吧',
      clearSearch: '清除搜索',
      dialogCreateTitle: '新建知识库',
      dialogEditTitle: '编辑知识库',
      form: {
        name: '名称',
        namePlaceholder: '例如：产品手册库',
        nameRequired: '请输入知识库名称',
        description: '描述',
        descriptionPlaceholder: '简要说明知识库用途，便于其他成员识别',
        visibility: '可见范围',
        publicHint: '公开：所有登录用户可见',
        privateHint: '私有：仅创建者与管理员可见'
      },
      deleteConfirm: '删除知识库「{name}」将同时删除其下所有文档和向量数据，且不可恢复。确定继续？',
      deleteConfirmButton: '确定删除'
    }
  },
  'en-US': {
    knowledge: {
      eyebrow: 'KNOWLEDGE BASE',
      title: 'Knowledge bases',
      subtitle: 'Each knowledge base has its own vector collection, so documents never mix',
      searchPlaceholder: 'Search knowledge bases',
      searchAria: 'Search knowledge bases',
      create: 'New knowledge base',
      overviewAria: 'Knowledge base overview',
      overview: {
        bases: 'Knowledge bases',
        documents: 'Documents',
        publicBases: 'Public knowledge bases',
        privateBases: 'Private knowledge bases'
      },
      listTitle: 'All knowledge bases',
      listMatch: '{count} matching knowledge base | {count} matching knowledge bases',
      listHint: 'Open a knowledge base to manage its documents, or start a chat',
      actionsAria: 'Knowledge base actions: {name}',
      documentCount: '{count} document | {count} documents',
      manage: 'Manage docs',
      chat: 'Start chat',
      newTitle: 'New knowledge base',
      newHint: 'Create a separate space for a new document collection',
      emptyMatch: 'No matching knowledge bases',
      empty: 'No knowledge bases yet. Create one to get started.',
      clearSearch: 'Clear search',
      dialogCreateTitle: 'New knowledge base',
      dialogEditTitle: 'Edit knowledge base',
      form: {
        name: 'Name',
        namePlaceholder: 'e.g. Product manuals',
        nameRequired: 'Enter a knowledge base name',
        description: 'Description',
        descriptionPlaceholder: 'Briefly describe what this knowledge base is for, so teammates can recognize it',
        visibility: 'Visibility',
        publicHint: 'Public: visible to all signed-in users',
        privateHint: 'Private: visible only to the creator and administrators'
      },
      deleteConfirm: 'Deleting "{name}" also deletes all of its documents and vector data, and cannot be undone. Continue?',
      deleteConfirmButton: 'Delete'
    }
  }
}
