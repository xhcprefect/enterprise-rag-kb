export default {
  'zh-CN': {
    documents: {
      backAria: '返回知识库列表',
      eyebrow: 'DOCUMENTS',
      title: '文档管理',
      subtitle: '上传文档后系统会自动解析、切分并向量化入库',
      upload: '上传文档',
      formatsLabel: '支持格式',
      formatsTip: '大文件分片上传，可暂停、可断点续传',
      resumeAction: '继续上传',
      retry: '重试',
      skip: '跳过',
      cancelCurrent: '取消当前',
      cancelAll: '全部取消',
      dedupBadge: '已复用',
      resumeNotice:
        '检测到未完成的上传「{name}」（已传 {percent}%）。点击右上角「{upload}」重新选择同一文件即可从断点继续。',
      discardResume: '放弃该上传',
      uploadAria: '上传进度',
      pause: '暂停',
      cancelUpload: '取消上传',
      cancelIngest: '取消入库',
      queued: '排队中',
      hint: {
        hashing: '正在计算文件摘要，确保续传的是同一文件…',
        completing: '分片已传完，服务端正在校验合并，请稍候…',
        uploading: '正在上传… {percent}%',
        paused: '已暂停（{percent}%），可点击「继续上传」，或稍后重新选择同一文件续传',
        queued: '文件已上传，正在排队等待解析…',
        ingesting: '文件已上传，正在解析并向量化入库… {percent}%',
        done: '入库完成',
        cancelled: '已取消',
        failed: '上传或入库失败，可在列表中查看原因',
        reusedIngesting: '该文件与库中已有文档内容相同，正在复用其入库结果…',
        reusedNeedsReindex: '该文件复用了库中一份失败或已取消的文档，状态保持不变；如需入库请点击「重建索引」'
      },
      queue: {
        title: '等待上传：{count} 个文件',
        notPersisted: '等待队列中的文件仅保存在当前页面，刷新后需重新选择',
        remove: '移出队列',
        duplicateSkipped: '已跳过 {count} 个重复文件（如「{name}」），它们已在队列中'
      },
      chunks: '{done} / {total} 片',
      listTitle: '文档列表',
      searchPlaceholder: '搜索文件名',
      searchAria: '按文件名搜索文档',
      filterPlaceholder: '全部状态',
      filterAria: '按状态筛选文档',
      status: {
        pending: '排队中',
        processing: '处理中',
        ready: '已就绪',
        failed: '失败',
        cancelled: '已取消'
      },
      table: {
        filename: '文件名',
        size: '大小',
        status: '状态',
        chunks: '分块',
        chars: '字数',
        uploadedAt: '上传时间',
        actions: '操作'
      },
      failedFallback: '处理失败',
      cancelledTip: '已取消入库，可重新上传或重建索引',
      rebuild: '重建索引',
      empty: '还没有文档，点击右上角「{upload}」开始',
      confirm: {
        cancelIngestTitle: '取消入库',
        cancelIngest: '确定取消「{name}」的入库吗？已写入的向量数据会被清理。',
        remove: '确定删除「{name}」及其向量数据吗？'
      },
      toast: {
        discarded: '已放弃未完成的上传',
        paused: '已暂停，可稍后重新选择同一文件继续',
        cancelled: '已取消上传',
        cancelSubmitted: '取消请求已提交，任务将在当前批次结束后停止',
        reindexQueued: '已加入处理队列，请确认 worker 正在运行',
        reindexFailed: '重建索引失败，请稍后重试',
        ingestDone: '入库完成',
        ingestFailed: '入库失败，可在列表中查看原因',
        uploadFailed: '上传失败，可重试或稍后继续',
        cancelFailed: '取消入库失败，请稍后重试',
        removeFailed: '删除失败，请稍后重试',
        listFailed: '文档列表加载失败，请刷新重试',
        documentGone: '该文档已被删除，已停止等待其入库结果',
        cancelledAll: '已取消当前上传并清空等待队列',
        reused: '该文件内容与库中已有文档相同，已直接复用，不会重复入库',
        reusedFailed: '该文件复用了库中一份入库失败的文档，状态保持不变；请点击「重建索引」显式重新入库',
        reusedCancelled: '该文件复用了库中一份已取消的文档，状态保持不变；请点击「重建索引」显式重新入库'
      },
      busy: '已有文件正在处理中，请等待完成或先取消',
      resumeMismatch: '未完成的上传是「{name}」，请选择同一文件继续，或先放弃该上传'
    }
  },
  'en-US': {
    documents: {
      backAria: 'Back to knowledge bases',
      eyebrow: 'DOCUMENTS',
      title: 'Documents',
      subtitle: 'Uploaded documents are parsed, split, and indexed automatically',
      upload: 'Upload document',
      formatsLabel: 'Supported formats',
      formatsTip: 'Large files upload in chunks. You can pause and resume anytime.',
      resumeAction: 'Resume upload',
      retry: 'Retry',
      skip: 'Skip',
      cancelCurrent: 'Cancel current',
      cancelAll: 'Cancel all',
      dedupBadge: 'Reused',
      resumeNotice:
        'An unfinished upload of "{name}" was found ({percent}% uploaded). Choose the same file again with "{upload}" to resume from where it stopped.',
      discardResume: 'Discard upload',
      uploadAria: 'Upload progress',
      pause: 'Pause',
      cancelUpload: 'Cancel upload',
      cancelIngest: 'Cancel indexing',
      queued: 'Queued',
      hint: {
        hashing: 'Computing the file checksum to verify it is the same file…',
        completing: 'All chunks are uploaded. The server is verifying and merging them…',
        uploading: 'Uploading… {percent}%',
        paused: 'Paused at {percent}%. Use "Resume upload", or select the same file again later.',
        queued: 'Upload complete. Waiting in the queue to be parsed…',
        ingesting: 'Upload complete. Parsing and indexing… {percent}%',
        done: 'Indexing complete',
        cancelled: 'Cancelled',
        failed: 'Upload or indexing failed. Check the document list for details.',
        reusedIngesting: 'This file matches a document already in this knowledge base. Reusing its indexing result…',
        reusedNeedsReindex:
          'This file matched an existing document that failed or was cancelled; its status is unchanged. Use "Rebuild index" to index it explicitly.'
      },
      queue: {
        title: 'Waiting to upload: {count} file(s)',
        notPersisted: 'Files waiting in the queue live only on this page. Select them again after a refresh.',
        remove: 'Remove',
        duplicateSkipped: 'Skipped {count} duplicate file(s) already in the queue (e.g. "{name}")'
      },
      chunks: '{done} / {total} chunks',
      listTitle: 'Documents',
      searchPlaceholder: 'Search file names',
      searchAria: 'Search documents by file name',
      filterPlaceholder: 'All statuses',
      filterAria: 'Filter documents by status',
      status: {
        pending: 'Queued',
        processing: 'Processing',
        ready: 'Ready',
        failed: 'Failed',
        cancelled: 'Cancelled'
      },
      table: {
        filename: 'File name',
        size: 'Size',
        status: 'Status',
        chunks: 'Chunks',
        chars: 'Characters',
        uploadedAt: 'Uploaded',
        actions: 'Actions'
      },
      failedFallback: 'Processing failed',
      cancelledTip: 'Indexing cancelled. Upload the file again or rebuild the index.',
      rebuild: 'Rebuild index',
      empty: 'No documents yet. Use "{upload}" to get started.',
      confirm: {
        cancelIngestTitle: 'Cancel indexing',
        cancelIngest: 'Cancel indexing of "{name}"? Vector data already written will be removed.',
        remove: 'Delete "{name}" and its vector data?'
      },
      toast: {
        discarded: 'Unfinished upload discarded',
        paused: 'Paused. Select the same file later to resume.',
        cancelled: 'Upload cancelled',
        cancelSubmitted: 'Cancellation requested. The task will stop after the current batch.',
        reindexQueued: 'Added to the processing queue. Make sure the worker is running.',
        reindexFailed: 'Failed to rebuild the index. Please try again.',
        ingestDone: 'Indexing complete',
        ingestFailed: 'Indexing failed. Check the document list for details.',
        uploadFailed: 'Upload failed. You can retry or resume later.',
        cancelFailed: 'Failed to cancel indexing. Please try again.',
        removeFailed: 'Failed to delete. Please try again.',
        listFailed: 'Failed to load documents. Refresh to try again.',
        documentGone: 'This document was deleted, so waiting for its indexing result has stopped.',
        cancelledAll: 'Cancelled the current upload and cleared the waiting queue',
        reused: 'This file is identical to a document already in this knowledge base. It was reused instead of indexed again.',
        reusedFailed:
          'This file matched an existing document that previously failed. Its status is unchanged; use "Rebuild index" to index it explicitly.',
        reusedCancelled:
          'This file matched an existing document whose indexing was cancelled. Its status is unchanged; use "Rebuild index" to index it explicitly.'
      },
      busy: 'Another file is being processed. Wait for it to finish or cancel it first.',
      resumeMismatch:
        'The unfinished upload is "{name}". Select the same file to resume, or discard that upload first.'
    }
  }
}
