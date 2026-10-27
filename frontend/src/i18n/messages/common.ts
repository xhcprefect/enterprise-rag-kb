export default {
  'zh-CN': {
    common: {
      appName: '企业知识库', workspace: '工作空间', user: '用户', admin: '管理员', member: '普通用户',
      username: '用户名', displayName: '显示名称', email: '邮箱', password: '密码', role: '角色', status: '状态',
      createdAt: '创建时间', actions: '操作', search: '搜索', refresh: '刷新', cancel: '取消', confirm: '确定',
      save: '保存', create: '创建', edit: '编辑', delete: '删除', close: '关闭', back: '返回', tip: '提示',
      danger: '危险操作', created: '已创建', updated: '已更新', deleted: '已删除', notProvided: '未填写',
      loading: '加载中…', noDescription: '暂无描述', public: '公开', private: '私有', enabled: '启用', disabled: '禁用'
    },
    nav: { login: '登录', chat: '知识问答', knowledge: '知识库管理', documents: '文档管理', usage: '用量与费用', users: '用户管理', audit: '审计日志', profile: '个人中心' },
    preferences: {
      theme: '主题设置', light: '浅色模式', dark: '深色模式', system: '跟随系统',
      language: '界面语言', currentTheme: '当前主题：{theme}', currentLanguage: '当前语言：简体中文', selected: '已选择'
    },
    layout: {
      closeNav: '关闭导航', mainNav: '主导航', home: '企业知识库首页', openNav: '打开导航菜单',
      collapseNav: '收起菜单', expandNav: '展开菜单', collapseSidebar: '收起侧边栏',
      noteTitle: '让每一份资料发挥价值', noteLine1: '集中管理企业文档', noteLine2: '检索答案，核对引用来源',
      noteCaption: '文档 · 检索 · 知识', adminAccount: '管理员账户', memberAccount: '成员账户',
      accountMenu: '账户菜单', logout: '退出登录', logoutConfirm: '确定要退出登录吗？'
    },
    errors: {
      request: '请求失败', unknown: '未知错误', network: '网络连接失败，请检查网络后重试', timeout: '请求超时，请稍后重试', invalidStream: '流式响应格式异常，请重试',
      streamInterrupted: '回答连接提前中断，请重试', http: '请求失败（{status}）'
    }
  },
  'en-US': {
    common: {
      appName: 'Knowledge Hub', workspace: 'Workspace', user: 'User', admin: 'Administrator', member: 'Member',
      username: 'Username', displayName: 'Display name', email: 'Email', password: 'Password', role: 'Role', status: 'Status',
      createdAt: 'Created', actions: 'Actions', search: 'Search', refresh: 'Refresh', cancel: 'Cancel', confirm: 'Confirm',
      save: 'Save', create: 'Create', edit: 'Edit', delete: 'Delete', close: 'Close', back: 'Back', tip: 'Confirmation',
      danger: 'Destructive action', created: 'Created', updated: 'Updated', deleted: 'Deleted', notProvided: 'Not provided',
      loading: 'Loading…', noDescription: 'No description', public: 'Public', private: 'Private', enabled: 'Active', disabled: 'Disabled'
    },
    nav: { login: 'Sign in', chat: 'Knowledge chat', knowledge: 'Knowledge bases', documents: 'Documents', usage: 'Usage and cost', users: 'Users', audit: 'Audit log', profile: 'My account' },
    preferences: {
      theme: 'Appearance', light: 'Light mode', dark: 'Dark mode', system: 'Use system setting',
      language: 'Interface language', currentTheme: 'Appearance: {theme}', currentLanguage: 'Current language: English', selected: 'Selected'
    },
    layout: {
      closeNav: 'Close navigation', mainNav: 'Main navigation', home: 'Knowledge Hub home', openNav: 'Open navigation',
      collapseNav: 'Collapse navigation', expandNav: 'Expand navigation', collapseSidebar: 'Collapse sidebar',
      noteTitle: 'Put your documents to work', noteLine1: 'Organize enterprise documents', noteLine2: 'Find answers and check sources',
      noteCaption: 'DOCUMENTS · SEARCH · KNOWLEDGE', adminAccount: 'Administrator account', memberAccount: 'Member account',
      accountMenu: 'Account menu', logout: 'Sign out', logoutConfirm: 'Are you sure you want to sign out?'
    },
    errors: {
      request: 'Request failed', unknown: 'Unknown error', network: 'Unable to connect. Check your network and try again.', timeout: 'The request timed out. Please try again.', invalidStream: 'Invalid response stream. Please try again.',
      streamInterrupted: 'The response connection ended early. Please try again.', http: 'Request failed ({status})'
    }
  }
}
