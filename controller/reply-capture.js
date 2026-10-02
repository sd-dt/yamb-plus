/* reply-capture.js —— 从 yamb.log 抓取「指令发出后的聊天栏回执」
 *
 * 原理：yamb 会把游戏里看到的每条聊天/系统消息写进日志，形如
 *   [Bot:bot1][MC:chat] 玩家甲: 这个指令不可用，尝试联系腐竹解决喵~
 *   [Bot:bot1][MC:system] [玩家甲] 这个指令不可用，尝试联系腐竹解决喵~
 * 所以只要记住「发送前的文件字节偏移」，发完之后盯新增内容即可。
 *
 * 不需要修改 yamb 源码。
 */
const fs = require('fs')

const { envAbs, repoPath } = require('../lib/env')
const LOG = envAbs('YAMB_DIR', repoPath('yamb')) + '/yamb.log'

const CHAT_RE = /\[MC:chat\]\s*([^:]*):\s*(.*)$/
const SYS_RE = /\[MC:system\]\s*(.*)$/

// 这些是噪音，不要转发到 QQ
const NOISE = [
  /加入了服务器/, /离开了服务器/,
  /\[MC:join\]/, /\[MC:leave\]/, /\[MC:death\]/, /\[MC:respawn\]/,
  /\[Command\]/, /\[rp-fix\]/, /\[keepalive\]/, /\[Standby\]/,
  /\[DB\]/, /\[Brew\]/, /\[AstrBot\]/, /\[Config\]/, /\[Main\]/,
  /Resource pack/i, /Minecraft client/, /AstrBot API/i
]

function sleep (ms) { return new Promise((r) => setTimeout(r, ms)) }

/** 当前日志文件大小，作为「从这里开始是新内容」的起点 */
function logSizeNow () {
  try { return fs.statSync(LOG).size } catch (e) { return 0 }
}

/** 从 offset 读到文件末尾 */
function readFrom (offset) {
  let fd
  try {
    const st = fs.statSync(LOG)
    if (st.size <= offset) return { text: '', offset: st.size }
    const len = st.size - offset
    const buf = Buffer.alloc(len)
    fd = fs.openSync(LOG, 'r')
    fs.readSync(fd, buf, 0, len, offset)
    return { text: buf.toString('utf8'), offset: st.size }
  } catch (e) {
    return { text: '', offset: offset }
  } finally {
    if (fd !== undefined) { try { fs.closeSync(fd) } catch (e) { /* 忽略 */ } }
  }
}

/** 从日志片段里提取「值得转发」的消息文本 */
function extract (text, sentText, botName) {
  const out = []
  const sent = String(sentText || '').trim()
  const sentNoSlash = sent.replace(/^\/+/, '')
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    if (NOISE.some((re) => re.test(line))) continue

    let msg = null
    const mc = line.match(CHAT_RE)
    if (mc) {
      const who = mc[1].trim()
      msg = mc[2].trim()
      // 自己刚发出去的那条回声，跳过
      if (who === botName) {
        const m = msg.replace(/^\/+/, '')
        if (m === sent || m === sentNoSlash) continue
      }
    } else {
      const ms = line.match(SYS_RE)
      if (ms) msg = ms[1].trim()
    }
    if (!msg) continue
    if (msg === sent || msg.replace(/^\/+/, '') === sentNoSlash) continue

    // 去掉服务器插件的 [TSL] / [TSLLLLL] 之类前缀，让 QQ 里好看一点
    msg = msg.replace(/^\[[A-Za-z]{2,10}\]\s*/, '').trim()
    if (!msg) continue
    out.push(msg)
  }
  return out
}

/**
 * 抓取回执。
 * @param {number} sinceOffset 发送指令前调用 logSizeNow() 得到的偏移
 * @param {string} sentText    刚发出的文本（用于过滤自己的回声）
 * @param {object} opts        { botName, timeoutMs, maxLines, quietMs }
 * @returns {Promise<string[]>}
 */
async function captureReply (sinceOffset, sentText, opts) {
  const o = opts || {}
  const botName = o.botName || ''
  const timeoutMs = o.timeoutMs || 4000
  const maxLines = o.maxLines || 3
  const quietMs = o.quietMs || 700

  let offset = sinceOffset
  const found = []
  const seen = new Set()
  const deadline = Date.now() + timeoutMs
  let lastAddAt = 0

  while (Date.now() < deadline) {
    await sleep(200)
    const r = readFrom(offset)
    offset = r.offset
    if (r.text) {
      for (const msg of extract(r.text, sentText, botName)) {
        if (seen.has(msg)) continue
        seen.add(msg)
        found.push(msg)
        lastAddAt = Date.now()
      }
    }
    if (found.length >= maxLines) break
    if (found.length > 0 && Date.now() - lastAddAt > quietMs) break
  }
  return found
}

module.exports = { captureReply, logSizeNow }

// ── 追加：从 yamb 的 outgoing.log 抓「机器人自己发出的消息」 ──
// 这样 yamb 的回复可以保持私聊（公屏零输出），回执照样能拿给 QQ 看。
const OUT = envAbs('YAMB_DIR', repoPath('yamb')) + '/outgoing.log'

function outSizeNow () {
  try { return fs.statSync(OUT).size } catch (e) { return 0 }
}

function readOutFrom (offset) {
  let fd
  try {
    const st = fs.statSync(OUT)
    if (st.size <= offset) return { text: "", offset: st.size }
    const len = st.size - offset
    const buf = Buffer.alloc(len)
    fd = fs.openSync(OUT, 'r')
    fs.readSync(fd, buf, 0, len, offset)
    return { text: buf.toString('utf8'), offset: st.size }
  } catch (e) {
    return { text: "", offset: offset }
  } finally {
    if (fd !== undefined) { try { fs.closeSync(fd) } catch (e) {} }
  }
}

// 一行格式： <毫秒> [TAB] <机器人发出的文本>
function parseOutLine (line) {
  const TAB = String.fromCharCode(9)
  const i = line.indexOf(TAB)
  let text = (i >= 0 ? line.slice(i + 1) : line).trim()
  if (!text) return null
  if (text.indexOf("/msg ") === 0) {
    const rest = text.slice(5).trim()
    const sp = rest.indexOf(" ")
    return sp >= 0 ? rest.slice(sp + 1).trim() : null
  }
  if (text.charAt(0) === "/") return null
  return text
}

async function captureOutgoing (sinceOffset, opts) {
  const o = opts || {}
  const timeoutMs = o.timeoutMs || 4000
  const maxLines = o.maxLines || 4
  const quietMs = o.quietMs || 700
  let offset = sinceOffset
  const found = []
  const seen = new Set()
  const deadline = Date.now() + timeoutMs
  let lastAddAt = 0
  while (Date.now() < deadline) {
    await sleep(200)
    const r = readOutFrom(offset)
    offset = r.offset
    if (r.text) {
      for (const raw of r.text.split(String.fromCharCode(10))) {
        const msg = parseOutLine(raw)
        if (!msg || seen.has(msg)) continue
        seen.add(msg)
        found.push(msg)
        lastAddAt = Date.now()
      }
    }
    if (found.length >= maxLines) break
    if (found.length > 0 && Date.now() - lastAddAt > quietMs) break
  }
  return found
}

module.exports.captureOutgoing = captureOutgoing
module.exports.outSizeNow = outSizeNow
