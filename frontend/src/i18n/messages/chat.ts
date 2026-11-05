export default {
  'zh-CN': {
    chat: {
      newConversation: '新对话',
      sidebar: {
        aria: '对话设置与历史会话',
        close: '关闭会话侧栏',
        open: '打开对话设置与历史会话',
        title: '对话工作台',
        subtitle: '连接知识，发现答案',
        note: '知识库问答检索文档内容，普通对话直接与模型交流。'
      },
      mode: {
        label: '对话模式',
        rag: '知识库问答',
        chat: '普通对话'
      },
      kb: {
        label: '当前知识库',
        placeholder: '选择知识库',
        docCount: '{count} 篇'
      },
      scope: {
        label: '当前依据',
        all: '全部知识库',
        choose: '选择文档',
        change: '切换范围',
        clear: '取消限定',
        saving: '正在保存范围…',
        saveFailed: '范围保存失败，请重试；当前依据保持不变。',
        conflict: '会话范围已被更新或正在生成回答，请重新载入会话后再试。',
        incomplete: '服务端没有返回完整的范围快照，当前依据保持不变，请重试。',
        lockedHint: '生成回答或保存范围时不能切换依据',
        removeAria: '从当前依据中移除：{name}',
        onlyThis: '仅依据此文档',
        restrictedHint: '本次回答仅依据所选 {count} 篇文档。',
        dialog: {
          title: '限定回答依据',
          description: '只从已就绪的文档中检索，最多选择 {max} 篇。清空选择表示检索整个知识库。',
          selectedTitle: '已选文档',
          selectedCount: '已选 {count}/{max}',
          selectedEmpty: '尚未选择文档，将检索整个知识库。',
          availableTitle: '已就绪的文档',
          loading: '正在加载文档…',
          loadedCount: '已加载 {loaded}/{total}',
          loadMore: '加载更多',
          empty: '这个知识库还没有已就绪的文档。',
          loadFailed: '文档加载失败，请重试。',
          retry: '重试',
          limit: '最多只能选择 {max} 篇文档。',
          cancel: '取消',
          confirm: '应用范围'
        }
      },
      newChat: '开启新对话',
      recent: {
        title: '最近会话',
        deleteAria: '删除会话：{title}',
        empty: '还没有历史会话',
        emptyHint: '从一个问题开始吧'
      },
      list: {
        searchPlaceholder: '搜索会话标题',
        searchAria: '按标题搜索会话',
        searchClear: '清除搜索',
        total: '共 {total} 个会话',
        totalFiltered: '匹配 {total} 个会话',
        matched: '共 {total} 个会话',
        loadMore: '加载更多',
        loadingMore: '正在加载…',
        loadFailed: '会话列表加载失败，请重试。',
        loadMoreFailed: '更多会话加载失败，请重试。',
        retry: '重试',
        rename: '重命名',
        renameAria: '重命名会话：{title}',
        renameTitle: '重命名会话',
        renamePlaceholder: '输入新的会话标题',
        renameHint: '1–200 个字符，保存时自动去除首尾空格。',
        renameEmpty: '标题不能为空。',
        renameTooLong: '标题不能超过 200 个字符。',
        renameFailed: '重命名失败，标题保持不变。',
        pin: '置顶',
        unpin: '取消置顶',
        pinAria: '置顶会话：{title}',
        unpinAria: '取消置顶会话：{title}',
        pinned: '已置顶',
        pinFailed: '置顶状态更新失败，列表保持不变。',
        metadataFailed: '会话信息更新失败，请重试。',
        copyAnswer: '复制回答',
        copyAnswerAria: '复制这条回答',
        copyCode: '复制代码',
        copyCodeAria: '复制代码块',
        copied: '已复制',
        copyFailed: '复制失败，请手动选择文本复制。',
        retrySend: '重新提问',
        retryAria: '用同样的问题和设置重新提问',
        retryBlocked: '原请求仍在处理或已成功，未重新发送。',
        retryUnknown: '暂时无法确认原请求结果，已停止重试；请稍后检查结果。',
        retryUnavailable: '原请求的提问内容或文档范围已不可用，请复制原问题后手动发送。',
        retryMismatch: '当前文档范围或模式与原请求不同，为避免改变依据，未重新发送。',
        cancel: '取消',
        save: '保存'
      },
      head: {
        freeChat: '与 AI 助手自由交流',
        selectKb: '选择知识库，开始探索',
        messageCount: '{count} 条消息'
      },
      empty: {
        selectKb: '选择一个知识库，开始有据可查的问答。',
        noKb: '先创建知识库并添加文档，或切换到普通对话。',
        choose: '选择知识库或对话模式',
        goKnowledge: '前往知识库管理'
      },
      messagesAria: '对话消息',
      welcome: {
        eyebrow: '让知识，触手可及',
        title: '今天，有什么想了解的？',
        ragLead: '从文档中寻找线索，让回答有据可查。',
        ragKb: '关于「{kb}」，你可以这样问：',
        chatLead: '写作、梳理思路，或探索一个新概念。',
        chatSub: '从你关心的问题开始，与 AI 助手交流。'
      },
      suggestions: {
        ragContents: '这个知识库包含哪些内容？',
        ragContentsTitle: '了解全貌',
        ragSummary: '请总结主要信息',
        ragSummaryTitle: '梳理重点',
        ragProcess: '有哪些关键流程？',
        ragProcessTitle: '查找流程',
        chatIntro: '介绍一下你自己',
        chatIntroTitle: '认识助手',
        chatEmail: '帮我写一封请假邮件',
        chatEmailTitle: '写作协助',
        chatVector: '解释一下什么是向量数据库',
        chatVectorTitle: '概念解读'
      },
      author: {
        you: '你',
        assistant: 'AI 助手'
      },
      generating: '生成中',
      phases: { retrieving: '检索中', generating: '生成中', saving: '保存中', recovering: '正在检查原请求结果' },
      truncated: '回答达到模型输出长度上限，以上内容可能不完整。系统不会自动追加调用。',
      recovery: {
        check: '检查请求结果', noResubmit: '只读取原请求，不会重复生成或追加模型调用。',
        processing: '原请求仍在处理中，请稍后检查结果，避免重复提问。',
        cancelled: '原请求已停止，尚未保存完整回答。', failed: '原请求未完成，尚未保存完整回答。',
        unavailable: '原请求结果不可用、会话已删除或访问权限已改变。',
        failedCheck: '暂时无法确认原请求结果。网络恢复后可再次加载，系统不会自动重新生成。',
        checking: '正在检查原请求结果…',
        checkAgain: '再次检查',
        cancel: '取消原请求',
        lockedHint: '原请求仍在处理中：完成或明确停止前，不能发送新问题或清除恢复入口。'
      },
      project: {
        label: '项目快照',
        all: '使用项目当前就绪快照',
        choose: '选择快照',
        change: '切换快照',
        switched: '已切换项目快照',
        switching: '正在切换快照…',
        noReady: '该项目暂无就绪快照',
        loadFailed: '项目快照列表加载失败',
        busy: '会话正忙（409），已刷新为最新快照，请稍后重试',
        stale: '快照已被更新（409），已刷新为最新快照，请重新选择',
        nonProject: '该会话不是项目会话，无法切换快照',
        denied: '无权访问该快照，或快照已不可用',
        revoked: '项目源已撤销（410），无法切换快照',
        lockedHint: '生成或保存中不能切换快照',
        snapshotLabel: '快照 {id}',
        lines: '行 {start}–{end}',
        line: '行 {line}',
        symbol: '符号 {name}'
      },
      thinking: {
        rag: '正在检索并生成回答…',
        chat: '正在生成回答…'
      },
      citations: {
        title: '引用来源',
        untitled: '未命名文档',
        chunk: '第 {index} 段',
        score: '相似度 {percent}%',
        view: '查看原文'
      },
      source: {
        title: '引用原文', loading: '正在核验并读取原文…', unavailable: '原文已更新、不可用或无法读取，请重新检索。',
        noPosition: '旧索引未记录页码或章节，可查看下方原文；重新索引后补充位置。',
        version: '索引版本 {version}', previewPdf: '预览 PDF 原文件', previewPdfAtPage: '预览 PDF 并定位到第 {page} 页',
        pdfTarget: '定位目标：原文件第 {page} 页（按文件物理页序计算，可能与页内印刷页码不同）。',
        pdfPageUnknown: '尚未确认该引用的页码，预览将从第一页打开；重新索引后可补充页码定位。',
        truncated: '预览内容过长，已截短显示。',
        page: '第 {index} 页', pages: '第 {start}–{end} 页', sheet: '工作表：{name}',
        row: '表格第 {index} 行', rows: '表格第 {start}–{end} 行', line: '文本第 {index} 行', lines: '文本第 {start}–{end} 行'
      },
      memory: {
        label: '对话摘要', view: '查看摘要', reset: '重置摘要', resetTitle: '重置摘要', resetConfirm: '确定重置本会话的摘要压缩吗？重置后旧摘要将不可再作为历史依据。', resetDone: '摘要已重置。', resetFailed: '重置失败，请重试。', resetConflict: '摘要正在更新或版本已变化，已刷新为最新状态。', refresh: '刷新',
        disabled: '摘要未启用', idle: '尚无摘要', pending: '正在生成摘要，已覆盖至第 {covered} 轮', ready: '摘要已覆盖至第 {covered} 轮', readyLag: '已覆盖至第 {covered} 轮，仍有历史待整理', readyUnknown: '已覆盖至第 {covered} 轮，待整理状态未知', failed: '摘要更新失败',
        viewTitle: '对话摘要详情', covered: '已覆盖至（轮次）', lag: '存在待整理历史', truncated: '上次生成被截断', version: '摘要版本', updatedAt: '更新时间', yes: '是', no: '否', unknown: '未知', loading: '正在加载…', unavailable: '摘要状态暂不可用，请稍后查看。',
        nonLossless: '摘要是对旧历史的有损压缩，仅供辅助参考，不等同于无损记忆，也不能替代原始消息。',
        structuredText: '结构化摘要'
      },
      requestStatus: { processing: '请求处理中', failed: '请求未完成', cancelled: '已停止' },
      composer: {
        aria: '输入问题',
        placeholderRag: '向知识库提问，让资料为你提供线索…',
        placeholderChat: '输入你的问题，开始对话…',
        topk: '检索片段',
        topkAria: '检索片段数',
        hintSend: '发送',
        hintNewline: '换行',
        stop: '停止',
        stopping: '正在停止',
        send: '发送',
        disclaimerRag: '回答由 AI 生成，请结合引用来源核对关键信息。',
        disclaimerChat: '回答由 AI 生成，请核对重要信息。'
      },
      deleteConfirm: '确定删除该会话吗？',
      emptyReply: {
        stopped: '（已停止生成）',
        rag: '抱歉，模型未返回内容，请重试或调小「检索片段」。',
        chat: '抱歉，模型未返回内容，请重试。'
      }
    }
  },
  'en-US': {
    chat: {
      newConversation: 'New conversation',
      sidebar: {
        aria: 'Chat settings and conversation history',
        close: 'Close conversation sidebar',
        open: 'Open chat settings and conversation history',
        title: 'Chat workspace',
        subtitle: 'Connect knowledge, find answers',
        note: 'Knowledge chat searches your documents; free chat talks directly with the model.'
      },
      mode: {
        label: 'Chat mode',
        rag: 'Knowledge chat',
        chat: 'Free chat'
      },
      kb: {
        label: 'Current knowledge base',
        placeholder: 'Select a knowledge base',
        docCount: '{count} document | {count} documents'
      },
      scope: {
        label: 'Current scope',
        all: 'Entire knowledge base',
        choose: 'Choose documents',
        change: 'Change scope',
        clear: 'Remove limit',
        saving: 'Saving scope…',
        saveFailed: 'Could not save the scope. Please retry; the current scope is unchanged.',
        conflict: 'The conversation scope changed or an answer is being generated. Reload the conversation and try again.',
        incomplete: 'The server did not return a complete scope snapshot. The current scope is unchanged; please retry.',
        lockedHint: 'The scope cannot be changed while generating or saving',
        removeAria: 'Remove from current scope: {name}',
        onlyThis: 'Use only this document',
        restrictedHint: 'This answer is limited to {count} selected document | These answers are limited to the {count} selected documents.',
        dialog: {
          title: 'Limit answer sources',
          description: 'Only ready documents can be searched. Choose up to {max}. Clear every selection to search the entire knowledge base.',
          selectedTitle: 'Selected documents',
          selectedCount: '{count}/{max} selected',
          selectedEmpty: 'No documents selected. The entire knowledge base will be searched.',
          availableTitle: 'Ready documents',
          loading: 'Loading documents…',
          loadedCount: '{loaded} of {total} loaded',
          loadMore: 'Load more',
          empty: 'This knowledge base has no ready documents yet.',
          loadFailed: 'Could not load documents. Please retry.',
          retry: 'Retry',
          limit: 'You can select up to {max} documents.',
          cancel: 'Cancel',
          confirm: 'Apply scope'
        }
      },
      newChat: 'Start a new conversation',
      recent: {
        title: 'Recent conversations',
        deleteAria: 'Delete conversation: {title}',
        empty: 'No conversations yet',
        emptyHint: 'Start with a question'
      },
      list: {
        searchPlaceholder: 'Search conversation titles',
        searchAria: 'Search conversations by title',
        searchClear: 'Clear search',
        total: '{total} conversation | {total} conversations',
        totalFiltered: '{total} match | {total} matches',
        matched: '{total} conversation | {total} conversations',
        loadMore: 'Load more',
        loadingMore: 'Loading…',
        loadFailed: 'Could not load the conversation list. Please retry.',
        loadMoreFailed: 'Could not load more conversations. Please retry.',
        retry: 'Retry',
        rename: 'Rename',
        renameAria: 'Rename conversation: {title}',
        renameTitle: 'Rename conversation',
        renamePlaceholder: 'Enter a new title',
        renameHint: '1–200 characters. Leading and trailing spaces are trimmed on save.',
        renameEmpty: 'The title cannot be empty.',
        renameTooLong: 'The title cannot exceed 200 characters.',
        renameFailed: 'Rename failed; the title is unchanged.',
        pin: 'Pin',
        unpin: 'Unpin',
        pinAria: 'Pin conversation: {title}',
        unpinAria: 'Unpin conversation: {title}',
        pinned: 'Pinned',
        pinFailed: 'Could not update the pin state; the list is unchanged.',
        metadataFailed: 'Could not update the conversation. Please retry.',
        copyAnswer: 'Copy answer',
        copyAnswerAria: 'Copy this answer',
        copyCode: 'Copy code',
        copyCodeAria: 'Copy code block',
        copied: 'Copied',
        copyFailed: 'Copy failed. Please select the text and copy it manually.',
        retrySend: 'Ask again',
        retryAria: 'Ask the same question again with the same settings',
        retryBlocked: 'The original request is still processing or already succeeded, so nothing was resent.',
        retryUnknown: 'The original result could not be confirmed, so the retry was stopped. Check the result later.',
        retryUnavailable: 'The original question or document scope is no longer available. Copy the question and send it manually.',
        retryMismatch: 'The document scope or mode differs from the original request, so nothing was resent to avoid changing the sources.',
        cancel: 'Cancel',
        save: 'Save'
      },
      head: {
        freeChat: 'Chat freely with the AI assistant',
        selectKb: 'Choose a knowledge base to get started',
        messageCount: '{count} message | {count} messages'
      },
      empty: {
        selectKb: 'Choose a knowledge base for answers backed by sources.',
        noKb: 'Create a knowledge base and add documents first, or switch to free chat.',
        choose: 'Choose a knowledge base or chat mode',
        goKnowledge: 'Go to knowledge bases'
      },
      messagesAria: 'Conversation messages',
      welcome: {
        eyebrow: 'KNOWLEDGE AT YOUR FINGERTIPS',
        title: 'What would you like to know today?',
        ragLead: 'Find clues in your documents so every answer can be traced.',
        ragKb: 'About "{kb}", you could ask:',
        chatLead: 'Draft, think things through, or explore a new idea.',
        chatSub: 'Start with what you care about and chat with the AI assistant.'
      },
      suggestions: {
        ragContents: 'What does this knowledge base cover?',
        ragContentsTitle: 'Get the overview',
        ragSummary: 'Summarize the key information',
        ragSummaryTitle: 'Key points',
        ragProcess: 'What are the main workflows?',
        ragProcessTitle: 'Find workflows',
        chatIntro: 'Tell me about yourself',
        chatIntroTitle: 'Meet the assistant',
        chatEmail: 'Help me write a leave request email',
        chatEmailTitle: 'Writing help',
        chatVector: 'Explain vector databases',
        chatVectorTitle: 'Explain a concept'
      },
      author: {
        you: 'You',
        assistant: 'AI assistant'
      },
      generating: 'Generating',
      phases: { retrieving: 'Searching', generating: 'Generating', saving: 'Saving', recovering: 'Checking the original request' },
      truncated: 'The answer reached the model output limit and may be incomplete. No additional model call was made automatically.',
      recovery: {
        check: 'Check request result', noResubmit: 'Reads the original result only; it does not generate another answer.',
        processing: 'The original request is still processing. Check again later rather than submitting it again.',
        cancelled: 'The original request was stopped before a complete answer was saved.', failed: 'The original request did not complete or save a complete answer.',
        unavailable: 'The result is unavailable, the conversation was deleted, or access has changed.',
        failedCheck: 'The original result could not be confirmed. Reload when the connection recovers; generation will not restart automatically.',
        checking: 'Checking the original request…',
        checkAgain: 'Check again',
        cancel: 'Cancel original request',
        lockedHint: 'The original request is still processing: you cannot send a new question or clear recovery until it finishes or you explicitly stop it.'
      },
      project: {
        label: 'Project snapshot',
        all: 'Use the project’s ready snapshot',
        choose: 'Choose snapshot',
        change: 'Switch snapshot',
        switched: 'Project snapshot switched',
        switching: 'Switching snapshot…',
        noReady: 'No ready snapshot for this project',
        loadFailed: 'Failed to load project snapshots',
        busy: 'The conversation is busy (409). Refreshed to the latest snapshot; try again shortly.',
        stale: 'The snapshot binding changed (409). Refreshed to the latest snapshot; choose again.',
        nonProject: 'This conversation is not a project conversation; its snapshot cannot be switched.',
        denied: 'No access to that snapshot, or it is no longer available.',
        revoked: 'The project source was revoked (410); the snapshot cannot be switched.',
        lockedHint: 'The snapshot cannot change while generating or saving.',
        snapshotLabel: 'Snapshot {id}',
        lines: 'lines {start}–{end}',
        line: 'line {line}',
        symbol: 'Symbol {name}'
      },
      thinking: {
        rag: 'Searching and generating an answer…',
        chat: 'Generating an answer…'
      },
      citations: {
        title: 'Sources',
        untitled: 'Untitled document',
        chunk: 'Chunk {index}',
        score: 'Relevance {percent}%',
        view: 'View source'
      },
      source: {
        title: 'Source text', loading: 'Verifying and loading source…', unavailable: 'The source has changed or is unavailable. Please search again.',
        noPosition: 'This older index has no page or section metadata. Source text is shown below; reindex to add locations.',
        version: 'Index version {version}', previewPdf: 'Preview original PDF', previewPdfAtPage: 'Preview PDF at page {page}',
        pdfTarget: 'Target: physical page {page} of the original PDF. Printed page labels may differ.',
        pdfPageUnknown: 'The source page could not be verified. Preview starts at the first page; reindex to add page locations.',
        truncated: 'The preview was shortened because it is too long.',
        page: 'Page {index}', pages: 'Pages {start}–{end}', sheet: 'Sheet: {name}',
        row: 'Table row {index}', rows: 'Table rows {start}–{end}', line: 'Text line {index}', lines: 'Text lines {start}–{end}'
      },
      memory: {
        label: 'Conversation summary', view: 'View summary', reset: 'Reset summary', resetTitle: 'Reset summary', resetConfirm: 'Reset the summary compression for this conversation? After reset it can no longer be used as historical context.', resetDone: 'Summary reset.', resetFailed: 'Reset failed. Please try again.', resetConflict: 'The summary is updating or its version changed; refreshed to the latest state.', refresh: 'Refresh',
        disabled: 'Summary not enabled', idle: 'No summary yet', pending: 'Summarizing, covered up to turn {covered}', ready: 'Summary covers up to turn {covered}', readyLag: 'Covers up to turn {covered}, more history pending', readyUnknown: 'Covers up to turn {covered}, pending status unknown', failed: 'Summary update failed',
        viewTitle: 'Conversation summary details', covered: 'Covered through turn', lag: 'Unsummarized history exists', truncated: 'Last generation truncated', version: 'Summary version', updatedAt: 'Updated at', yes: 'Yes', no: 'No', unknown: 'Unknown', loading: 'Loading…', unavailable: 'Summary status is temporarily unavailable. Check back later.',
        nonLossless: 'The summary is a lossy compression of earlier history, for reference only. It is not lossless memory and does not replace the original messages.',
        structuredText: 'Structured summary'
      },
      requestStatus: { processing: 'Request in progress', failed: 'Request incomplete', cancelled: 'Stopped' },
      composer: {
        aria: 'Enter your question',
        placeholderRag: 'Ask your knowledge base and let the documents guide the answer…',
        placeholderChat: 'Type your question to start the conversation…',
        topk: 'Chunks',
        topkAria: 'Number of chunks to retrieve',
        hintSend: 'to send',
        hintNewline: 'for a new line',
        stop: 'Stop',
        stopping: 'Stopping',
        send: 'Send',
        disclaimerRag: 'Answers are AI-generated. Verify key details against the cited sources.',
        disclaimerChat: 'Answers are AI-generated. Please verify important information.'
      },
      deleteConfirm: 'Delete this conversation?',
      emptyReply: {
        stopped: '(Generation stopped)',
        rag: 'Sorry, the model returned no content. Try again or reduce the number of chunks.',
        chat: 'Sorry, the model returned no content. Please try again.'
      }
    }
  }
}
