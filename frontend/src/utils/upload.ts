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
