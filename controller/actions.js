'use strict'
/*
 * 动作转发 + 连点调度（控制器层）
 *
 *   POST /action { account, act, ...参数, interval?|continuous?, seconds? }
 *        · act 走白名单，转发给对应账号的 yamb：POST /api/action
 *        · interval  : 每 N tick 重复一次（1 tick = 50ms）
 *        · continuous: 每 tick 重复一次（等同 interval 1，但下限 2 tick 防失控）
 *        · seconds   : 自动停止时间（可省）
 *        · act=stop  : 立刻停掉这个账号的所有连点 + 让 yamb 松手
 *
 *   设计要点：连点逻辑留在【我们自己的层】，
 *            所以 yamb 只需要"无状态动作"接口 —— 以后加玩法不用再动 yamb ✓
 *   安全：每个账号最多 2 个连点在跑；interval 下限 2 tick；stop 一键全停
 */

const http = require('http')

const PORTS = require('../lib/fleet').PORTS
const YAMB_KEY = require('../lib/env').env.require('YAMB_API_KEY')
const TICK_MS = 50
const MAX_LOOPS = 2
const MIN_INTERVAL_TICKS = 2

// 单次动作白名单（连点只允许这些；其余一次性执行）
const ACTS = [
  'attack', 'use', 'attackentity', 'useentity',
  'dig', 'useblock', 'jump', 'sneak', 'sprint',
  'move', 'look', 'pos', 'drop', 'stop'
]

const loops = new Map() // account -> { name, timer, autoStop }

function yambAction (account, payload, timeoutMs) {
  return new Promise((resolve) => {
    const port = PORTS[account]
    if (!port) return resolve({ success: false, message: '未知账号：' + account })
    const body = JSON.stringify(payload)
    const req = http.request({
      host: '127.0.0.1', port: port, path: '/api/action', method: 'POST',
      headers: {
        'x-api-key': YAMB_KEY,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body)
      }
    }, (res) => {
      let d = ''
      res.on('data', (c) => { d += c })
      res.on('end', () => {
        try { resolve(JSON.parse(d)) } catch (e) { resolve({ success: false, message: 'yamb 返回非 JSON: ' + d.slice(0, 120) }) }
      })
    })
    req.on('error', (e) => resolve({ success: false, message: 'yamb 无响应: ' + e.message }))
    req.setTimeout(timeoutMs || 8000, () => req.destroy(new Error('超时')))
    req.write(body)
    req.end()
  })
}

function clearLoop (account, log) {
  const cur = loops.get(account)
  if (!cur) return false
  clearInterval(cur.timer)
  if (cur.autoStop) clearTimeout(cur.autoStop)
  loops.delete(account)
  if (log) log('已停止连点：' + account + '（' + cur.name + '）')
  return true
}

function clearAll (log) {
  const names = Array.from(loops.keys())
  for (const a of names) clearLoop(a, log)
  return names
}

function normalize (body) {
  const account = String(body.account || '').trim()
  const act = String(body.act || '').trim().toLowerCase()
  const out = { account: account, act: act }
  for (const k of ['target', 'dir']) if (body[k] !== undefined) out[k] = String(body[k])
  for (const k of ['x', 'y', 'z', 'yaw', 'pitch']) if (body[k] !== undefined && body[k] !== '') out[k] = Number(body[k])
  return out
}

async function handleAction (res, body, send, log) {
  const p = normalize(body)
  if (!p.account) return send(res, 400, { ok: false, message: '缺 account' })
  if (!PORTS[p.account]) return send(res, 400, { ok: false, message: '未知账号：' + p.account + '（可用：' + Object.keys(PORTS).join(', ') + '）' })
  if (!p.act) return send(res, 400, { ok: false, message: '缺 act' })
  if (ACTS.indexOf(p.act) < 0) return send(res, 400, { ok: false, message: '不支持的动作：' + p.act + '（可用：' + ACTS.join(', ') + '）' })

  // ① stop：一键全停
  if (p.act === 'stop') {
    const had = clearLoop(p.account, log)
    const r = await yambAction(p.account, { act: 'stop' })
    return send(res, 200, { ok: true, message: (had ? '已停止连点；' : '') + (r.message || ''), yamb: r })
  }

  // ② 连点（continuous / interval）
  const cont = body.continuous === true
  const ticks = cont ? 1 : (body.interval !== undefined ? Number(body.interval) : 0)
  if (cont || (ticks && ticks > 0)) {
    const every = Math.max(MIN_INTERVAL_TICKS, Math.round(ticks || 1))
    if (loops.size >= MAX_LOOPS && !loops.has(p.account)) {
      return send(res, 200, { ok: false, message: '连点太多了（全局上限 ' + MAX_LOOPS + ' 个），先 !mcbot <账号> stop' })
    }
    const name = p.act + (p.target ? ' ' + p.target : '') + ' 每 ' + every + ' tick'
    clearLoop(p.account, log)
    const first = await yambAction(p.account, p)
    if (!first.success) return send(res, 200, { ok: false, message: first.message || '动作执行失败', yamb: first })
    const timer = setInterval(() => { yambAction(p.account, p).catch(() => {}) }, every * TICK_MS)
    let autoStop = null
    const secs = Number(body.seconds) || 0
    if (secs > 0) autoStop = setTimeout(() => { clearLoop(p.account, log) }, Math.min(secs, 3600) * 1000)
    loops.set(p.account, { name: name, timer: timer, autoStop: autoStop })
    if (log) log('开始连点：' + p.account + ' ' + name + (secs > 0 ? ('（' + secs + ' 秒后自动停）') : ''))
    return send(res, 200, { ok: true, message: '已开始：' + name + (secs > 0 ? ('，' + secs + ' 秒后自动停') : '（用 stop 停）'), yamb: first })
  }

  // ③ 单次动作（如果这个账号正在连点，先停掉，避免打架）
  const wasLooping = clearLoop(p.account, log)
  const r = await yambAction(p.account, p)
  return send(res, 200, {
    ok: !!r.success,
    message: (wasLooping ? '（已停掉原有连点）' : '') + (r.message || '动作未执行'),
    yamb: r
  })
}

function status () {
  const out = []
  for (const [acc, v] of loops.entries()) out.push({ account: acc, action: v.name })
  return out
}

module.exports = { handleAction: handleAction, status: status, clearAll: clearAll, ACTS: ACTS, PORTS: PORTS }
