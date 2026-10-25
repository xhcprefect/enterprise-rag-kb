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
      thinking: {
        rag: '正在检索并生成回答…',
        chat: '正在生成回答…'
      },
      citations: {
        title: '引用来源',
        untitled: '未命名文档',
        chunk: '第 {index} 段',
        score: '相似度 {percent}%'
      },
      composer: {
        aria: '输入问题',
        placeholderRag: '向知识库提问，让资料为你提供线索…',
        placeholderChat: '输入你的问题，开始对话…',
        topk: '检索片段',
        topkAria: '检索片段数',
        hintSend: '发送',
        hintNewline: '换行',
        stop: '停止',
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
      thinking: {
        rag: 'Searching and generating an answer…',
        chat: 'Generating an answer…'
      },
      citations: {
        title: 'Sources',
        untitled: 'Untitled document',
        chunk: 'Chunk {index}',
        score: 'Relevance {percent}%'
      },
      composer: {
        aria: 'Enter your question',
        placeholderRag: 'Ask your knowledge base and let the documents guide the answer…',
        placeholderChat: 'Type your question to start the conversation…',
        topk: 'Chunks',
        topkAria: 'Number of chunks to retrieve',
        hintSend: 'to send',
        hintNewline: 'for a new line',
        stop: 'Stop',
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
