import { sha256 } from '@noble/hashes/sha2.js'

/** 分块读取完整文件计算 SHA-256，内存不随文件大小增长。 */
export async function hashFile(file: Blob, signal?: AbortSignal): Promise<string> {
  const hash = sha256.create()
  const blockSize = 4 * 1024 * 1024
  for (let start = 0; start < file.size; start += blockSize) {
    signal?.throwIfAborted()
    const bytes = await file.slice(start, start + blockSize).arrayBuffer()
    signal?.throwIfAborted()
    hash.update(new Uint8Array(bytes))
  }
  signal?.throwIfAborted()
  return Array.from(hash.digest(), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/**
 * 入库进度为 0 的含义是「worker 还没领取任务 / 还没落第一批分块」，
 * 而不是「进度为零」。这种情况应当显示不确定态（排队中），
 * 而不是一根 0% 的定长条——后者看起来像卡死了。
 */
export function isIngestQueued(phase: string, percent: number): boolean {
  return phase === 'ingesting' && percent === 0
}
