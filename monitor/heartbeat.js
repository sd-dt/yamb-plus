#!/usr/bin/env node
// 每小时时报 v3 —— 双号版 + yamb 状态行
// 设计要点：
//   1. 机器人离线时【发不出消息】—— "该来的消息没来"本身就是告警
//   2. 两个号都探测；只要有一个在线就照常发时报（优先用 A 发，A 挂了用 B 发）
//   3. 时报正文里带上两个号的状态、备用号发送自检结果、以及 yamb 挂机状态
//      —— 2026-09-26 出过「yamb 被 !mcbot stop 停掉，1.5 小时没人知道」，就是为堵这个坑
const fs = require('fs')
const { execSync } = require('child_process')

const NL = String.fromCharCode(10)
const { env, envAbs, repoPath } = require('../lib/env')
const fleetMod = require('../lib/fleet')
const TOKEN = env.require('NAPCAT_TOKEN')
const ACCOUNTS = fleetMod.NAPCAT.map((n) => ({ key: n.key, name: n.name, port: n.port, container: n.container }))
const NOTIFY_USER = fleetMod.NOTIFY_USER
const NOTIFY_GROUP = fleetMod.NOTIFY_GROUP
const STATE = envAbs('STATE_FILE', repoPath('data', 'state.json'))

const ts = () => new Date().toLocaleString('zh-CN', { hour12: false })
const log = (...a) => console.log('[' + ts() + ']', ...a)

async function get (port, ep) {
  const ctl = new AbortController()
  const t = setTimeout(() => ctl.abort(), 8000)
  try {
    const r = await fetch('http://127.0.0.1:' + port + '/' + ep, { headers: { Authorization: 'Bearer ' + TOKEN }, signal: ctl.signal })
    return await r.json()
  } finally { clearTimeout(t) }
}

async function post (port, ep, body) {
  const ctl = new AbortController()
  const t = setTimeout(() => ctl.abort(), 12000)
  try {
    const r = await fetch('http://127.0.0.1:' + port + '/' + ep, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(body),
      signal: ctl.signal
    })
    return await r.json()
  } finally { clearTimeout(t) }
}

/* 容器真实状态：docker 的 RestartCount 是"容器自己重启"的次数，
 * 和 monitor.js 里 napcatRestarts（监控主动救援的次数）是两码事，别混。 */
function containerLine (name) {
  try {
    const out = execSync('docker inspect -f "{{.RestartCount}}|{{.State.StartedAt}}|{{.State.OOMKilled}}" ' + name,
      { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
    const parts = out.split('|')
    const rc = parseInt(parts[0], 10)
    const mins = Math.max(0, Math.floor((Date.now() - new Date(parts[1]).getTime()) / 60000))
    const dd = Math.floor(mins / 1440)
    const hh = Math.floor((mins % 1440) / 60)
    const mm = mins % 60
    const dur = (dd ? dd + ' 天 ' : '') + ((hh || dd) ? hh + ' 小时 ' : '') + mm + ' 分'
    return '自重启 ' + (isNaN(rc) ? '?' : rc) + ' 次，已连续运行 ' + dur + (parts[2] === 'true' ? '  ⚠️ 曾被 OOM 杀' : '')
  } catch (e) {
    return '读不到 docker 信息（脚本可能无权访问 docker）'
  }
}

function memMB () {
  return parseInt(execSync("free -m | awk 'NR==2{print $7}'").toString().trim(), 10)
}

/* yamb（MC 挂机）状态：运行中(账号) / 已停止(原因, 时间)
 * 注意：qqbot-heartbeat 是 system 服务，环境里没有 XDG_RUNTIME_DIR，
 *       访问用户级 systemd 必须显式给上，否则报 Failed to connect to bus。 */
function yambStatus () {
  const sh = (c) => { try { return execSync(c, { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim() } catch (e) { return '' } }
  const SYSD = 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user '
  const act = sh(SYSD + 'is-active yamb.service 2>/dev/null || true')
  const running = act === 'active' || (!act && sh("pgrep -f 'yamb/dist/index.js' | head -1") !== '')

  if (running) {
    let who = ''
    try {
      const path = require('path')
      const dir = path.join(envAbs('YAMB_DIR', repoPath('yamb')), 'config', 'bots')
      const on = fs.readdirSync(dir)
        .filter((f) => /\.ya?ml$/.test(f))
        .filter((f) => /^enabled:\s*true/m.test(fs.readFileSync(path.join(dir, f), 'utf8')))
        .map((f) => f.replace(/\.ya?ml$/, ''))
      who = on.length ? '(' + on.join(',') + ')' : '(没有启用的账号)'
    } catch (e) { who = '' }
    return '运行中' + who
  }

  let why = ''
  const info = sh(SYSD + 'show yamb.service -p Result -p ExecMainStatus -p ExecMainCode -p InactiveEnterTimestamp 2>/dev/null || true')
  if (info) {
    const g = (k) => { const m = info.match(new RegExp('^' + k + '=(.*)$', 'm')); return m ? m[1].trim() : '' }
    const res = g('Result')
    const code = g('ExecMainStatus')
    const at = g('InactiveEnterTimestamp').replace(/^[A-Za-z]{3}\s+/, '').slice(0, 16)
    let reason
    if (res === 'success' || res === '') reason = '主动停止/正常退出'
    else if (res === 'oom-kill') reason = '内存超限被杀'
    else if (res === 'signal') reason = '被信号杀掉'
    else reason = res + (code && code !== '0' ? '(退出码' + code + ')' : '')
    why = '（' + reason + (at ? '，' + at : '') + '）'
  }
  return '已停止' + why
}

;(async () => {
  let st = {}
  try { st = JSON.parse(fs.readFileSync(STATE, 'utf8')) } catch (e) {}

  const probes = []
  for (const acc of ACCOUNTS) {
    let online = false
    let who = ''
    try {
      const j = await get(acc.port, 'get_login_info')
      if (j && j.status === 'ok' && j.data && j.data.user_id) { online = true; who = String(j.data.user_id) }
    } catch (e) { log(acc.key + ' 状态查询失败: ' + e.message) }
    probes.push({ key: acc.key, name: acc.name, port: acc.port, online: online, who: who })
  }

  const up = probes.filter((p) => p.online)
  log('探测：' + probes.map((p) => p.key + '=' + (p.online ? '在线' : '离线')).join(' '))

  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const timeStr = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':00'

  if (up.length === 0) {
    log('!! 两个号都不在线，跳过时报（消息没来 = 告警）')
    st.heartbeatMiss = (st.heartbeatMiss || 0) + 1
    st.lastHeartbeat = timeStr + ' (双号离线未发)'
    try { fs.writeFileSync(STATE, JSON.stringify(st, null, 2)) } catch (e) {}
    process.exit(1)
  }

  const avail = memMB()
  const text = [
    'QQBot 时报 ' + timeStr,
    ACCOUNTS[0].key + '(' + ACCOUNTS[0].name + ')：' + (probes[0] && probes[0].online ? '在线' : '离线'),
    ACCOUNTS[1].key + '(' + ACCOUNTS[1].name + ')：' + (probes[1] && probes[1].online ? '在线' : '离线') + (probes[1] && probes[1].online ? '' : '  <- 备用号离线，建议抽空重扫'),
    '备用号发送自检：' + (st.spareSendOk === undefined ? '未做过' : (st.spareSendOk ? '正常 ' + (st.spareSendAt || '') : '异常! ' + (st.spareSendNote || ''))),
    'yamb 挂机：' + yambStatus(),
    '可用内存：' + avail + ' MB',
    'NapCat 容器：' + containerLine((ACCOUNTS[0] && ACCOUNTS[0].container) || 'napcat'),
    'AstrBot 容器：' + containerLine('astrbot'),
    // _p62: 近 24h 内没有自救重启则隐藏该行，避免长期挂着历史记录
    ...((st.lastRestartAt && (Date.now() - Number(st.lastRestartAt) < 24 * 3600 * 1000))
      ? ['监控自救重启：' + (st.napcatRestarts || 0) + ' 次（最近 ' + new Date(st.lastRestartAt).toLocaleString('zh-CN', { hour12: false }) + '） —— 只在"双号都不可达"时由监控主动执行']
      : []),
    '内存告警：' + (st.memAlerts || 0) + ' 次'
  ].join(NL)

  log('准备发送:' + NL + text)

  let ok = false
  for (const p of up) {
    try {
      const r1 = await post(p.port, 'send_private_msg', { user_id: NOTIFY_USER, message: text })
      ok = !!(r1 && (r1.status === 'ok' || r1.retcode === 0))
      log(p.key + ' 私聊结果: ' + JSON.stringify(r1))
      if (ok) break
    } catch (e) { log(p.key + ' 私聊异常: ' + e.message) }
  }

  if (!ok) {
    for (const p of up) {
      try {
        const r2 = await post(p.port, 'send_group_msg', { group_id: NOTIFY_GROUP, message: text })
        ok = !!(r2 && (r2.status === 'ok' || r2.retcode === 0))
        log(p.key + ' 群结果: ' + JSON.stringify(r2))
        if (ok) break
      } catch (e) { log(p.key + ' 群异常: ' + e.message) }
    }
  }

  st.lastHeartbeat = timeStr + (ok ? ' 已发送' : ' 发送失败')
  st.heartbeatCount = (st.heartbeatCount || 0) + 1
  st.onlineCount = up.length
  try { fs.writeFileSync(STATE, JSON.stringify(st, null, 2)) } catch (e) {}
  log(ok ? 'OK 时报已发送' : 'NG 时报发送失败')
  process.exit(ok ? 0 : 1)
})().catch(e => { log('异常: ' + e.message); process.exit(1) })
