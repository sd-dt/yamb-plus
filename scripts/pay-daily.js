#!/usr/bin/env node
/* _p69: 每天凌晨自动登录转钱小号
 * 流程：临时下线挂机号 -> 登录小号（改 yaml enabled + yamb 热上下号）
 *       -> 轮询等进服 -> 发转钱指令 -> 下线小号 -> 恢复挂机号
 * 失败排到队尾重试；每个小号每天最多尝试 PAY_MAX_TRIES（默认 4）次。
 * 配置：PAY_DIR/pay.json（收款人/金额/账号名单/端口），状态：PAY_DIR/pay-state.json
 * 与主系统的全部交互走 HTTP，密钥从环境变量取（不写死在代码里）：
 *   PAY_CTRL_KEY      控制器(:15100)密钥            [必填]
 *   PAY_YAMB_KEY      yamb 控制口(:15199)密钥       [必填]
 *   PAY_NAPCAT_TOKEN  NapCat HTTP token（不填则只打日志，不私聊通知）
 * 可覆盖：PAY_CTRL_PORT / PAY_YAMB_PORT / PAY_NAPCAT_PORT / PAY_DIR /
 *         PAY_BOTS_DIR / PAY_MAX_TRIES / PAY_INGAME_TIMEOUT_MS / PAY_RETRY_WAIT_MS
 */
'use strict'
const http = require('http')
const fs = require('fs')
const path = require('path')

const CTRL_PORT = parseInt(process.env.PAY_CTRL_PORT || '15100', 10)
const CTRL_KEY = process.env.PAY_CTRL_KEY || ''
const YAMB_PORT = parseInt(process.env.PAY_YAMB_PORT || '15199', 10)
const YAMB_KEY = process.env.PAY_YAMB_KEY || ''
const NAPCAT_PORT = parseInt(process.env.PAY_NAPCAT_PORT || '3000', 10)
const NAPCAT_TOKEN = process.env.PAY_NAPCAT_TOKEN || ''
const PAY_DIR = process.env.PAY_DIR || '/opt/qqbot/astrbot-data/plugins/astrbot_plugin_mcbot'
const PAY_FILE = path.join(PAY_DIR, 'pay.json')
const STATE_FILE = path.join(PAY_DIR, 'pay-state.json')
const LOCK_FILE = path.join(PAY_DIR, 'pay.lock')
const BOTS_DIR = process.env.PAY_BOTS_DIR || '/opt/qqbot/yamb/config/bots'
const MAX_TRIES = parseInt(process.env.PAY_MAX_TRIES || '4', 10)
const DEFAULT_CMD = '/pay {to} {amount}'
const INGAME_TIMEOUT_MS = parseInt(process.env.PAY_INGAME_TIMEOUT_MS || String(10 * 60 * 1000), 10)
const POLL_MS = 15 * 1000
const RETRY_WAIT_MS = parseInt(process.env.PAY_RETRY_WAIT_MS || String(60 * 1000), 10)
const STALE_LOCK_MS = 3 * 60 * 60 * 1000

if (!CTRL_KEY || !YAMB_KEY) {
  console.error('[pay] 缺少 PAY_CTRL_KEY / PAY_YAMB_KEY 环境变量，退出')
  process.exit(2)
}

const ts = () => new Date().toLocaleString('zh-CN', { hour12: false })
const log = (...a) => console.log('[' + ts() + ']', ...a)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function readJson (f, dft) {
  try { return JSON.parse(fs.readFileSync(f, 'utf8')) } catch (e) { return dft }
}

function todayStr () {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
}

function req (port, method, p, headers, body, timeoutMs) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : JSON.stringify(body)
    const h = Object.assign({}, headers)
    if (payload) {
      h['Content-Type'] = 'application/json'
      h['Content-Length'] = Buffer.byteLength(payload)
    }
    const r = http.request({ host: '127.0.0.1', port, path: p, method, headers: h, timeout: timeoutMs || 200000 }, (res) => {
      let d = ''
      res.on('data', (c) => { d += c })
      res.on('end', () => {
        try { resolve(JSON.parse(d)) } catch (e) { reject(new Error('响应不是 JSON：' + d.slice(0, 120))) }
      })
    })
    r.on('timeout', () => r.destroy(new Error('请求超时')))
    r.on('error', reject)
    if (payload) r.write(payload)
    r.end()
  })
}

const ctrl = (method, p, body) => req(CTRL_PORT, method, p, { 'x-api-key': CTRL_KEY }, body)
const yambCtrl = (method, p, body) => req(YAMB_PORT, method, p, { 'x-api-key': YAMB_KEY }, body)
const yambStatus = (port) => req(port, 'GET', '/api/status', { 'x-api-key': YAMB_KEY }, undefined, 15000)

/* ── 小号上下号：改 yaml enabled 标记 + yamb 控制口热操作（与 switch.sh 的 hot_apply 同路数）── */
function yambAlive () {
  try {
    const { execSync } = require('child_process')
    const out = execSync("pgrep -f 'yamb/dist/index[.]js' | head -1",
      { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
    return out !== ''
  } catch (e) { return false }
}

function setYamlEnabled (account, flag) {
  const f = path.join(BOTS_DIR, account + '.yaml')
  let txt = fs.readFileSync(f, 'utf8')
  const want = 'enabled: ' + (flag ? 'true' : 'false')
  if (/^enabled:.*$/m.test(txt)) txt = txt.replace(/^enabled:.*$/m, want)
  else txt = want + '\n' + txt
  fs.writeFileSync(f, txt)
}

async function startInstance (acc) {
  setYamlEnabled(acc, true)
  // yamb 没在跑：直接拉起服务（小号此刻 enabled:true，yamb 满足"至少一个启用 bot"可正常启动并自动带上小号）
  if (!yambAlive()) {
    const { execFileSync } = require('child_process')
    try {
      execFileSync('systemctl', ['--user', 'start', 'yamb.service'], {
        env: Object.assign({}, process.env, { XDG_RUNTIME_DIR: process.env.XDG_RUNTIME_DIR || '/run/user/1000' }),
        timeout: 90000
      })
      log('yamb 未在运行 -> 已随小号 ' + acc + ' 启动')
      // _p73b: systemctl(Type=simple) 立即返回，但 yamb 启动需数秒、控制口 15199 可能还没监听
      // -> 不再直接 return，落到下面的控制口调用（若 yamb 已自动带上小号，会回"已在运行"）
    } catch (e) {
      log('⚠️ 直接拉起 yamb 失败，转控制口热启动: ' + e.message)
    }
  }
  // _p73b: 控制口可能尚未就绪（yamb 刚被拉起），ECONNREFUSED 时重试等待
  const waitCtrlMs = parseInt(process.env.PAY_CTRL_WAIT_MS || String(30 * 1000), 10)
  const deadline = Date.now() + waitCtrlMs
  let warned = false
  let r = null
  for (;;) {
    try { r = await yambCtrl('POST', '/api/instances/start', { account: acc }); break } catch (e) {
      const refused = String(e.message).indexOf('ECONNREFUSED') >= 0
      if (!refused || Date.now() >= deadline) {
        setYamlEnabled(acc, false)
        throw new Error('yamb 上线请求失败：' + e.message)
      }
      if (!warned) { log('yamb 控制口尚未就绪，等待中（最多 ' + Math.round(waitCtrlMs / 1000) + 's）…'); warned = true }
      await sleep(1000)
    }
  }
  const s = JSON.stringify(r || {})
  if ((r && r.ok) || s.indexOf('已在运行') >= 0) return
  setYamlEnabled(acc, false)
  throw new Error('yamb 上线失败：' + s.slice(0, 160))
}

async function stopInstance (acc) {
  try { await yambCtrl('POST', '/api/instances/stop', { account: acc }) } catch (e) { /* 尽力而为 */ }
  try { setYamlEnabled(acc, false) } catch (e) { log('⚠️ ' + acc + ' yaml 停用失败：' + e.message) }
}

/* ── 单个小号完整流程；返回 { ok, note, replies } ── */
async function payOne (acc, cfg) {
  // 0) 临时下线其他挂机号：保证 /command 走小号，也不受 MAX_ONLINE=2 限制
  try {
    const s0 = await ctrl('GET', '/status')
    const on = (s0.enabledList || []).filter((a) => a !== acc)
    if (on.length) {
      log('临时下线挂机号：' + on.join('、'))
      await ctrl('POST', '/switch', { mode: 'disable', accounts: on })
      await sleep(5000)
    }
  } catch (e) {
    throw new Error('查询/下线挂机号失败：' + e.message)
  }

  // 1) 上线（若上次运行残留了 enabled:true / 在跑的实例，startInstance 能兜住）
  await startInstance(acc)
  log(acc + ' 已发起上线，等待进服…')

  // 2) 等进服（实例端口 /api/status 出现 "minecraft":true）
  const port = (cfg.ports || {})[acc]
  if (!port) throw new Error('pay.json 里没有 ' + acc + ' 的端口（ports 缺失）')
  const deadline = Date.now() + INGAME_TIMEOUT_MS
  let ingame = false
  while (Date.now() < deadline) {
    await sleep(POLL_MS)
    try {
      const j = await yambStatus(port)
      if (j && j.minecraft === true) { ingame = true; break }
      log(acc + ' 还没进服（' + Math.round((deadline - Date.now()) / 1000) + 's 后超时）')
    } catch (e) { /* 实例没起来/没监听，继续等 */ }
  }
  if (!ingame) {
    await stopInstance(acc)
    throw new Error(Math.round(INGAME_TIMEOUT_MS / 60000) + ' 分钟内没进服')
  }

  // 3) 转钱：刚进服稳几秒再发，指令带 account 以明确走小号
  await sleep(8000)
  const cmd = String(cfg.command || DEFAULT_CMD)
    .replace('{to}', cfg.pay_to).replace('{amount}', cfg.pay_amount)
  const r = await ctrl('POST', '/command', { command: cmd, account: acc })
  if (!r.ok) {
    await stopInstance(acc)
    throw new Error('发送转钱指令失败：' + String(r.message || '未知').slice(0, 160))
  }
  const replies = (r.replies || []).filter(Boolean).slice(0, 5).join(' | ')
  log(acc + ' 转钱指令已发：' + cmd + (replies ? '  服务器回复：' + replies : ''))
  await sleep(10000)

  // 4) 下线小号（挂机号在 run() 结尾统一恢复）
  await stopInstance(acc)
  return { ok: true, note: cmd, replies }
}

async function napcatSend (text) {
  const cfg = readJson(PAY_FILE, {})
  const user = parseInt(String(cfg.notify_user || ''), 10)
  if (!user) { log('pay.json 未配置 notify_user，跳过私聊通知'); return }
  if (!NAPCAT_TOKEN) { log('未配置 PAY_NAPCAT_TOKEN，跳过私聊通知'); return }
  try {
    const r = await req(NAPCAT_PORT, 'POST', '/send_private_msg',
      { Authorization: 'Bearer ' + NAPCAT_TOKEN }, { user_id: user, message: text }, 20000)
    log('通知结果: ' + JSON.stringify(r).slice(0, 160))
  } catch (e) { log('通知失败: ' + e.message) }
}

function saveState (st) {
  try { fs.writeFileSync(STATE_FILE, JSON.stringify(st, null, 2) + '\n') } catch (e) { log('状态写盘失败: ' + e.message) }
}

async function run (only) {
  const cfg = readJson(PAY_FILE, {})
  const all = Array.isArray(cfg.accounts) ? cfg.accounts.filter((a) => a && !/\s|[\/\\]/.test(a)) : []
  // _p73: 单号开关（paySwitch[a] !== false 缺省=开）；paytest 手动点名不受开关限制
  const paySwitch = cfg.paySwitch || {}
  const active = all.filter((a) => paySwitch[a] !== false)
  let accounts = only ? (all.indexOf(only) >= 0 ? [only] : []) : active
  if (!accounts.length) {
    log(only ? ('没有叫 ' + only + ' 的转钱小号，退出')
      : (all.length ? '小号全部处于暂停状态，退出（用 !mcbot payon <账号> 恢复）'
        : '没有登记转钱小号，退出（用 !mcbot addpay <账号> 登记）'))
    return
  }
  if (!cfg.pay_to || !cfg.pay_amount) {
    log('未设置收款人/金额，跳过（用 !mcbot payset <收款人> <金额> 设置）')
    await napcatSend('💰 转钱任务跳过：还没有设置收款人和金额。\n设置方法：!mcbot payset <收款人> <金额>')
    return
  }

  const today = todayStr()
  const st = readJson(STATE_FILE, { date: '', tries: {}, done: [] })
  if (st.date !== today) { st.date = today; st.tries = {}; st.done = [] }
  st.tries = st.tries || {}
  st.done = Array.isArray(st.done) ? st.done : []

  const queue = accounts.filter((a) => st.done.indexOf(a) < 0)
  if (!queue.length) { log('今天的小号都已转完，无事可做'); return }

  // 记住之前在线的挂机号，全部结束后恢复
  let mains = []
  try {
    const s0 = await ctrl('GET', '/status')
    mains = (s0.enabledList || []).filter((a) => accounts.indexOf(a) < 0)
  } catch (e) { log('⚠️ 记录挂机号失败：' + e.message) }

  const report = []
  const started = Date.now()

  while (queue.length) {
    const acc = queue.shift()
    if ((st.tries[acc] || 0) >= MAX_TRIES) { report.push('❌ ' + acc + '：今日已试满 ' + MAX_TRIES + ' 次，放弃'); continue }
    st.tries[acc] = (st.tries[acc] || 0) + 1
    const n = st.tries[acc]
    log('尝试 ' + acc + '（第 ' + n + '/' + MAX_TRIES + ' 次）')
    let res
    try {
      res = await payOne(acc, cfg)
    } catch (e) {
      res = { ok: false, note: String(e.message || e), replies: '' }
    }
    if (res.ok) {
      st.done.push(acc)
      report.push('✅ ' + acc + '：转钱完成（第 ' + n + ' 次尝试）' + (res.replies ? '\n   服务器回复：' + res.replies : ''))
    } else {
      report.push('⚠️ ' + acc + '：第 ' + n + ' 次失败 — ' + res.note)
      if (n < MAX_TRIES) {
        queue.push(acc) // 排到队尾，稍后重试
        saveState(st)
        log(acc + ' ' + Math.round(RETRY_WAIT_MS / 1000) + ' 秒后重试')
        await sleep(RETRY_WAIT_MS)
      } else {
        report.push('❌ ' + acc + '：今日 ' + MAX_TRIES + ' 次都失败，放弃（明天 04:00 再试）')
      }
    }
    saveState(st)
  }

  // 恢复之前的挂机号
  if (mains.length) {
    log('恢复挂机号：' + mains.join('、'))
    try { await ctrl('POST', '/switch', { mode: 'enable', accounts: mains }) } catch (e) {
      log('⚠️ 恢复挂机号失败：' + e.message + '（可用 !mcbot enable 手动恢复）')
      report.push('⚠️ 挂机号恢复失败（' + mains.join('、') + '），可手动 !mcbot enable ' + mains.join(' '))
    }
  }

  const mins = Math.round((Date.now() - started) / 60000)
  const msg = ['💰 转钱日报（' + today + '，用时 ' + mins + ' 分钟）'].concat(report).join('\n')
  log(msg)
  await napcatSend(msg)
}

async function main () {
  const argv = process.argv.slice(2)
  let only = null
  const ai = argv.indexOf('--account')
  if (ai >= 0 && argv[ai + 1]) only = argv[ai + 1]

  // 锁：防止定时任务与 paytest 同时跑；超过 3 小时的旧锁视为残留
  try {
    if (fs.existsSync(LOCK_FILE)) {
      const age = Date.now() - fs.statSync(LOCK_FILE).mtimeMs
      if (age < STALE_LOCK_MS) { log('已有一轮转钱在跑（pay.lock），退出'); return }
      log('清除超过 3 小时的残留锁')
      fs.unlinkSync(LOCK_FILE)
    }
    const fd = fs.openSync(LOCK_FILE, 'wx')
    fs.writeSync(fd, String(process.pid))
    fs.closeSync(fd)
  } catch (e) {
    log('拿锁失败，退出: ' + e.message)
    return
  }
  try {
    await run(only)
  } finally {
    try { fs.unlinkSync(LOCK_FILE) } catch (e) { /* 已不在 */ }
  }
}

main().then(() => process.exit(0)).catch((e) => { log('异常退出: ' + (e.stack || e.message)); process.exit(1) })
