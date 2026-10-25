/** 传输层只报告稳定错误码，由调用处按当前界面语言展示。 */
export class StreamProtocolError extends Error {
  readonly code: 'invalidStream' | 'streamInterrupted'
  constructor(code: 'invalidStream' | 'streamInterrupted') {
    super(code === 'invalidStream' ? '流式响应格式异常，请重试' : '回答连接提前中断，请重试')
    this.name = 'StreamProtocolError'
    this.code = code
  }
}

/** 与具体页面解耦的流式事件读取；明确区分业务结束和意外断流。 */
export async function consumeEventStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: string, data: any) => void,
  signal?: AbortSignal
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let terminal = false

  const dispatch = (frame: string) => {
    let event = 'message'
    const lines: string[] = []
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) lines.push(line.slice(5).trimStart())
    }
    if (!lines.length) return // SSE 注释/心跳
    let data: any
    try {
      data = JSON.parse(lines.join('\n'))
    } catch {
      throw new StreamProtocolError('invalidStream')
    }
    if (event === 'done' || event === 'error') terminal = true
    onEvent(event, data)
  }

  try {
    while (!terminal) {
      signal?.throwIfAborted()
      const { done, value } = await reader.read()
      if (done) {
        buffer += decoder.decode()
        if (buffer.trim()) dispatch(buffer)
        if (!terminal) throw new StreamProtocolError('streamInterrupted')
        break
      }
      buffer += decoder.decode(value, { stream: true })
      let boundary: RegExpExecArray | null
      while (!terminal && (boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        dispatch(frame)
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}
