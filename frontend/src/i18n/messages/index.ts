import common from './common.ts'
import login from './login.ts'
import chat from './chat.ts'
import account from './account.ts'
import knowledge from './knowledge.ts'
import documents from './documents.ts'
import usage from './usage.ts'

export const messages = {
  'zh-CN': { ...common['zh-CN'], ...login['zh-CN'], ...chat['zh-CN'], ...account['zh-CN'], ...knowledge['zh-CN'], ...documents['zh-CN'], ...usage['zh-CN'] },
  'en-US': { ...common['en-US'], ...login['en-US'], ...chat['en-US'], ...account['en-US'], ...knowledge['en-US'], ...documents['en-US'], ...usage['en-US'] }
}
