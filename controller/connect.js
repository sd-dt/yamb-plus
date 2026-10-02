'use strict'
/*
 * 消息互联模块（QQ 好友 ↔ MC 服务器公屏）
 *
 *   POST /connect { qq, action:start|stop|status, username? }
 *        start 且没有用户名 → 回 { ok:true, need_username:true }
 *        start 且给了用户名 → 校验并登记（2~16 个中英文/数字/下划线，不能有空格冒号）
 *        stop → 关闭；status → 查状态
 *        状态存 controller/connect.json（重启不丢）
 *
 *   POST /mcsay { qq, text }
 *        · 只有已开启的用户能用
 *        · 以 / 或 # 开头 → 直接拒绝（不允许发指令）
 *        · 空内容 / 超长 → 拒绝
 *        · 通过 → 拼成「用户名：text」发到游戏公屏（依次试各挂机号，谁在线用谁）
 *
 *   后台推送：每 2 秒读一次 yamb.log 的末尾，取【上一次之后的新行】
 *        · 解析 [MC:chat] 名字: 内容  和  [MC:system] <名字> 内容
 *        · 过滤：系统消息（解析不出来的自然被丢）、我们 4 个号、TSL/XMCBot 等 bot
 *        · 同批内按「玩家+内容」去重（双号同时挂时同一条会被记两遍）
 *        · 推给每个已开启的用户：「玩家名：消息」（不带时间）
 *
 * 本文件可以单独跑自测：node connect.js --selftest
 */

const fs = require('fs')
const http = require('http')
const { execFileSync } = require('child_process')

const { env, envAbs, repoPath } = require('../lib/env')
const CONNECT_FILE = __dirname + '/connect.json'
const YAMB_LOG = envAbs('YAMB_DIR', repoPath('yamb')) + '/yamb.log'
const RELAY_MS = 2000
const MAX_LEN = 120
const TAIL_LINES = 2000
const NAME_RE = /^[0-9A-Za-z_\u4e00-\u9fa5]{2,16}$/

const ACCOUNTS = require('../lib/fleet').ACCOUNTS
const BLOCK_DEFAULT = ['TSL', 'XMCBot01', 'XMCBot02', 'Server', 'CONSOLE', 'SYS', 'Bot']
const NAPCAT = { host: env('NAPCAT_HOST', '127.0.0.1'), port: env.int('NAPCAT_PRIMARY_PORT', 3000), token: env.require('NAPCAT_TOKEN') }

// ── 状态 ────────────────────────────────────────────────
function loadState () {
  try {
    const d = JSON.parse(fs.readFileSync(CONNECT_FILE, 'utf8'))
    return d && typeof d === 'object' ? d : {}
  } catch (e) { return {} }
}
function saveState (s) {
  try { fs.writeFileSync(CONNECT_FILE, JSON.stringify(s, null, 2)) } catch (e) {}
}
function entryOf (s, qq) {
  if (!s[qq] || typeof s[qq] !== 'object') s[qq] = { enabled: false, username: '' }
  return s[qq]
}

// ── 解析一行日志 ────────────────────────────────────────
function parseLine (line) {
  if (!line) return null
  let m = /\[MC:chat\]\s*([^:]{1,24}):\s*(\S.*)$/.exec(line)
  if (m) return { player: m[1].trim(), text: m[2].trim() }
  m = /\[MC:system\]\s*<([^>]{1,24})>\s*(\S.*)$/.exec(line)
  if (m) return { player: m[1].trim(), text: m[2].trim() }
  return null
}

function isBlocked (player, extra) {
  if (!player) return true
  if (ACCOUNTS.indexOf(player) >= 0) return true
  if (BLOCK_DEFAULT.indexOf(player) >= 0) return true
  if (Array.isArray(extra) && extra.indexOf(player) >= 0) return true
  return false
}

// ── 发消息 ──────────────────────────────────────────────
function httpPost (host, port, path, obj, headers) {
  return new Promise((resolve) => {
    const payload = JSON.stringify(obj)
    const h = Object.assign({ 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }, headers || {})
    const req = http.request({ host: host, port: port, path: path, method: 'POST', headers: h }, (res) => {
      let d = ''
      res.on('data', (c) => { d += c })
      res.on('end', () => resolve(d))
    })
    req.on('error', (e) => resolve('ERR ' + e.message))
    req.setTimeout(8000, () => req.destroy(new Error('timeout')))
    req.write(payload)
    req.end()
  })
}

function sendPrivate (qq, text) {
  return httpPost(NAPCAT.host, NAPCAT.port, '/send_private_msg',
    { user_id: Number(qq), message: text },
    { Authorization: 'Bearer ' + NAPCAT.token })
}

// ── 后台推送 ────────────────────────────────────────────
let lastTotal = -1

function readTail () {
  const raw = execFileSync('tail', ['-n', String(TAIL_LINES), YAMB_LOG], { timeout: 6000 }).toString('utf8')
  const lines = raw.split('\n')
  if (lines.length && lines[lines.length - 1] === '') lines.pop()
  const wc = execFileSync('wc', ['-l', YAMB_LOG], { timeout: 6000 }).toString('utf8')
  const total = parseInt(String(wc).trim().split(/\s+/)[0], 10) || 0
  return { lines: lines, total: total }
}

function pollOnce (log) {
  let d
  try { d = readTail() } catch (e) { return 0 }
  const lines = d.lines
  const total = d.total

  // 首次运行 / 日志被轮转：只对齐游标，不补发旧消息
  if (lastTotal < 0 || total < lastTotal) { lastTotal = total; return 0 }

  const startIdx = total - lines.length + 1 // lines[0] 在文件里的行号（1-based）
  const fresh = []
  for (let i = 0; i < lines.length; i++) {
    const idx = startIdx + i
    if (idx <= lastTotal) continue
    const p = parseLine(lines[i])
    if (!p) continue
    if (isBlocked(p.player)) continue
    fresh.push(p)
  }
  lastTotal = total

  const state = loadState()
  const users = Object.keys(state).filter((q) => state[q] && state[q].enabled)
  if (!users.length || !fresh.length) return 0

  // 同批内跨号去重（两个挂机号会各记一遍同一条公屏）
  const seen = new Set()
  const out = []
  for (const p of fresh) {
    const key = p.player + '\u0000' + p.text
    if (seen.has(key)) continue
    seen.add(key)
    out.push(p)
  }

  for (const qq of users) {
    for (const p of out) {
      sendPrivate(qq, p.player + '：' + p.text)
    }
  }
  if (log) log('互联推送：' + out.length + ' 条 -> ' + users.length + ' 人')
  return out.length
}

function startRelay (log) {
  lastTotal = -1
  setInterval(() => {
    try { pollOnce(log) } catch (e) { if (log) log('互联推送异常: ' + e.message) }
  }, RELAY_MS)
  if (log) log('消息互联已启动（每 ' + (RELAY_MS / 1000) + ' 秒检查一次）')
}

// ── HTTP 处理 ───────────────────────────────────────────
function handleConnect (res, body, send, log) {
  const qq = String(body.qq || '').trim()
  const action = String(body.action || 'status').trim()
  if (!/^\d{5,12}$/.test(qq)) return send(res, 400, { ok: false, message: 'qq 参数不合法' })
  const s = loadState()
  const e = entryOf(s, qq)

  if (action === 'start') {
    const uname = String(body.username || '').trim() || e.username
    if (!uname) return send(res, 200, { ok: true, need_username: true, enabled: false, message: '请告诉我你要用的用户名' })
    if (!NAME_RE.test(uname)) return send(res, 200, { ok: false, message: '用户名要 2~16 个中英文/数字/下划线，不能有空格或冒号' })
    e.username = uname
    e.enabled = true
    saveState(s)
    if (log) log('互联开启：' + qq + ' -> ' + uname)
    return send(res, 200, { ok: true, enabled: true, username: uname, message: '已开启互联，游戏里说话会显示「' + uname + '：…」' })
  }
  if (action === 'stop') {
    e.enabled = false
    saveState(s)
    if (log) log('互联关闭：' + qq)
    return send(res, 200, { ok: true, enabled: false, username: e.username, message: '已关闭互联' })
  }
  return send(res, 200, { ok: true, enabled: !!e.enabled, username: e.username })
}

async function handleSay (res, body, send, log, deps) {
  const qq = String(body.qq || '').trim()
  const text = String(body.text || '').trim()
  const s = loadState()
  const e = s[qq]
  if (!e || !e.enabled) return send(res, 200, { ok: false, message: '你还没开启互联（!connect start）' })
  if (!text) return send(res, 200, { ok: false, message: '内容为空' })
  if (/^[/#]/.test(text)) return send(res, 200, { ok: false, message: '不允许发送指令（不能以 / 或 # 开头）' })
  if (text.length > MAX_LEN) return send(res, 200, { ok: false, message: '太长了（上限 ' + MAX_LEN + ' 字）' })
  const full = e.username + '：' + text
  for (const acc of deps.accounts) {
    let r
    try { r = await deps.yambSay(acc, full) } catch (err) { r = null }
    if (r && (r.success === true || r.ok === true)) {
      if (log) log('互联发送（' + acc + '）：' + full.slice(0, 80))
      return send(res, 200, { ok: true, message: '已发送：' + full })
    }
  }
  return send(res, 200, { ok: false, message: '发送失败：没有可用的挂机号（可能都没在线）' })
}

module.exports = {
  handleConnect: handleConnect,
  handleSay: handleSay,
  startRelay: startRelay,
  pollOnce: pollOnce,
  parseLine: parseLine,
  isBlocked: isBlocked,
  NAME_RE: NAME_RE
}

// ── 自测：node connect.js --selftest ────────────────────
if (require.main === module && process.argv.indexOf('--selftest') >= 0) {
  const samples = [
    ['[Bot:bot1][MC:chat] 玩家甲: 润！', '玩家甲', '润！'],
    ['[Bot:bot1][MC:chat] 玩家乙: 测试消息', '玩家乙', '测试消息'],
    ['[Bot:bot1][MC:chat] 玩家丙: 你亲了 bot1 一口~ ♥', '玩家丙', '你亲了 bot1 一口~ ♥'],
    ['[Bot:bot1][MC:system] <玩家乙> 测试', '玩家乙', '测试'],
    ['[Bot:bot1][MC:system] ＋ 玩家丁 加入了服务器', null, null],
    ['[Bot:bot1][MC:system] [玩家丙] 你亲了 bot1 一口~ ♥', null, null],
    ['[Bot:bot1][MC:join] xxx', null, null],
    ['[Bot:bot1][MC:leave] xxx', null, null],
    ['[Bot:bot1][MC:system] － 玩家戊 离开了服务器', null, null]
  ]
  let fail = 0
  for (const [line, wantP, wantT] of samples) {
    const got = module.exports.parseLine(line)
    const ok = wantP === null ? got === null : (got && got.player === wantP && got.text === wantT)
    if (!ok) { fail++; console.log('  ✗ ' + line + '  -> ' + JSON.stringify(got)) } else { console.log('  ✓ ' + line.slice(0, 60)) }
  }

  const cases = [
    ['Wqingqing', false], ['fana114514', false], ['TSL', true], ['XMCBot01', true],
    ['bot1', true], ['bot2', true], ['bot3', true], ['bot4', true], ['', true]
  ]
  for (const [name, want] of cases) {
    const got = module.exports.isBlocked(name)
    if (got !== want) { fail++; console.log('  ✗ 过滤 ' + JSON.stringify(name) + ' 期望 ' + want + ' 得到 ' + got) } else { console.log('  ✓ 过滤 ' + JSON.stringify(name) + ' = ' + got) }
  }

  const names = [['ab', true], ['a', false], ['bot1', true], ['玩家甲', true], ['has space', false], ['has:colon', false], ['x'.repeat(17), false], ['玩家123_', true]]
  for (const [n, want] of names) {
    const got = module.exports.NAME_RE.test(n)
    if (got !== want) { fail++; console.log('  ✗ 用户名 ' + JSON.stringify(n) + ' 期望 ' + want + ' 得到 ' + got) } else { console.log('  ✓ 用户名 ' + JSON.stringify(n) + ' = ' + got) }
  }

  console.log(fail === 0 ? '\n自测全部通过 ✓' : '\n自测失败 ' + fail + ' 项 ✗')
  process.exit(fail === 0 ? 0 : 1)
}
