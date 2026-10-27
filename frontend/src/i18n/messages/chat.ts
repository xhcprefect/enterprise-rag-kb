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
        failedCheck: '暂时无法确认原请求结果。网络恢复后可再次检查，系统不会自动重新生成。'
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
        failedCheck: 'The original result could not be confirmed. Check again when the connection recovers; generation will not restart automatically.'
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
