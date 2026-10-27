/**
 * 异步结果的"代次"守卫。
 *
 * 会话详情、生成回答这类请求都是异步的，落地前用户可能已经切到别的会话、
 * 新建会话或直接发了新问题。每次开始一个会改写界面的动作就 begin() 一次，
 * 异步结果落地前用 isCurrent(token) 确认自己仍是最新动作；过期的结果丢弃，
 * 避免"慢的旧详情"覆盖"已经开始的生成"。
 *
 * 不依赖 vue/api，node 测试可直接运行。
 */
export interface SessionEpoch {
  /** 开始一个新动作并返回它的代次；此前所有未落地的结果全部作废。 */
  begin(): number
  /** 该代次是否仍是最新（期间没有更新的动作开始）。 */
  isCurrent(token: number): boolean
  /** 当前代次；仅供调试与断言。 */
  current(): number
}

export function createSessionEpoch(initial = 0): SessionEpoch {
  let epoch = initial
  return {
    begin() {
      epoch += 1
      return epoch
    },
    isCurrent(token: number) {
      return token === epoch
    },
    current() {
      return epoch
    }
  }
}
