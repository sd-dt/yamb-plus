#!/usr/bin/env node
/* mcbot-controller.js —— QQ 指令 ↔ MC 挂机机器人 的桥
 *
 * 为什么需要它：
 *   AstrBot 跑在 Docker 容器里，既够不到宿主机的 systemd，也读不到 yamb 的配置。
 *   本控制器跑在宿主机上，监听 127.0.0.1 + Docker 网桥网关 IP（不对公网开放），
 *   由 AstrBot 插件通过 HTTP 调用。
 *
 * 端点（除 /health 外全部需要 x-api-key 请求头）：
 *   GET  /health               存活探测（无需密钥）
 *   GET  /status               开关状态 / 启用账号 / 游戏内状态 / 4 个账号概览
 *   POST /switch  {account}    切换账号（下线当前 → 上线目标）
 *   POST /stop                 下线当前 MC bot
 *   POST /start   {account?}   上线（不带参数则沿用上次启用的账号）
 *   POST /command {command}    游戏内执行服务器指令（自动补斜杠）
 *   POST /yamb    {command}    执行 yamb 自己的指令（走 #b 公屏前缀通道）
 *
 * /command 里的特殊子命令（由 QQ 的 !mcbot command 转发进来）：
 *   loop <内容> <间隔tick>     新建循环（1 tick = 50ms，最小 20 tick = 1 秒）
 *   loop list                  列出所有循环
 *   loop del <编号>            删除某条循环
 *   loop clear                 清空所有循环
 *   说明：内容以 / 开头 → 当服务器指令；否则 → 当聊天栏文字。
 *         循环跑在宿主机上，与 QQ/AstrBot 是否在线无关；账号切换后跟着新账号走。
 *
 * /yamb 里的特殊子命令（白名单管理，直接改 config/bots/<账号>.yaml 的 adminList）：
 *   list（或 admins）          查看 4 个 bot 各自的管理员名单
 *   add <游戏名>               给【当前运行】的 bot 加管理员
 *   del <游戏名>               从【当前运行】的 bot 删管理员
 *   alladd <游戏名>            给【全部 4 个 bot】加管理员
 *   alldel <游戏名>            从【全部 4 个 bot】删管理员
 *   reload                     重启 yamb 让白名单改动生效（掉线约 40 秒）
 *   说明：add 类操作会先写文件（持久），再尝试用游戏内指令热加载到正在跑的 bot；
 *         若游戏内不支持热加载，执行 !mcbot yamb reload 重启 yamb 即可生效（掉线约 40 秒）。
 */
const http = require('http')
const net = require('net')
const fs = require('fs')
const path = require('path')
const { execFileSync, execFile, spawn } = require('child_process')
const { captureReply, logSizeNow, captureOutgoing, outSizeNow } = require('./reply-capture')

const PORT = 15100
const API_KEY = process.env.MCBOT_CTRL_KEY || process.env.CTRL_KEY || 'change-me'
const YAMB = '/opt/qqbot/yamb'
const BOTS = path.join(YAMB, 'config', 'bots')
const SWITCH = '/opt/qqbot/mcbot/switch.sh'
const YAMB_API_KEY = process.env.YAMB_API_KEY || 'change-me'
const NAPCAT_TOKEN = process.env.ONEBOT_TOKEN || 'change-me'
const ACCOUNTS = ['sd_dt', '___Fr0g___', '___donk666___', 'Nekoade_MAP']
const PORTS = { sd_dt: 15101, '___Fr0g___': 15102, '___donk666___': 15103, Nekoade_MAP: 15104 }
const ACC_SET = new Set(ACCOUNTS)
// 账号数字别名（顺序由用户指定，注意和 ACCOUNTS 不同）
const ACC_NUM = { '1': 'sd_dt', '2': '___donk666___', '3': '___Fr0g___', '4': 'Nekoade_MAP' }
const NUM_OF = {}
for (const k of Object.keys(ACC_NUM)) NUM_OF[ACC_NUM[k]] = k
// 把 '1'~'4' 或完整账号名归一化成账号名；认不出返回 ''
function accOf (x) {
  const v = String(x == null ? '' : x).trim()
  if (ACC_NUM[v]) return ACC_NUM[v]
  if (ACC_SET.has(v)) return v
  return ''
}

const GAME_CMD_COOLDOWN_MS = 5000
let lastGameCmdAt = 0

// ── 循环指令（loop）相关常量 ─────────────────────────────
const LOOP_FILE = '/opt/qqbot/mcbot/loops.json'
const TICK_MS = 50                 // Minecraft 1 tick = 50ms（20 TPS）
const LOOP_MIN_TICKS = 20          // 最小 1 秒：再短就是刷屏，会被服务器禁言/封号
const LOOP_MAX_TICKS = 72000       // 最大 1 小时
const LOOP_TICKER_MS = 100         // 调度器检查周期
const LOOP_TEXT_MAX = 200
let loops = []
let loopSeq = 0
let loopBusy = false

// ── 转钱（pay）相关常量 _p69 ─────────────────────────────
const PAY_FILE = '/opt/qqbot/astrbot-data/plugins/astrbot_plugin_mcbot/pay.json'
const PAY_STATE_FILE = '/opt/qqbot/astrbot-data/plugins/astrbot_plugin_mcbot/pay-state.json'
const PAY_SCRIPT = '/opt/qqbot/mcbot/pay-daily.js'
const PAY_LOG = '/opt/qqbot/mcbot/pay-run.log'
const PAY_PORT_BASE = 15105
const PAY_AUTH_SCRIPT = '/opt/qqbot/mcbot/ms-device-auth.js'   // _p70: 设备码授权脚本
const PAY_AUTH_DIR = '/opt/qqbot/mcbot/pay-auth'               // _p70: 授权码/完成状态（600）

const ts = () => new Date().toLocaleString('zh-CN', { hour12: false })
const log = (...a) => console.log('[' + ts() + ']', ...a)

// ── 读取 yamb 当前启用的账号 ──────────────────────────────
function enabledAccounts () {
  const out = []
  for (const a of ACCOUNTS) {
    try {
      const txt = fs.readFileSync(path.join(BOTS, a + '.yaml'), 'utf8')
      const m = txt.match(/^enabled:\s*(\S+)/m)
      if (m && m[1] === 'true') out.push(a)
    } catch (e) { /* 文件不存在就跳过 */ }
  }
  return out
}

// 兼容老代码：需要单个账号时取第一个
function enabledAccount () {
  return enabledAccounts()[0] || ''
}

// ── 消息互联（QQ 好友 <-> 服务器公屏）─────────────────────
const connect = require("./connect.js")
const actions = require("./actions.js")
connect.startRelay(log)

// ── 调用 yamb 的本地 API ─────────────────────────────────
function yambCall (account, endpoint, body, timeoutMs) {
  const limit = timeoutMs || 12000
  return new Promise((resolve) => {
    const port = PORTS[account] || (payRead().ports || {})[account] || 0   // _p69: 转钱小号端口回退
    if (!port) return resolve({ success: false, message: '未知账号端口' })
    const payload = body === undefined ? null : JSON.stringify(body)
    const headers = { 'x-api-key': YAMB_API_KEY }
    if (payload) {
      headers['Content-Type'] = 'application/json'
      headers['Content-Length'] = Buffer.byteLength(payload)
    }
    const req = http.request({
      host: '127.0.0.1',
      port: port,
      path: '/api/' + endpoint,
      method: payload ? 'POST' : 'GET',
      headers: headers
    }, (res) => {
      let data = ''
      res.on('data', (c) => { data += c })
      res.on('end', () => {
        try {
          resolve(JSON.parse(data))
        } catch (e) {
          resolve({ success: false, message: 'yamb 返回非 JSON: ' + data.slice(0, 120) })
        }
      })
    })
    req.on('error', (e) => resolve({ success: false, message: 'yamb 无响应: ' + e.message }))
    req.setTimeout(limit, () => { req.destroy(new Error('超时')) })
    if (payload) req.write(payload)
    req.end()
  })
}

function yambSay (account, text) {
  return yambCall(account, 'say', { message: text })
}

// ── 查询某个 NapCat 实例是否已登录（号 A 在 3000，号 B 在 3001）──
function napcatLoginInfo (port, timeoutMs) {
  return new Promise((resolve) => {
    const payload = "{}"
    const req = http.request({
      host: "127.0.0.1", port: port, path: "/get_login_info", method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(payload),
        Authorization: "Bearer " + NAPCAT_TOKEN
      }
    }, (res) => {
      let data = ""
      res.on("data", (c) => { data += c })
      res.on("end", () => {
        try {
          const j = JSON.parse(data)
          resolve(!!(j && j.status === "ok" && j.data && j.data.user_id))
        } catch (e) { resolve(false) }
      })
    })
    req.on("error", () => resolve(false))
    req.setTimeout(timeoutMs || 5000, () => { req.destroy(new Error("timeout")) })
    req.write(payload)
    req.end()
  })
}

// ── 调用 switch.sh ──────────────────────────────────────
// 兜底回执：QQ 侧只认 replies，空 replies 表现出来的就是"没反应"
function receiptHint (text, r) {
  if (!(r && r.success)) return ''
  return '（yamb 已注入「' + text + '」，但 20 秒内没有游戏内回执：可能被白名单/锁定/前缀过滤，或 bot 未进服）'
}

function yambInject (account, sender, text) {
  return yambCall(account, 'command', { command: text, sender: sender })
}

function runSwitch (args, timeoutMs) {
  return new Promise((resolve) => {
    execFile(SWITCH, args, { timeout: timeoutMs || 90000 }, (err, stdout, stderr) => {
      const out = String(stdout || '').trim()
      const errtxt = String(stderr || '').trim()
      if (err) return resolve({ ok: false, message: (errtxt || out || err.message).slice(0, 400) })
      resolve({ ok: true, message: out.slice(0, 400) })
    })
  })
}

// ── yamb 服务是否在跑 ────────────────────────────────────
function serviceActive () {
  try {
    const out = execFileSync('systemctl', ['--user', 'is-active', 'yamb.service'], {
      env: Object.assign({}, process.env, {
        XDG_RUNTIME_DIR: process.env.XDG_RUNTIME_DIR || '/run/user/1000'
      })
    }).toString().trim()
    return out === 'active'
  } catch (e) {
    return false
  }
}

// ── 循环指令（loop）──────────────────────────────────────
function secsHuman (ticks) {
  const s = ticks / 20
  return (Number.isInteger(s) ? s : s.toFixed(1)) + ' 秒'
}
const ticksHuman = (t) => t + ' tick（' + secsHuman(t) + '）'

function loadLoops () {
  loops = []
  loopSeq = 0
  try {
    const j = JSON.parse(fs.readFileSync(LOOP_FILE, 'utf8'))
    const arr = Array.isArray(j) ? j : (Array.isArray(j.loops) ? j.loops : [])
    for (const x of arr) {
      if (!x || !x.text) continue
      const lp = {
        id: Number(x.id) || 0,
        text: String(x.text),
        ticks: Number(x.ticks) || LOOP_MIN_TICKS,
        count: Number(x.count) || 0,
        createdAt: x.createdAt || '',
        lastAt: x.lastAt || '',
        lastOk: x.lastOk === undefined ? null : !!x.lastOk,
        lastNote: String(x.lastNote || ''),
        nextAt: 0
      }
      if (lp.ticks < LOOP_MIN_TICKS) lp.ticks = LOOP_MIN_TICKS
      loops.push(lp)
      if (lp.id > loopSeq) loopSeq = lp.id
    }
    if (loops.length) log('已恢复 ' + loops.length + ' 条循环指令（来自 ' + LOOP_FILE + '）')
  } catch (e) {
    if (e.code !== 'ENOENT') log('loops.json 读取失败（按空处理）: ' + e.message)
  }
}

function saveLoops () {
  try {
    fs.writeFileSync(LOOP_FILE, JSON.stringify({
      savedAt: ts(),
      note: 'qqbot 循环指令。间隔单位 tick（1 tick = 50ms）。删掉文件不会停止正在跑的循环，用 !mcbot command loop clear。',
      loops: loops.map((x) => ({
        id: x.id, text: x.text, ticks: x.ticks, count: x.count,
        createdAt: x.createdAt, lastAt: x.lastAt, lastOk: x.lastOk, lastNote: x.lastNote
      }))
    }, null, 2))
  } catch (e) {
    log('loops.json 写入失败: ' + e.message)
  }
}

function loopLine (lp) {
  const flag = lp.lastOk === null ? '·' : (lp.lastOk ? '✅' : '⚠️')
  const kind = lp.text.startsWith('/') ? '指令' : '聊天'
  let out = '#' + lp.id + '  ' + ticksHuman(lp.ticks) + '  已执行 ' + lp.count + ' 次  ' +
    (lp.lastAt ? ('上次 ' + lp.lastAt + ' ' + flag) : '尚未执行') +
    (lp.account ? ('  绑定 ' + lp.account) : '') + '\n' +
    '     [' + kind + '] ' + lp.text
  if (lp.lastOk === false && lp.lastNote) out += '\n     ⚠️ ' + lp.lastNote
  return out
}

const LOOP_USAGE = [
  '循环指令用法（间隔单位 tick，1 tick = 50ms，20 tick = 1 秒）：',
  '  !mcbot command loop <消息或指令> <间隔tick>   新建（立刻发一次，之后按间隔重复）',
  '  !mcbot <账号名> command loop <内容> <间隔tick>  绑定到指定账号（挂两个号时用）',
  '  !mcbot command loop list                     查看所有循环',
  '  !mcbot command loop del <编号>               删除某条',
  '  !mcbot command loop clear                    全部清空',
  '',
  '内容以 / 开头 → 当服务器指令执行；否则 → 当聊天栏文字发送。',
  '例：!mcbot command loop /afk 1200       （每 60 秒执行一次 /afk）',
  '    !mcbot command loop 大家好 600      （每 30 秒在聊天栏发一次「大家好」）',
  '',
  '间隔范围：' + LOOP_MIN_TICKS + ' tick（1 秒）~ ' + LOOP_MAX_TICKS + ' tick（1 小时）。',
  '循环跑在宿主机上，跟 QQ / AstrBot 在不在线无关；切换账号后会跟着新的账号执行。'
].join('\n')

async function handleLoopCommand (raw, bindAcc) {
  const rest = String(raw || '').replace(/^loop[\s]*/i, '').trim()
  const bind = (typeof bindAcc === 'string' && bindAcc && ACC_SET.has(bindAcc)) ? bindAcc : ''

  if (!rest || /^(help|帮助|\?)$/i.test(rest)) {
    return { ok: true, message: LOOP_USAGE }
  }

  if (/^(list|ls|列表)$/i.test(rest)) {
    if (!loops.length) return { ok: true, message: '📋 目前没有循环指令。\n\n' + LOOP_USAGE }
    const acc = enabledAccount()
    const out = ['📋 循环指令（共 ' + loops.length + ' 条，当前账号 ' + (acc || '无') + '）']
    for (const lp of loops) out.push(loopLine(lp))
    out.push('', '删除：!mcbot command loop del <编号>；全部清空：!mcbot command loop clear')
    return { ok: true, message: out.join('\n') }
  }

  if (/^(clear|清空|全删|stop)$/i.test(rest)) {
    const n = loops.length
    loops = []
    saveLoops()
    return { ok: true, message: n ? ('✅ 已清空 ' + n + ' 条循环指令') : '（本来就没有循环指令）' }
  }

  const md = rest.match(/^(del|rm|delete|删除)\s+(\d+)$/i)
  if (md) {
    const id = parseInt(md[2], 10)
    const i = loops.findIndex((x) => x.id === id)
    if (i < 0) return { ok: false, message: '没有编号为 #' + id + ' 的循环' }
    const [gone] = loops.splice(i, 1)
    saveLoops()
    return { ok: true, message: '✅ 已删除循环 #' + id + '：' + gone.text }
  }

  // 新增：<内容> <间隔tick>
  const parts = rest.split(/\s+/)
  const lastTok = parts[parts.length - 1]
  if (parts.length < 2 || !/^\d+$/.test(lastTok)) {
    return { ok: false, message: '间隔必须是最后一个纯数字参数（单位 tick）。\n\n' + LOOP_USAGE }
  }
  const ticks = parseInt(lastTok, 10)
  const text = parts.slice(0, -1).join(' ').trim()
  if (!text) return { ok: false, message: '缺少要循环的内容。\n\n' + LOOP_USAGE }
  if (ticks < LOOP_MIN_TICKS) {
    return { ok: false, message: '间隔太短：最小 ' + LOOP_MIN_TICKS + ' tick（1 秒）。\n再短就是刷屏，会被服务器禁言甚至封号。' }
  }
  if (ticks > LOOP_MAX_TICKS) {
    return { ok: false, message: '间隔太长：最大 ' + LOOP_MAX_TICKS + ' tick（1 小时）。' }
  }
  if (text.length > LOOP_TEXT_MAX) {
    return { ok: false, message: '内容太长（上限 ' + LOOP_TEXT_MAX + ' 字符，当前 ' + text.length + '）' }
  }

  const lp = {
    id: ++loopSeq, text: text, ticks: ticks, count: 0, account: bind,
    createdAt: ts(), lastAt: '', lastOk: null, lastNote: '', nextAt: 0
  }
  loops.push(lp)
  saveLoops()
  log('新增循环 #' + lp.id + ' 每 ' + lp.ticks + ' tick: ' + lp.text)
  return {
    ok: true,
    message: [
      '✅ 已新增循环 #' + lp.id,
      '   内容：' + lp.text + '（' + (lp.text.startsWith('/') ? '当服务器指令' : '当聊天栏文字') + '）',
      '   间隔：' + ticksHuman(lp.ticks),
      bind ? ('   绑定账号：' + bind + '（只在 ' + bind + ' 挂机时执行）')
           : '   未绑定账号：会发给第一个启用的号。想固定发给某个号，用 !mcbot <账号名> command loop ...',
      '   查看：!mcbot command loop list    删除：!mcbot command loop del ' + lp.id
    ].join('\n'),
    loop: { id: lp.id, text: lp.text, ticks: lp.ticks }
  }
}

// 调度器：100ms 检查一次，按绝对时间排期（不会累积漂移）
async function loopTick () {
  if (loopBusy || !loops.length) return
  const now = Date.now()
  const due = loops.filter((lp) => !lp.nextAt || now >= lp.nextAt)
  if (!due.length) return
  loopBusy = true
  try {
    const enabledList = (typeof enabledAccounts === 'function') ? enabledAccounts() : [enabledAccount()].filter(Boolean)
    for (const lp of due) {
      lp.nextAt = now + lp.ticks * TICK_MS
      // 绑定了账号就发给它（前提是它正在挂机），否则发给第一个启用的号
      let acc = enabledList[0] || ''
      if (lp.account) {
        if (enabledList.indexOf(lp.account) >= 0) acc = lp.account
        else if (acc) lp.lastNote = '绑定的 ' + lp.account + ' 没在挂机，已改发 ' + acc
      }
      if (!acc) {
        lp.lastOk = false
        lp.lastNote = '跳过：当前没有启用的账号'
        continue
      }
      try {
        const r = await yambSay(acc, lp.text)
        lp.count++
        lp.lastAt = ts()
        lp.lastOk = !!(r && r.success)
        lp.lastNote = lp.lastOk ? ('ok @' + acc) : String((r && r.message) || '无响应').slice(0, 120)
        log('循环 #' + lp.id + ' [' + acc + ']: ' + lp.text + ' -> ' + (lp.lastOk ? 'ok' : lp.lastNote))
      } catch (e) {
        lp.lastOk = false
        lp.lastNote = String((e && e.message) || e).slice(0, 120)
        log('循环 #' + lp.id + ' 异常: ' + lp.lastNote)
      }
    }
    saveLoops()
  } finally {
    loopBusy = false
  }
}

// ── 白名单（adminList）管理 ──────────────────────────────
// 写入时一律加双引号，所以中文/点/横线等特殊字符都安全（YAML 解析器会自动去引号）
function yamlQuote (v) {
  return '"' + String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'
}
function unquoteYaml (v) {
  if (v.length > 1 && v[0] === '"' && v[v.length - 1] === '"') {
    return v.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, '\\')
  }
  if (v.length > 1 && v[0] === "'" && v[v.length - 1] === "'") return v.slice(1, -1).replace(/''/g, "'")
  return v
}
function validAdminName (n) {
  if (!n || n.length > 32) return false
  if (/[\s\u0000-\u001f]/.test(n)) return false
  return true
}
function javaIdWarning (n) {
  return /^[A-Za-z0-9_]{3,16}$/.test(n)
    ? ''
    : '\n⚠️ 提醒：Java 版 MC 的正版 ID 只能是字母/数字/下划线（3~16 位），这个名字加了也匹配不到玩家。'
}

// 写文件前用 yamb 自带的 YAML 解析器验一遍（没有解析器就跳过校验，只记日志）
function yamlParseCheck (text) {
  const mods = ['/opt/qqbot/yamb/node_modules/yaml', '/opt/qqbot/yamb/node_modules/js-yaml']
  for (const m of mods) {
    let y = null
    try {
      y = require(m)
    } catch (e) {
      if (e && e.code === 'MODULE_NOT_FOUND') continue
      return { ok: false, via: m, message: String(e.message || e) }
    }
    try {
      if (typeof y.parse === 'function') { y.parse(text); return { ok: true, via: m } }
      if (typeof y.load === 'function') { y.load(text); return { ok: true, via: m } }
    } catch (e) {
      return { ok: false, via: m, message: String(e.message || e) }
    }
  }
  return { ok: null, via: '(未找到 yaml 解析器，跳过校验)' }
}

function botYamlPath (account) { return path.join(BOTS, account + '.yaml') }

function readAdminList (account) {
  const s = fs.readFileSync(botYamlPath(account), 'utf8')
  const m = s.match(/^adminList:[ \t]*\r?\n((?:[ \t]+-[^\n]*\r?\n?)*)/m)
  if (!m) return null
  return m[1].split('\n').map((l) => l.replace(/^[ \t]+-\s*/, '').trim()).filter(Boolean).map(unquoteYaml)
}

function writeAdminList (account, list) {
  const p = botYamlPath(account)
  const s = fs.readFileSync(p, 'utf8')
  const m = s.match(/^adminList:[ \t]*\r?\n((?:[ \t]+-[^\n]*\r?\n?)*)/m)
  if (!m) throw new Error('找不到 adminList 段')
  const block = 'adminList:\n' + list.map((n) => '  - ' + yamlQuote(n)).join('\n') + '\n'
  const out = s.slice(0, m.index) + block + s.slice(m.index + m[0].length)
  const chk = yamlParseCheck(out)
  if (chk.ok === false) {
    throw new Error('新内容不是合法 YAML，已放弃写入（' + chk.via + '：' + String(chk.message).slice(0, 80) + '）')
  }
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '')
  fs.writeFileSync(p + '.bak-adminlist-' + stamp, s)
  fs.writeFileSync(p, out)
  return p
}

function restartYamb () {
  return new Promise((resolve) => {
    execFile('systemctl', ['--user', 'restart', 'yamb.service'], {
      env: Object.assign({}, process.env, {
        XDG_RUNTIME_DIR: process.env.XDG_RUNTIME_DIR || '/run/user/1000'
      }),
      timeout: 30000
    }, (err, stdout, stderr) => {
      if (err) return resolve({ ok: false, message: (String(stderr || '').trim() || err.message).slice(0, 200) })
      resolve({ ok: true, message: '当前账号：' + (enabledAccount() || '（无）') })
    })
  })
}

// ── 自动挑"信使"：四个号里没在线的那个 ────────────────────
// 为什么要它：QQ 发来的 yamb 指令要以某个管理员身份注入，yamb 会把回执私聊给这个身份。
// 选"不在线的号"当信使，回执会被服务器直接丢弃 —— 游戏里谁都看不到，QQ 侧照旧从日志抓到全文。
// 实现：直接对 MC 服务器做一次 Server List Ping（标准协议，零依赖），拿在线玩家名单。
function readMcTarget () {
  try {
    const t = fs.readFileSync('/opt/qqbot/yamb/.env', 'utf8')
    const h = (t.match(/^MC_HOST=(.+)$/m) || [])[1]
    const p = (t.match(/^MC_PORT=(\d+)/m) || [])[1]
    return h ? { host: h.trim(), port: parseInt(p || '25565', 10) } : null
  } catch (e) { return null }
}

function writeVarInt (v) {
  const out = []
  let n = v
  while (true) {
    if ((n & ~0x7f) === 0) { out.push(n); break }
    out.push((n & 0x7f) | 0x80)
    n >>>= 7
  }
  return Buffer.from(out)
}

function readVarInt (buf, off) {
  let value = 0
  let size = 0
  while (true) {
    const b = buf[off + size]
    if (b === undefined) return null
    value |= (b & 0x7f) << (7 * size)
    size++
    if ((b & 0x80) === 0) break
    if (size > 5) return null
  }
  return { value: value, size: size }
}

function mcPing (host, port, timeoutMs) {
  return new Promise((resolve) => {
    const sock = net.connect({ host: host, port: port })
    let buf = Buffer.alloc(0)
    let done = false
    const finish = (r) => {
      if (done) return
      done = true
      try { sock.destroy() } catch (e) { /* ignore */ }
      resolve(r)
    }
    sock.setTimeout(timeoutMs || 4000, () => finish(null))
    sock.on('error', () => finish(null))
    sock.on('connect', () => {
      const hb = Buffer.from(host, 'utf8')
      const pb = Buffer.alloc(2)
      pb.writeUInt16BE(port)
      const body = Buffer.concat([
        writeVarInt(0), writeVarInt(765),
        writeVarInt(hb.length), hb, pb, writeVarInt(1)
      ])
      sock.write(Buffer.concat([writeVarInt(body.length), body]))
      const req = writeVarInt(0)
      sock.write(Buffer.concat([writeVarInt(req.length), req]))
    })
    sock.on('data', (c) => {
      buf = Buffer.concat([buf, c])
      const len = readVarInt(buf, 0)
      if (!len) return
      const pid = readVarInt(buf, len.size)
      if (!pid || pid.value !== 0) return
      const slen = readVarInt(buf, len.size + pid.size)
      if (!slen) return
      const start = len.size + pid.size + slen.size
      if (buf.length < start + slen.value) return
      try { finish(JSON.parse(buf.slice(start, start + slen.value).toString('utf8'))) } catch (e) { finish(null) }
    })
  })
}

let mcOnlineCache = { at: 0, names: null, note: '' }
async function mcOnlineNames () {
  const now = Date.now()
  if (mcOnlineCache.names && now - mcOnlineCache.at < 60000) return mcOnlineCache
  const t = readMcTarget()
  if (!t) { mcOnlineCache = { at: now, names: null, note: '读不到 yamb/.env 里的 MC_HOST' }; return mcOnlineCache }
  const j = await mcPing(t.host, t.port, 4000)
  if (!j || !j.players) { mcOnlineCache = { at: now, names: null, note: t.host + ':' + t.port + ' 状态查询无响应' }; return mcOnlineCache }
  const sample = j.players.sample || []
  const names = new Set(sample.map((x) => String((x && x.name) || '').toLowerCase()))
  mcOnlineCache = {
    at: now, names: names, note: '',
    online: j.players.online, max: j.players.max,
    truncated: sample.length >= 12
  }
  return mcOnlineCache
}

let lastSenderMsg = ''
function logSenderOnce (msg) {
  if (msg === lastSenderMsg) return
  lastSenderMsg = msg
  log(msg)
}

// ── 在线名单：首选问 yamb 自己（bot 在游戏里，它手上的名单最权威）──
//     GET /api/players（yamb 自带接口）。空名单一律当"没数据"，不当成"没人在线"。
async function yambOnlineNames (botAcc) {
  if (!botAcc) return null
  const r = await yambCall(botAcc, 'players', undefined, 5000)
  if (!r || r.success === false) return null
  let arr = null
  if (Array.isArray(r.players)) arr = r.players
  else if (Array.isArray(r.list)) arr = r.list
  else if (Array.isArray(r.data)) arr = r.data
  else if (Array.isArray(r.onlinePlayers)) arr = r.onlinePlayers
  else if (r.players && typeof r.players === 'object') arr = Object.keys(r.players)
  if (!arr) {
    logSenderOnce('信使：/api/players 返回的形状不认识 -> ' + JSON.stringify(r).slice(0, 200))
    return null
  }
  const names = new Set()
  for (const x of arr) {
    const n = typeof x === 'string' ? x : String((x && (x.name || x.username)) || '')
    if (n) names.add(n.toLowerCase())
  }
  if (!names.size) return null
  return { names: names, online: names.size, note: 'yamb /api/players' }
}

async function pickSender (botAcc) {
  const cfg = String(process.env.MCBOT_INJECT_SENDER || 'auto-offline').trim()
  if (cfg === 'self') return botAcc
  if (cfg === 'auto') return ACCOUNTS.find((a) => a !== botAcc) || botAcc
  if (cfg !== 'auto-offline') return cfg

  const others = ACCOUNTS.filter((a) => a !== botAcc)

  // 数据源 1：yamb /api/players（权威）
  let info = await yambOnlineNames(botAcc)
  // 数据源 2：MC 服务器状态查询的 sample（有的服务器会下发，本服不下发）
  if (!info) {
    const p = await mcOnlineNames()
    if (p && p.names && p.names.size) {
      info = { names: p.names, online: p.online, note: '服务器状态查询', truncated: p.truncated }
    }
  }

  if (info && info.names && info.names.size) {
    const free = others.filter((a) => !info.names.has(a.toLowerCase()))
    if (free.length) {
      const shown = [...info.names].slice(0, 12).join(',') + (info.names.size > 12 ? ' 等' : '')
      logSenderOnce('信使 = ' + free[0] + '（来源 ' + info.note + '；在线 ' + info.online + ' 人：' + shown +
        '；它不在线' + (info.truncated ? '，⚠️ 该名单被截断到 12 个，判断可能不准' : '') + '）')
      return free[0]
    }
    logSenderOnce('信使 = ' + others[0] + '（' + botAcc + ' 之外的三个号都在线，只能挑一个在线的：回执会私聊到它）')
    return others[0] || botAcc
  }

  // 两个数据源都没数据：宁可保守（用你指定的固定号），也不要嘴上说"不在线"
  const fixed = String(process.env.MCBOT_FALLBACK_SENDER || '').trim()
  if (fixed && ACCOUNTS.indexOf(fixed) >= 0 && fixed !== botAcc) {
    logSenderOnce('信使 = ' + fixed + '（拿不到在线名单，退回 MCBOT_FALLBACK_SENDER 指定的固定账号）')
    return fixed
  }
  logSenderOnce('信使 = ' + (others[0] || botAcc) + '（⚠️ 拿不到在线名单：yamb /api/players 与服务器状态查询都没数据；' +
    '这个号若在线，回执会私聊到它。可设 MCBOT_FALLBACK_SENDER=<账号名> 固定一个你不上线的号）')
  return others[0] || botAcc
}

async function handleAdminCommand (raw, forcedAcc) {
  const t = String(raw || '').trim()
  if (!t) return null
  const forced = (forcedAcc && ACC_SET.has(forcedAcc)) ? forcedAcc : ''

  if (/^(admins|list|名单|白名单)$/i.test(t)) {
    const cur = enabledAccount()
    const scope = forced ? [forced] : ACCOUNTS
    const out = [forced
      ? ('📋 ' + forced + ' 的管理员名单（config/bots/' + forced + '.yaml 的 adminList）')
      : '📋 bot 白名单（config/bots/*.yaml 的 adminList）']
    for (const a of scope) {
      let list = null
      try { list = readAdminList(a) } catch (e) { list = null }
      const tag = a === cur ? '（当前运行）' : ''
      out.push('◆ ' + a + tag + (list ? '   共 ' + list.length + ' 人' : '   !! 读取失败'))
      if (list) { for (const nm of list) out.push('     · ' + nm) }
      out.push('')
    }
    if (forced) {
      out.push('', '看全部 4 个：!mcbot yamb list')
    } else {
      out.push('', '看单个：!mcbot <账号名> yamb list')
    }
    out.push('加人：!mcbot yamb add <游戏名>（当前运行的）｜ !mcbot yamb alladd <游戏名>（4 个都加）｜ !mcbot <账号名> yamb add <游戏名>（指定）')
    out.push('删人：!mcbot yamb del <游戏名> ｜ !mcbot yamb alldel <游戏名>')
    out.push('生效：add/del 会先写文件，再尝试游戏内热加载；不行就 !mcbot yamb reload（掉线约 40 秒）')
    return { ok: true, message: out.join('\n') }
  }

  if (/^(reload|重启|restart)$/i.test(t)) {
    log('重启 yamb（白名单生效）')
    const r = await restartYamb()
    return {
      ok: r.ok,
      message: r.ok
        ? ('✅ 已重启 yamb，约 40 秒后自动回到服务器。\n' + r.message + '\n生效后可用 !mcbot yamb admins 复查。')
        : ('❌ 重启 yamb 失败：' + r.message)
    }
  }

  const m = t.match(/^(alladd|alldel|add|del|remove)\s+(\S+)$/i)
  if (!m) return null

  const op = m[1].toLowerCase()
  const name = m[2]
  const isAdd = (op === 'add' || op === 'alladd')
  const everyBot = (op === 'alladd' || op === 'alldel')

  if (!validAdminName(name)) {
    return { ok: false, message: '名字不合法：' + name + '\n（不能为空、不能带空格/换行，长度 ≤ 32）' }
  }

  const cur = enabledAccount()
  const targets = everyBot
    ? ACCOUNTS.slice()
    : (forced ? [forced] : (cur ? [cur] : []))
  if (!targets.length) {
    return { ok: false, message: '当前没有启用的账号。\n要给指定的 bot 加人，用：!mcbot yamb alladd ' + name + '（会加到全部 4 个）' }
  }

  const done = []
  const skipped = []
  const failed = []
  for (const a of targets) {
    let list
    try { list = readAdminList(a) } catch (e) { failed.push(a + '（' + e.message + '）'); continue }
    if (!list) { failed.push(a + '（无 adminList 段）'); continue }
    const has = list.some((n) => n.toLowerCase() === name.toLowerCase())
    if (isAdd && has) { skipped.push(a + '（已在名单里）'); continue }
    if (!isAdd && !has) { skipped.push(a + '（本来就不在名单里）'); continue }
    const next = isAdd ? list.concat([name]) : list.filter((n) => n.toLowerCase() !== name.toLowerCase())
    try {
      writeAdminList(a, next)
      done.push(a + '（' + next.length + ' 人）')
    } catch (e) {
      failed.push(a + '（' + e.message + '）')
    }
  }

  // 热加载：只对"正在运行的那个号"、且是加人时尝试
  const isRunningTarget = (targets.length === 1 && targets[0] === cur)
  let hot = null
  if (isAdd && isRunningTarget && done.length && serviceActive()) {
    try {
      const sender = await pickSender(cur)
      const since = outSizeNow()
      const r = await yambInject(cur, sender, '#b add ' + name)
      const replies = await captureOutgoing(since, { maxLines: 40, timeoutMs: 20000 })
      hot = { ok: !!(r && r.success), replies: replies || [] }
      log('热加载白名单 [' + cur + ']: #b add ' + name + ' -> ' + (hot.ok ? 'ok' : 'failed'))
    } catch (e) {
      hot = { ok: false, replies: [], message: String((e && e.message) || e).slice(0, 120) }
    }
  }

  const out = []
  out.push((isAdd ? '✅ 已把「' + name + '」加入白名单' : '✅ 已把「' + name + '」移出白名单'))
  if (done.length) out.push('   已写入：' + done.join('、'))
  if (skipped.length) out.push('   跳过：' + skipped.join('、'))
  if (failed.length) out.push('   ❌ 失败：' + failed.join('、'))
  if (hot) {
    out.push('', '已尝试热加载到运行中的 ' + cur + '：#b add ' + name)
    if (hot.replies && hot.replies.length) {
      out.push('   游戏回执：')
      for (const r of hot.replies.slice(0, 6)) out.push('   > ' + String(r).slice(0, 160))
    } else {
      out.push('   （没抓到游戏回执）')
    }
    out.push('   如果游戏里提示未知指令 / 没反应，说明这个版本不支持热加载，')
    out.push('   执行 !mcbot yamb reload 重启 yamb 即可生效（掉线约 40 秒，微软令牌有缓存，不用重登）。')
  } else if (isAdd && done.length && !isRunningTarget) {
    out.push('', '（' + targets.join('、') + ' 当前没在运行，配置文件已改好，下次启动时自动生效；')
    out.push('  想立刻生效：先 !mcbot ' + targets[0] + ' 切过去，再 !mcbot yamb add ' + name + '）')
  }
  out.push('', '复查：!mcbot yamb list')
  return { ok: failed.length === 0, message: out.join('\n') + (isAdd ? javaIdWarning(name) : '') }
}

// ── HTTP 工具 ───────────────────────────────────────────
function send (res, code, obj) {
  const body = JSON.stringify(obj)
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body)
  })
  res.end(body)
}

function readBody (req) {
  return new Promise((resolve) => {
    let data = ''
    req.on('data', (c) => {
      data += c
      if (data.length > 65536) req.destroy()
    })
    req.on('end', () => {
      try { resolve(data ? JSON.parse(data) : {}) } catch (e) { resolve(null) }
    })
  })
}

// ── 业务 ────────────────────────────────────────────────
async function handleStatus () {
  const enabled = enabledAccounts()
  const active = serviceActive()
  const list = []
  for (const a of ACCOUNTS) {
    const on = enabled.indexOf(a) >= 0
    let ingame = null
    if (on) {
      const r = await yambCall(a, 'status', undefined, 5000)
      // 【pkg99】yamb 的 getStatus() 里 success 恒为 true（它只表示“命令跑通了”），
      // 真正的“是否在游戏里”要看 minecraft 字段；否则没进服也会显示“已进服”。
      if (r && r.success && r.minecraft === true) ingame = r
    }
    list.push({ name: a, num: NUM_OF[a] || '', enabled: on, ingame: ingame })
  }
  const first = enabled[0] || null
  const firstRow = list.filter((x) => x.name === first)[0]
  return {
    ok: true,
    service: active ? 'running' : 'stopped',
    enabled: first,
    enabledList: enabled,
    ingame: (firstRow && firstRow.ingame) || null,
    accounts: list
  }
}

async function route (req, res) {
  const url = (req.url || '/').split('?')[0]

  if (url === '/health') return send(res, 200, { ok: true })

  const key = req.headers['x-api-key']
  if (key !== API_KEY) {
    log('拒绝：密钥错误  来自 ' + req.socket.remoteAddress)
    return send(res, 401, { ok: false, message: 'unauthorized' })
  }

  if (req.method === 'GET' && url === '/primary') {
    const online = await napcatLoginInfo(3000, 5000)
    return send(res, 200, { ok: true, primary: '3764332026', online: online })
  }

  if (req.method === 'GET' && url === '/status') {
    return send(res, 200, await handleStatus())
  }

  if (req.method === 'GET' && url === '/pay-list') {
    return send(res, 200, await payList())   // _p69b: GET 放行（要在 405 拦截之前）
  }

  if (req.method !== 'POST') {
    return send(res, 405, { ok: false, message: 'method not allowed' })
  }

  const body = await readBody(req)
  if (body === null) {
    return send(res, 400, { ok: false, message: 'JSON 解析失败' })
  }

  if (url === '/restart-astrbot') {
    // 插件在"加管理员/补白名单"之后调用：延迟 N 秒重启 AstrBot，
    // 让写进文件的会话白名单立刻生效（回执先发出去，再重启）
    const delay = Math.max(0, Math.min(120, parseInt(body.delay, 10) || 3)) * 1000
    log('收到请求：' + (delay / 1000) + ' 秒后重启 AstrBot')
    setTimeout(() => {
      execFile('docker', ['restart', 'astrbot'],
        { timeout: 180000 },
        (err, so, se) => {
          log('重启 AstrBot ' + (err ? ('失败: ' + String(se || err.message).slice(0, 200)) : '完成'))
        })
    }, delay)
    return send(res, 200, { ok: true, message: '将在 ' + (delay / 1000) + ' 秒后重启 AstrBot（约 25 秒后恢复）' })
  }

  if (url === '/action') {
    return actions.handleAction(res, body, send, log)
  }

  if (url === '/connect') {
    return connect.handleConnect(res, body, send, log)
  }

  if (url === '/mcsay') {
    return connect.handleSay(res, body, send, log, { accounts: ACCOUNTS, yambSay: yambSay })
  }

  // ── 转钱（_p69）────────────────────────────────────────
  if (url === '/pay-add') return send(res, 200, await payAdd(body))
  if (url === '/pay-del') return send(res, 200, await payDel(body))
  if (url === '/pay-list') return send(res, 200, await payList())
  if (url === '/pay-run') return send(res, 200, payRun(body))
  if (url === '/pay-auth') return send(res, 200, await payAuth(body))   // _p70


  if (url === '/switch') {
    // 新用法：{ mode: 'set'|'toggle'|'enable'|'disable', accounts: [...] }
    // 老用法：{ account: 'sd_dt' }  ==  set sd_dt
    const list = Array.isArray(body.accounts)
      ? body.accounts.map((x) => accOf(x) || String(x).trim()).filter(Boolean)
      : []
    const mode = String(body.mode || '').trim().toLowerCase()
    let args
    if (list.length || mode) {
      const bad = list.filter((a) => !ACC_SET.has(a))
      if (bad.length) return send(res, 400, { ok: false, message: '未知账号：' + bad.join('、') })
      // _p66: set 是唯一会改动"未提及账号"（把没点名的全部关掉）的模式，
      //       绝不能作为兜底默认值。mode 拼错直接报错；没给 mode 按"追加启用"处理。
      let m
      if (mode) {
        if (['set', 'toggle', 'enable', 'disable'].indexOf(mode) < 0) {
          return send(res, 400, { ok: false, message: '未知 mode：' + mode + '（可用：set/toggle/enable/disable）' })
        }
        m = mode
      } else {
        m = 'enable'
      }
      if (!list.length) return send(res, 400, { ok: false, message: '缺少 accounts' })
      args = [m].concat(list)
    } else {
      const acc = accOf(body.account)
      if (!acc) return send(res, 400, { ok: false, message: '未知账号：' + String(body.account || '') + '（可用数字 1~4 或完整账号名）' })
      args = [acc]
    }
    log('账号操作 -> switch.sh ' + args.join(' '))
    const r = await runSwitch(args, 150000)
    return send(res, r.ok ? 200 : 500, r)
  }

  if (url === '/stop') {
    log('下线 MC bot')
    const r = await runSwitch(['stop'])
    return send(res, r.ok ? 200 : 500, r)
  }

  if (url === '/start') {
    const acc = accOf(body.account)
    log('上线 MC bot' + (acc ? ' -> ' + acc : '（沿用上次启用的账号）'))
    const r = await runSwitch(acc ? [acc] : ['start'])
    return send(res, r.ok ? 200 : 500, r)
  }

  if (url === '/command') {
    const raw = String(body.command || '').trim()
    if (!raw) return send(res, 400, { ok: false, message: '缺少 command' })

    // 循环指令在这里拦截：它本身不是游戏指令，不进冷却，也不要求当前有账号在线
    if (/^loop(\s|$)/i.test(raw)) {
      const r = await handleLoopCommand(raw, String(body.account || '').trim())
      log('循环指令: ' + raw + ' -> ' + (r.ok ? 'ok' : 'ng'))
      return send(res, r.ok ? 200 : 400, r.ok
        ? { ok: true, sent: raw, message: r.message, replies: String(r.message).split('\n'), loop: r.loop }
        : { ok: false, message: r.message })
    }

    const enabledNow = enabledAccounts().concat(payEnabledAccounts())   // _p69: 含转钱小号
    const wantAcc = accOf(body.account)
    let acc = enabledNow[0] || ''
    if (wantAcc) {
      if (enabledNow.indexOf(wantAcc) < 0) {
        return send(res, 409, { ok: false, message: '账号 ' + wantAcc + ' 当前没在挂机（正在挂：' + (enabledNow.join('、') || '无') + '）' })
      }
      acc = wantAcc
    }
    if (!acc) return send(res, 409, { ok: false, message: '当前没有启用的账号' })
    const nowCmd = Date.now()
    if (nowCmd - lastGameCmdAt < GAME_CMD_COOLDOWN_MS) {
      return send(res, 429, { ok: false, message: '操作太频繁，请稍等几秒' })
    }
    lastGameCmdAt = nowCmd
    const text = raw
    log('游戏内指令 [' + acc + ']: ' + text)
    const since = logSizeNow()
    const r = await yambSay(acc, text)
    const replies = (await captureReply(since, text, { botName: acc, maxLines: 40, timeoutMs: 20000 })) || []
    if (!replies.length) { const hint = receiptHint(text, r); if (hint) replies.push(hint) }
    return send(res, 200, {
      ok: !!(r && r.success),
      message: (r && r.message) || '无响应',
      sent: text,
      replies: replies
    })
  }

  if (url === '/yamb') {
    const raw = String(body.command || '').trim()
    if (!raw) return send(res, 400, { ok: false, message: '缺少 command' })

    const reqAcc = accOf(body.account)
    const forcedAcc = reqAcc

    // 白名单管理在这里拦截（list/add/del/alladd/alldel/reload）：
    // 不进游戏指令冷却；当前没有账号在线时也要能用（alladd 要给 4 个 bot 都写）
    const adminRes = await handleAdminCommand(raw, forcedAcc)
    if (adminRes) {
      log('白名单指令' + (forcedAcc ? ' [' + forcedAcc + ']' : '') + ': ' + raw + ' -> ' + (adminRes.ok ? 'ok' : 'ng'))
      return send(res, adminRes.ok ? 200 : 400, adminRes.ok
        ? { ok: true, sent: raw, message: adminRes.message, replies: String(adminRes.message).split('\n') }
        : { ok: false, message: adminRes.message })
    }

    const enabledNow = enabledAccounts()
    let acc = enabledNow[0] || ''
    if (forcedAcc) {
      if (enabledNow.indexOf(forcedAcc) < 0) {
        return send(res, 409, {
          ok: false,
          message: '账号 ' + forcedAcc + ' 当前没在挂机（正在挂：' + (enabledNow.join('、') || '无') + '）。\n（白名单管理不受这个限制：!mcbot ' + forcedAcc + ' yamb add/list 都能用）'
        })
      }
      acc = forcedAcc
    }
    if (!acc) return send(res, 409, { ok: false, message: '当前没有启用的账号' })
    const nowCmd = Date.now()
    if (nowCmd - lastGameCmdAt < GAME_CMD_COOLDOWN_MS) {
      return send(res, 429, { ok: false, message: '操作太频繁，请稍等几秒' })
    }
    lastGameCmdAt = nowCmd
    const helpCmd = String(raw || '').trim().toLowerCase()
    if (helpCmd === 'help' || helpCmd === '帮助') {
      const helpLines = yambHelpLines(acc)
      if (helpLines.length) {
        log('yamb help（本地读取）[' + acc + ']: ' + helpLines.length + ' 行')
        return send(res, 200, { ok: true, message: '已读取 ' + acc + ' 的指令列表', sent: 'help', replies: helpLines })
      }
    }
    const text = '#b ' + raw.replace(/^#b\s+/i, '')
    const sender = await pickSender(acc)
    log('yamb 指令 [' + acc + ']: ' + text)
    const since = outSizeNow()
    const r = await yambInject(acc, sender, text)
    const replies = (await captureOutgoing(since, { maxLines: 40, timeoutMs: 20000 })) || []
    if (!replies.length) { const hint = receiptHint(text, r); if (hint) replies.push(hint) }
    return send(res, 200, {
      ok: !!(r && r.success),
      message: (r && r.message) || '无响应',
      sent: text,
      replies: replies
    })
  }

  return send(res, 404, { ok: false, message: 'not found' })
}

function yambHelpLines (acc) {
  // 直接读 yamb 的 bot 配置，返回 helpLines（避免游戏内抓包被限流/超时截断）
  const p = '/opt/qqbot/yamb/config/bots/' + acc + '.yaml'
  let text = ''
  try { text = fs.readFileSync(p, 'utf8') } catch (e) { return [] }
  const out = []
  let inBlock = false
  for (const line of text.split(/\r?\n/)) {
    if (/^helpLines:\s*$/.test(line)) { inBlock = true; continue }
    if (!inBlock) continue
    const m = line.match(/^\s+-\s+(.*)$/)
    if (m) { out.push(m[1].trim()); continue }
    if (/^\S/.test(line)) break
  }
  return out
}

function requestHandler (req, res) {
  route(req, res).catch((e) => {
    const msg = e && e.message ? e.message : String(e)
    log('处理异常: ' + msg)
    try { send(res, 500, { ok: false, message: msg }) } catch (x) { /* 已发送 */ }
  })
}


// ── 转钱（pay）_p69 ──────────────────────────────────────
function payRead () {
  try {
    return JSON.parse(fs.readFileSync(PAY_FILE, 'utf8'))
  } catch (e) {
    return { pay_to: '', pay_amount: '', command: '/pay {to} {amount}', notify_user: '', accounts: [], ports: {} }
  }
}

function payWrite (cfg) {
  fs.writeFileSync(PAY_FILE, JSON.stringify(cfg, null, 2) + '\n')
}

function payStateRead () {
  try {
    return JSON.parse(fs.readFileSync(PAY_STATE_FILE, 'utf8'))
  } catch (e) {
    return { date: '', tries: {}, done: [] }
  }
}

function payToday () {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
}

function payAccounts () {
  const cfg = payRead()
  const list = Array.isArray(cfg.accounts) ? cfg.accounts : []
  return list.filter((a) => a && !/\s|[\/\\]/.test(a))
}

function payEnabledAccounts () {
  const out = []
  for (const a of payAccounts()) {
    try {
      const m = fs.readFileSync(path.join(BOTS, a + '.yaml'), 'utf8').match(/^enabled:\s*(\S+)/m)
      if (m && m[1] === 'true') out.push(a)
    } catch (e) { /* 文件不存在就跳过 */ }
  }
  return out
}

function payNextPort (cfg) {
  const used = Object.values(PORTS).concat(Object.values(cfg.ports || {}))
  for (let p = PAY_PORT_BASE; p < PAY_PORT_BASE + 60; p++) {
    if (used.indexOf(p) < 0) return p
  }
  return 0
}

function payYamlText (account, port, password) {
  const pw = String(password).replace(/\\/g, '\\\\').replace(/"/g, '\\"')
  return [
    'id: ' + account,
    'enabled: false',
    '',
    'account:',
    '  username: ' + account,
    '  password: "' + pw + '"',
    '  auth: microsoft',
    '',
    'adminList:',
    ...ACCOUNTS.map((a) => '  - ' + a),
    '',
    'astrbot:',
    '  enabled: true',
    '  port: ' + port,
    '',
    'command:',
    '  prefix: "#b"',
    '  allowPublicCommands: true',
    '  replyAlwaysWhisper: true',
    '',
    'helpLines:',
    '  - "=== 转钱小号 ==="',
    '  - "本号为自动转钱小号（每天 04:00 上号转钱，平时不在线）"',
    '  - "help — 显示这份帮助"',
    '',
    'teleport:',
    '  databaseFile: ./data/' + account + '.db',
    '',
    'viewer:',
    '  enabled: false',
    '',
    'bot:',
    '  idleTimeoutMs: 86400000',
    ''
  ].join('\n')
}

async function payAdd (body) {
  const account = String(body.account || '').trim()
  // _p70: 设备码授权流，不再经 QQ 收密码
  if (!account) return { ok: false, message: '缺少 account' }
  if (/\s|[\/\\]/.test(account) || account.length > 64) {
    return { ok: false, message: '账号名不合法（不能含空白或斜杠，长度 ≤ 64）' }
  }
  // _p71: 先查名单（重新授权走 !mcbot payauth，不用 delpay+addpay）
  const cfg0 = payRead()
  if ((Array.isArray(cfg0.accounts) ? cfg0.accounts : []).indexOf(account) >= 0) {
    return { ok: false, message: '该账号已在转钱名单里（重新授权用 !mcbot payauth ' + account + '，不必重新登记）' }
  }
  const yml = path.join(BOTS, account + '.yaml')
  // _p71: 名单外残留的 yaml（老版 delpay 遗留等）直接改名备份后覆盖重建
  if (fs.existsSync(yml)) {
    const bak = yml + '.removed-' + new Date().toISOString().slice(0, 10).replace(/-/g, '')
    fs.renameSync(yml, bak)
    log('发现名单外残留配置，已备份覆盖: ' + bak)
  }
  const cfg = payRead()
  cfg.accounts = Array.isArray(cfg.accounts) ? cfg.accounts : []
  cfg.ports = cfg.ports || {}
  cfg.paySwitch = cfg.paySwitch || {}           // _p73: 新号默认参与转钱
  cfg.paySwitch[account] = true
  const port = payNextPort(cfg)
  if (!port) return { ok: false, message: '没有空闲端口可分配（15105~15164）' }
  const ytext = payYamlText(account, port, '')   // _p70: 密码恒为空，登录态走本地 token 缓存
  const check = yamlParseCheck(ytext)
  if (check.ok === false) return { ok: false, message: '生成的 YAML 校验失败：' + check.message }
  cfg.accounts.push(account)
  cfg.ports[account] = port
  try {
    payWrite(cfg)
    fs.writeFileSync(yml, ytext)   // _p69c: 先记账再写配置；写配置失败则回滚名单
  } catch (e) {
    cfg.accounts.splice(cfg.accounts.indexOf(account), 1)
    delete cfg.ports[account]
    payWrite(cfg)
    return { ok: false, message: "写入失败：" + e.message }
  }
  log('转钱小号已登记: ' + account + '（端口 ' + port + '）')
  let extra = ''
  if (yambAlive() && enabledAccounts().length > 0) {
    const ry = await restartYamb()
    extra = '已重启 yamb 让新号生效（在线的号会掉线约 40 秒自动回来）' +
      (ry && ry.ok ? '' : '\n⚠️ yamb 重启失败：' + ((ry && ry.message) || '未知'))
  } else {
    // _p69d: yamb 上游在 0 个启用号时启动即退出，此时不硬重启；
    // paytest / 04:00 定时任务的 startInstance 会按需拉起 yamb（小号 enabled:true 撑起启动条件）
    extra = 'yamb 当前未运行或没有启用的挂机号，暂不重启；新号会在下次 yamb 启动时自动生效' +
      '（每天 04:00 自动登录会带上它）'
  }
  // _p70: 登记完当场发微软设备授权码（免密码）
  const auth = await payStartAuth(account)
  if (auth.ok) {
    extra += '\n\n🔗 ' + auth.verification_uri + '\n🔑 授权码：' + auth.user_code +
      '\n请在浏览器打开网址，用「' + account + '」的微软账号登录并输入授权码（15 分钟内有效）。' +
      '\n全程无需把密码发给机器人：授权完成后 token 只保存在服务器本地缓存，完成时我会私聊通知你。'
  } else {
    extra += '\n\n⚠️ 设备授权码没能发出：' + auth.message
  }
  return {
    ok: true,
    message: '✅ 已登记转钱小号 ' + account + '（端口 ' + port + '）\n' + extra +
      '\n\n授权完成后可用 !mcbot paytest ' + account + ' 当场试转一次\n' +
      '每天 04:00 自动登录转钱；收款人/金额记得用 !mcbot payset 设置'
  }
}

// _p71: 尽力而为地停掉某个小号的 yamb 实例（走控制口）
function yambCtrlStop (account) {
  return new Promise((resolve) => {
    const payload = JSON.stringify({ account: account })
    const req = http.request({
      host: '127.0.0.1', port: 15199, path: '/api/instances/stop', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload), 'x-api-key': YAMB_API_KEY },
      timeout: 30000
    }, (res) => {
      let d = ''
      res.on('data', (c) => { d += c })
      res.on('end', () => { log('停实例 ' + account + ': ' + d.slice(0, 100)); resolve(true) })
    })
    req.on('error', () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
    req.write(payload)
    req.end()
  })
}

async function payDel (body) {
  const account = String(body.account || '').trim()
  if (!account) return { ok: false, message: '缺少 account' }
  const cfg = payRead()
  cfg.accounts = Array.isArray(cfg.accounts) ? cfg.accounts : []
  const i = cfg.accounts.indexOf(account)
  if (i < 0) return { ok: false, message: '转钱名单里没有：' + account }
  cfg.accounts.splice(i, 1)
  if (cfg.ports) delete cfg.ports[account]
  if (cfg.paySwitch) delete cfg.paySwitch[account]   // _p73
  payWrite(cfg)
  // _p71: 彻底移除——先尽量停掉在跑的实例，yaml 改名备份（不再是"保留停用"，避免挡住重新 addpay）
  let note = ''
  const stopped = await yambCtrlStop(account)
  if (stopped) note = '\n该号实例已下线'
  try {
    const yml = path.join(BOTS, account + '.yaml')
    const bak = yml + '.removed-' + new Date().toISOString().slice(0, 10).replace(/-/g, '')
    fs.renameSync(yml, bak)
    note += '\nyaml 已移除（备份：' + bak + '）'
  } catch (e) { /* 文件不存在就算了 */ }
  log('转钱小号已移除: ' + account)
  return { ok: true, message: '✅ 已彻底移除 ' + account + note }
}

function payList () {
  const cfg = payRead()
  const st = payStateRead()
  const sameDay = st.date === payToday()
  const lines = ['💰 转钱配置（每天 04:00 自动执行，失败自动排队重试，每个号每天最多 4 次）']
  lines.push('收款人：' + (cfg.pay_to || '（未设置）'))
  lines.push('金额：' + (cfg.pay_amount || '（未设置）'))
  if (!cfg.pay_to || !cfg.pay_amount) lines.push('⚠️ 还没设置：!mcbot payset <收款人> <金额>')
  const accs = payAccounts()
  if (!accs.length) lines.push('小号：（无）— 用 !mcbot addpay <账号> 登记')
  const sw = cfg.paySwitch || {}
  for (const a of accs) {
    const off = sw[a] === false   // _p73: 单号开关，缺省=开
    const t = sameDay ? (st.tries[a] || 0) : 0
    const done = sameDay && Array.isArray(st.done) && st.done.indexOf(a) >= 0
    lines.push('◆ ' + a + '（端口 ' + ((cfg.ports || {})[a] || '?') + '）' +
      (off ? '  ⏸ 已暂停（!mcbot payon ' + a + ' 恢复）' : '') +
      (done ? '  ✅ 今日已转' : (t ? '  今日已试 ' + t + '/4' : '')))
  }
  return Promise.resolve({ ok: true, message: lines.join('\n') })
}

function payRun (body) {
  const account = String(body.account || '').trim()
  const accs = payAccounts()
  if (account && accs.indexOf(account) < 0) {
    return Promise.resolve({ ok: false, message: '未知转钱账号：' + account + '（用 !mcbot paylist 查看名单）' })
  }
  let pid = ''
  try {
    const out = fs.openSync(PAY_LOG, 'a')
    const child = spawn(process.execPath, [PAY_SCRIPT].concat(account ? ['--account', account] : []),
      { detached: true, stdio: ['ignore', out, out], cwd: '/opt/qqbot/mcbot' })
    child.unref()
    fs.closeSync(out)
    pid = child.pid
  } catch (e) {
    return Promise.resolve({ ok: false, message: '启动失败：' + e.message })
  }
  log('触发转钱' + (account ? ' [' + account + ']' : '（全部）') + ' -> pid ' + pid)
  return Promise.resolve({
    ok: true,
    message: '✅ 已开始执行转钱' + (account ? '（账号 ' + account + '）' : '（全部小号）') +
      '\n流程：临时下线挂机号 → 登录小号 → 等进服 → 转钱 → 下线小号 → 恢复挂机号\n' +
      '失败会自动排队重试（每个号每天最多 4 次），结果会私聊通知\n' +
      '日志：/opt/qqbot/mcbot/pay-run.log'
  })
}


// ── 设备码授权（_p70）────────────────────────────────────
// 拉起 ms-device-auth.js（与 yamb 登录同构：同缓存目录/同 flow），等授权码出现后返回给 QQ。
// token 只落在本地缓存文件；code/done 状态文件不含 token。
async function payStartAuth (account) {
  const codeFile = path.join(PAY_AUTH_DIR, account + '.code.json')
  const doneFile = path.join(PAY_AUTH_DIR, account + '.done.json')
  try { fs.unlinkSync(codeFile) } catch (e) {}
  try { fs.unlinkSync(doneFile) } catch (e) {}
  try { fs.mkdirSync(PAY_AUTH_DIR, { recursive: true, mode: 0o700 }) } catch (e) {}
  const out = fs.openSync(PAY_LOG, 'a')
  const child = spawn(process.execPath, [PAY_AUTH_SCRIPT, account],
    { detached: true, stdio: ['ignore', out, out], env: Object.assign({}, process.env, { PAY_NAPCAT_TOKEN: NAPCAT_TOKEN }) })
  child.unref()
  fs.closeSync(out)
  log('设备码授权已发起: ' + account + ' -> pid ' + child.pid)
  for (let i = 0; i < 16; i++) {   // 最多等 ~8 秒
    await new Promise((r) => setTimeout(r, 500))
    try {
      const c = JSON.parse(fs.readFileSync(codeFile, 'utf8'))
      if (c && c.user_code) {
        return { ok: true, verification_uri: c.verification_uri || 'https://www.microsoft.com/link', user_code: c.user_code }
      }
    } catch (e) { /* 还没就绪 */ }
    try {
      const d = JSON.parse(fs.readFileSync(doneFile, 'utf8'))
      if (d && d.ok) {
        // 缓存里已有有效 token，本次无需授权
        return { ok: true, verification_uri: '（该账号缓存里已有有效登录态，无需授权）', user_code: '无需授权码' }
      }
    } catch (e) {}
  }
  return { ok: false, message: '8 秒内没等到授权码（详情见 pay-run.log）；稍后可用 !mcbot payauth ' + account + ' 重试' }
}

async function payAuth (body) {
  const account = String(body.account || '').trim()
  if (payAccounts().indexOf(account) < 0) {
    return { ok: false, message: '未知转钱账号：' + account + '（用 !mcbot paylist 查看名单）' }
  }
  const auth = await payStartAuth(account)
  if (!auth.ok) return { ok: false, message: '设备授权码没能发出：' + auth.message }
  return {
    ok: true,
    message: '🔑 「' + account + '」微软设备授权码已发出\n' +
      '🔗 ' + auth.verification_uri + '\n' +
      '🔑 授权码：' + auth.user_code + '\n' +
      '请在浏览器打开上面的网址，用该小号的微软账号登录并输入授权码（15 分钟内有效）。\n' +
      '授权完成后 token 只保存在服务器本地，我会私聊通知你。'
  }
}

// _p69d: yamb 进程是否在跑
function yambAlive () {
  try {
    const out = execFileSync('pgrep', ['-f', 'yamb/dist/index.js'],
      { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
    return out !== ''
  } catch (e) { return false }
}

// ── 绑定地址：127.0.0.1 + Docker 网桥网关 ────────────────
function dockerGateway () {
  try {
    const net = execFileSync('docker', ['inspect', 'astrbot', '-f',
      '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}']).toString().trim()
    if (!net) return ''
    const gw = execFileSync('docker', ['network', 'inspect', net, '-f',
      '{{(index .IPAM.Config 0).Gateway}}']).toString().trim()
    return /^\d+\.\d+\.\d+\.\d+$/.test(gw) ? gw : ''
  } catch (e) {
    return ''
  }
}

const gw = dockerGateway()
const binds = ['127.0.0.1']
if (gw) binds.push(gw)
const subnetPrefix = gw ? gw.split('.').slice(0, 2).join('.') + '.' : null

function allowedRemote (addr) {
  if (!addr) return false
  const a = addr.replace(/^::ffff:/, '')
  if (a === '127.0.0.1' || a === '::1') return true
  if (subnetPrefix && a.indexOf(subnetPrefix) === 0) return true
  return false
}

// ── 启动：恢复循环指令 + 启动调度器 ──────────────────────
loadLoops()
setInterval(loopTick, LOOP_TICKER_MS)

for (const b of binds) {
  const srv = http.createServer((req, res) => {
    if (!allowedRemote(req.socket.remoteAddress)) {
      log('拒绝：来源不在白名单 ' + req.socket.remoteAddress)
      return send(res, 403, { ok: false, message: 'forbidden' })
    }
    requestHandler(req, res)
  })
  srv.on('error', (e) => log('监听 ' + b + ':' + PORT + ' 失败: ' + e.message))
  srv.listen(PORT, b, () => log('监听 http://' + b + ':' + PORT))
}

if (subnetPrefix) {
  log('Docker 网段白名单: ' + subnetPrefix + 'x.x')
} else {
  log('!! 未探测到 Docker 网桥网关，只监听 127.0.0.1（AstrBot 容器可能连不上）')
}
