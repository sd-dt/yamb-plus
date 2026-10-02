#!/usr/bin/env node
// 每日日报：汇总 MC 登录结果 + 监控状态，推送到 QQ
const fs = require('fs')

const { env, envAbs, repoPath } = require('../lib/env')
const fleetMod = require('../lib/fleet')
const TOKEN = env.require('NAPCAT_TOKEN')
const API = 'http://' + env('NAPCAT_HOST', '127.0.0.1') + ':' + env.int('NAPCAT_PRIMARY_PORT', 3000)
const PRIVATE_TARGET = fleetMod.NOTIFY_USER
const GROUP_TARGET = fleetMod.NOTIFY_GROUP
const STATE_PATH = envAbs('STATE_FILE', repoPath('data', 'state.json'))

const mcLogPath = process.argv[2] || envAbs('MC_LOG_FILE', repoPath('data', 'last-run.log'))
function read (p) { try { return fs.readFileSync(p, 'utf8') } catch (e) { return '' } }

function parseMc (text) {
  const lines = text.split('\n')
  const pay = {}, warn = [], accounts = []
  let total = '', inSummary = false
  for (const raw of lines) {
    const line = raw.replace(/^\[[^\]]+\]\s*/, '').trim()
    let m
    m = raw.match(/【([^】]+)】【聊天】.*你已支付\s+(\S+)\s+([\d.]+)/)
    if (m) pay[m[1]] = '已转 ' + m[3] + ' 给 ' + m[2]
    if (/余额(过低|不足)/.test(raw)) {
      m = raw.match(/【([^】]+)】【聊天】(.*)$/)
      if (m) warn.push(m[1] + '：' + m[2].trim())
    }
    if (line.indexOf('本次任务汇总') >= 0) { inSummary = true; continue }
    if (inSummary) {
      if (line.indexOf('合计') === 0) { total = line; inSummary = false; continue }
      if (line.indexOf('====') === 0) continue
      m = line.match(/^(\S+)\s{2,}(.+)$/)
      if (m) accounts.push({ name: m[1], status: m[2] })
    }
  }
  return { accounts: accounts, total: total, pay: pay, warn: warn }
}

async function post (ep, body) {
  const r = await fetch(API + '/' + ep, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    body: JSON.stringify(body)
  })
  return await r.json()
}

;(async () => {
  const mc = parseMc(read(mcLogPath))
  const st = JSON.parse(read(STATE_PATH) || '{}')
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const dateStr = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())

  const L = []
  L.push('📋 QQBot 每日日报 ' + dateStr)
  L.push('')
  L.push('━━━ MC 登录 ━━━')
  if (mc.accounts.length === 0) {
    L.push('  （未解析到登录结果，请查看 daily.log）')
  } else {
    for (const a of mc.accounts) L.push('  ' + a.name + '  ' + (a.status.indexOf('成功') === 0 ? 'OK ' : 'NG ') + a.status)
    if (mc.total) L.push('  ' + mc.total)
  }
  L.push('')
  L.push('━━━ 转账 ━━━')
  if (Object.keys(mc.pay).length === 0) L.push('  （未检测到转账记录）')
  else for (const k of Object.keys(mc.pay)) L.push('  ' + k + '  OK  ' + mc.pay[k])
  if (mc.warn.length > 0) {
    L.push('  !! 余额告警：')
    for (const w of mc.warn) L.push('    ' + w)
  }
  L.push('')
  L.push('━━━ 系统 ━━━')
  L.push('  QQ 机器人：' + (st.lastOnline === true ? '在线' : st.lastOnline === false ? '离线' : '未知'))
  L.push('  可用内存：' + (st.lastMemMB != null ? st.lastMemMB : '?') + ' MB（24h 最低 ' + (st.memMinMB === 99999 ? '?' : st.memMinMB) + ' MB）')
  L.push('  NapCat 自动重启：' + (st.napcatRestarts || 0) + ' 次')
  L.push('  内存告警：' + (st.memAlerts || 0) + ' 次')
  if (st.lastNote) L.push('  备注：' + st.lastNote)

  const text = L.join('\n')
  console.log('---- 准备推送的日报 ----')
  console.log(text)
  console.log('------------------------')

  let ok = false
  try {
    const r1 = await post('send_private_msg', { user_id: PRIVATE_TARGET, message: text })
    console.log('私聊推送结果:', JSON.stringify(r1))
    ok = !!(r1 && (r1.status === 'ok' || r1.retcode === 0))
  } catch (e) { console.log('私聊推送异常:', e.message) }

  if (!ok) {
    try {
      const r2 = await post('send_group_msg', { group_id: GROUP_TARGET, message: text })
      console.log('群推送结果:', JSON.stringify(r2))
      ok = !!(r2 && (r2.status === 'ok' || r2.retcode === 0))
    } catch (e) { console.log('群推送异常:', e.message) }
  }

  console.log(ok ? 'OK 日报推送成功' : 'NG 日报推送失败')
})().catch(e => { console.log('report 异常:', e.message); process.exit(1) })
