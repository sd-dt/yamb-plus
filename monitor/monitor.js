#!/usr/bin/env node
/* QQ 机器人监控 v4 —— 双号热备版
 *
 * 相比 v3 的变化：
 *   1. 同时探测【两个 QQ 号】：
 *        A = 主号（fleet.json napcat[0]）  本地 API 127.0.0.1:3000
 *        B = 备用号（fleet.json napcat[1]）本地 API 127.0.0.1:3001
 *   2. 告警策略（用户定稿）：只要还有一个号在线，就【完全安静】；
 *      两个号都挂了才发邮件告警。
 *   3. 两个号都掉线时，二维码邮件里会【同时带上两个号的二维码】，
 *      一次扫码就能把两个号都救回来。
 *   4. 自动重启只在【两个号都已掉线、且有号不可达】时才执行，
 *      且只重启真正不可达的那个容器（重启会掉登录，代价大）。
 *   5. 每个号单独记录掉线起点/原因，state.json 里新增 accStatus 供日报读取。
 *
 * 自测： node monitor/monitor.js --test-qr    只发一封二维码邮件，不改状态
 *      node monitor/monitor.js --probe      只打印两号状态，不发任何通知
 */
const fs = require('fs')
const { execSync } = require('child_process')

const NL = String.fromCharCode(10)

let sendMail = async () => ({ ok: false, reason: 'mailer 未加载' })
try {
  sendMail = require('./mailer').sendMail
} catch (e) {
  console.log('[monitor] 警告：mailer.js 未加载，本次只走 QQ 通知 ——', e.message)
}

const { env, envAbs, repoPath } = require('../lib/env')
const fleetMod = require('../lib/fleet')
const STATE_PATH = envAbs('STATE_FILE', repoPath('data', 'state.json'))
const TOKEN = env.require('NAPCAT_TOKEN')
const ACCOUNTS = fleetMod.NAPCAT
const RESTART_COOLDOWN_MS = 20 * 60 * 1000
const ALERT_COOLDOWN_MS = 30 * 60 * 1000
const QR_ALERT_INTERVAL_MS = 10 * 60 * 1000
const MEM_ALERT_MB = 150
const FAIL_THRESHOLD = 2
const NOTIFY_USER = fleetMod.NOTIFY_USER
const NOTIFY_GROUP = fleetMod.NOTIFY_GROUP

const ts = () => new Date().toLocaleString('zh-CN', { hour12: false })
const log = (...a) => console.log('[' + ts() + ']', ...a)

function loadState () {
  try { return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8')) } catch (e) { return {} }
}
function saveState (s) { fs.writeFileSync(STATE_PATH, JSON.stringify(s, null, 2)) }

async function apiCall (port, endpoint, body, timeoutMs) {
  const ctl = new AbortController()
  const t = setTimeout(() => ctl.abort(), timeoutMs || 8000)
  try {
    const opts = { headers: { Authorization: 'Bearer ' + TOKEN }, signal: ctl.signal }
    if (body !== undefined) {
      opts.method = 'POST'
      opts.headers['Content-Type'] = 'application/json'
      opts.body = JSON.stringify(body)
    }
    const r = await fetch('http://127.0.0.1:' + port + '/' + endpoint, opts)
    const txt = await r.text()
    if (!txt) return null
    return JSON.parse(txt)
  } finally {
    clearTimeout(t)
  }
}

/* ── 通知：QQ 优先（拿起手机就能看到），邮件兜底（QQ 挂了也能到）── */
async function qqNotify (text) {
  for (const acc of ACCOUNTS) {
    try {
      const r1 = await apiCall(acc.port, 'send_private_msg', { user_id: NOTIFY_USER, message: text }, 12000)
      if (r1 && (r1.status === 'ok' || r1.retcode === 0)) {
        log('  私聊通知(' + acc.key + '): ok')
        return true
      }
      log('  私聊通知(' + acc.key + ') 失败:', JSON.stringify(r1))
    } catch (e) { log('  私聊通知(' + acc.key + ') 异常:', e.message) }
  }
  for (const acc of ACCOUNTS) {
    try {
      const r2 = await apiCall(acc.port, 'send_group_msg', { group_id: NOTIFY_GROUP, message: text }, 12000)
      if (r2 && (r2.status === 'ok' || r2.retcode === 0)) {
        log('  群通知(' + acc.key + '): ok')
        return true
      }
    } catch (e) { log('  群通知(' + acc.key + ') 异常:', e.message) }
  }
  return false
}

async function notifyBoth (subject, text, attachments) {
  const mail = await sendMail(subject, text, attachments)
  log('  邮件:', mail.ok ? ('成功 ' + mail.messageId) : ('失败 ' + mail.reason))
  const qq = await qqNotify(subject + NL + text)
  return { mail: mail.ok, qq: qq }
}

function memAvailableMB () {
  return parseInt(execSync("free -m | awk 'NR==2{print $7}'").toString().trim(), 10)
}

/** 取某个容器 NapCat 最新一张二维码：解码 URL + 生成时间 */
function latestQrInfo (container) {
  const info = { url: '', at: '', agoSec: -1 }
  try {
    const out = execSync('docker logs --tail 500 ' + container + ' 2>&1', {
      stdio: ['ignore', 'pipe', 'ignore'],
      maxBuffer: 16 * 1024 * 1024
    }).toString()
    for (const line of out.split(NL)) {
      const i = line.indexOf('https://txz.qq.com/p?k=')
      if (i < 0) continue
      let u = line.slice(i)
      const sp = u.indexOf(' ')
      if (sp >= 0) u = u.slice(0, sp)
      info.url = u
      info.at = line.slice(0, 17)   // "MM-DD HH:MM:SS"
    }
  } catch (e) { /* 忽略 */ }
  if (info.at) {
    try {
      const now = new Date()
      const mmdd = info.at.slice(0, 5)
      const hms = info.at.slice(6)
      const d = new Date(now.getFullYear() + '-' + mmdd + 'T' + hms)
      info.agoSec = Math.max(0, Math.round((now - d) / 1000))
    } catch (e) { /* 忽略 */ }
  }
  return info
}

/** 从容器里取出二维码图片（已登录时 NapCat 不生成，取不到是正常的） */
function fetchQrImage (container, tag) {
  const dest = '/tmp/napcat-qrcode-' + tag + '-' + Date.now() + '.png'
  try {
    execSync('docker cp ' + container + ':/app/napcat/cache/qrcode.png ' + dest, {
      stdio: ['ignore', 'ignore', 'ignore']
    })
    if (fs.existsSync(dest) && fs.statSync(dest).size > 0) return dest
    return ''
  } catch (e) {
    return ''
  }
}

/** 探测单个号 */
async function probe (acc, s) {
  const r = { key: acc.key, name: acc.name, uin: acc.uin, reachable: false, loggedIn: false, who: '', online: false, failStreak: 0 }
  s.fail = s.fail || {}
  try {
    const st = await apiCall(acc.port, 'get_status', undefined, 8000)
    r.reachable = !!st
    s.fail[acc.key] = 0
  } catch (e) {
    s.fail[acc.key] = (s.fail[acc.key] || 0) + 1
    log(acc.key + ' NapCat API 无响应（连续第 ' + s.fail[acc.key] + ' 次）: ' + e.message)
  }
  r.failStreak = s.fail[acc.key] || 0

  if (r.reachable) {
    try {
      const info = await apiCall(acc.port, 'get_login_info', undefined, 8000)
      if (info && info.status === 'ok' && info.data && info.data.user_id) {
        r.loggedIn = true
        r.who = info.data.nickname + '(' + info.data.user_id + ')'
      }
    } catch (e) {
      log(acc.key + ' get_login_info 异常: ' + e.message)
    }
  }
  r.online = r.loggedIn
  return r
}

function accLine (r) {
  return r.key + '[' + r.name + '] ' + (r.online ? '在线' : (r.reachable ? '未登录' : '不可达')) +
    (r.who ? ' ' + r.who : '')
}

/** 两号都掉线时的二维码邮件：一次带两个号的码 */
async function sendQrAlert (probes, opts) {
  const o = opts || {}
  const needScan = probes.filter((p) => p.reachable && !p.loggedIn)
  const dead = probes.filter((p) => !p.reachable && p.failStreak >= FAIL_THRESHOLD)

  const lines = []
  lines.push('QQ 机器人【两个号都掉线了】。')
  lines.push('在线情况：' + probes.map(accLine).join('  |  '))
  lines.push('')
  lines.push('只要还有一个号在线就不会打扰你，现在是两个都挂了，所以才发这封邮件。')
  lines.push('')

  const attachments = []
  for (const p of probes) {
    const acc = ACCOUNTS.filter((a) => a.key === p.key)[0]
    const title = p.key + ' —— ' + p.name + '(' + p.uin + ')'
    lines.push('====== ' + title + ' ======')
    if (!p.reachable) {
      lines.push('状态：NapCat 进程/接口不可达（连续 ' + p.failStreak + ' 次探测失败）')
      lines.push('这个号大概率不用扫码，等下面的自动重启把它拉起来就行。')
      lines.push('如果重启后仍然没登录，再按下面的方法扫码。')
      lines.push('')
    } else if (p.loggedIn) {
      lines.push('状态：正常在线，不需要扫码。')
      lines.push('')
      continue
    } else {
      lines.push('状态：进程活着但没登录 —— 会话被腾讯踢了，必须重新扫码。')
    }

    const qr = latestQrInfo(acc.container)
    if (qr.url) {
      lines.push('解码链接（手机点一下直接唤起 QQ 扫码）：')
      lines.push(qr.url)
      lines.push('生成时间：' + (qr.at || '未知') + (qr.agoSec >= 0 ? '（距今约 ' + qr.agoSec + ' 秒）' : ''))
      lines.push('有效期：约 2 分钟（NapCat 每 2 分钟自动换一张）')
      lines.push('!! 超过 2 分钟这张码就失效了 !!')
    } else {
      lines.push('（暂时取不到解码链接，NapCat 可能还没开始出码）')
    }

    const img = p.reachable ? fetchQrImage(acc.container, p.key) : ''
    if (img) {
      attachments.push({ filename: 'napcat-' + p.key + '-qrcode.png', path: img })
      lines.push('二维码图片见附件：napcat-' + p.key + '-qrcode.png（手机 QQ 扫一扫打开附件图片即可）')
    }
    lines.push('也可以等下一封邮件（每 10 分钟一封），或在电脑上执行：')
    lines.push('  bash ' + acc.qrScript)
    lines.push('')
  }

  lines.push('========================')
  lines.push('本机自查命令（在服务器上执行）：')
  lines.push('  bash ' + repoPath('monitor', 'status.sh'))
  for (const a of ACCOUNTS) lines.push('  docker logs --tail 60 ' + a.container)
  lines.push('')
  lines.push('时间：' + ts())

  const subject = o.test
    ? '【自测】二维码邮件通道测试（双号版）'
    : '【告警】两个 QQ 机器人都掉线了（附二维码 ' + (attachments.length || 0) + ' 张）'

  log('  发送二维码邮件  需要扫码=' + needScan.map((p) => p.key).join(',') +
    '  不可达=' + dead.map((p) => p.key).join(',') +
    '  附件=' + attachments.length + ' 张')
  return notifyBoth(subject, lines.join(NL), attachments.length ? attachments : undefined)
}

;(async () => {
  const testQr = process.argv.includes('--test-qr')
  const probeOnly = process.argv.includes('--probe')

  const s = Object.assign({
    napcatRestarts: 0, memAlerts: 0, memMinMB: 99999, lastMemMB: null,
    lastOnline: null, lastRestartAt: 0, lastCheckAt: '', lastNote: '',
    offlineSince: 0, lastOfflineMinutes: 0, offlineReason: '',
    lastAlertKey: '', lastAlertAt: 0, lastQrAlertAt: 0,
    fail: {}, accSince: {}, accStatus: {}
  }, loadState())

  s.lastCheckAt = ts()

  if (testQr) {
    log('== 二维码邮件自测（双号版）==')
    const fake = ACCOUNTS.map((a) => ({ key: a.key, name: a.name, uin: a.uin, reachable: true, loggedIn: false, who: '', online: false, failStreak: 0 }))
    const r = await sendQrAlert(fake, { test: true })
    log('  结果:', JSON.stringify(r))
    return
  }

  // ── 1. 探测两个号 ────────────────────────────────────
  const probes = []
  for (const acc of ACCOUNTS) probes.push(await probe(acc, s))

  const up = probes.filter((p) => p.online)
  const down = probes.filter((p) => !p.online)
  const anyOnline = up.length > 0
  const allDown = !anyOnline

  log('探测结果：' + probes.map(accLine).join('  |  ') + '  =>  ' + (anyOnline ? '至少一个在线（静默）' : '两个都掉线'));

  s.accStatus = {}
  for (const p of probes) {
    s.accStatus[p.key] = { name: p.name, uin: p.uin, online: p.online, reachable: p.reachable, who: p.who, failStreak: p.failStreak, at: ts() }
  }
  s.onlineCount = up.length
  s.lastOnline = anyOnline

  if (probeOnly) {
    console.log(JSON.stringify({ probes: s.accStatus, anyOnline: anyOnline, availMB: (() => { try { return memAvailableMB() } catch (e) { return null } })() }, null, 2))
    return
  }

  // ── 2. 掉线计时（按号记录；两号全挂才记总起点）────────
  for (const p of down) {
    if (!s.accSince[p.key]) {
      s.accSince[p.key] = Date.now()
      log('  ' + p.key + ' 掉线（' + (p.reachable ? '未登录' : '不可达') + '），开始计时')
    }
  }
  for (const p of up) {
    if (s.accSince[p.key]) {
      const mins = ((Date.now() - s.accSince[p.key]) / 60000).toFixed(1)
      log('  ' + p.key + ' 已恢复（掉线 ' + mins + ' 分钟）')
      delete s.accSince[p.key]
    }
  }
  if (allDown) {
    if (!s.offlineSince) {
      s.offlineSince = Date.now()
      s.offlineReason = down.map((p) => p.key + ':' + (p.reachable ? 'not_logged_in' : 'unreachable')).join(',')
      log('!! 两个号都掉线，总掉线计时开始')
    }
  }

  async function alertOnce (key, subject, text, attachments) {
    const now = Date.now()
    if (s.lastAlertKey === key && now - s.lastAlertAt < ALERT_COOLDOWN_MS) {
      log('  （同类告警 ' + key + ' 冷却期内，跳过）')
      return
    }
    s.lastAlertKey = key
    s.lastAlertAt = now
    await notifyBoth(subject, text, attachments)
  }

  // ── 3. 只在“两个号都掉线”时才动手 ─────────────────────
  if (allDown) {
    const dead = probes.filter((p) => !p.reachable && p.failStreak >= FAIL_THRESHOLD)
    const deadKeys = dead.map((p) => p.key)
    const scanProbes = probes.filter((p) => p.reachable && !p.loggedIn)

    // 3a. 有号不可达（进程卡死/端口不通）-> 告警 + 自动重启这些容器
    if (deadKeys.length) {
      await alertOnce('unreachable_' + deadKeys.join(''),
        '【告警】QQ机器人 NapCat 无响应：' + deadKeys.join('/'),
        deadKeys.map((k) => {
          const p = probes.filter((x) => x.key === k)[0]
          const acc = ACCOUNTS.filter((a) => a.key === k)[0]
          return k + '（' + p.name + ' ' + p.uin + '，容器 ' + acc.container + '）本地 API 连续 ' + p.failStreak + ' 次（约 ' + (p.failStreak * 5) + ' 分钟）无响应。'
        }).join(NL) + NL +
        '时间：' + ts() + NL +
        '因为在线的号已经没有了，系统会尝试重启上述容器。' + NL +
        '注意：重启会导致 QQ 掉登录、需要重新扫码，二维码会在下一封邮件里发给你。' + NL +
        '若长时间不恢复，请在服务器上执行：' + NL +
        '  bash ' + repoPath('monitor', 'status.sh') + NL +
        ACCOUNTS.map((a) => '  docker logs --tail 60 ' + a.container).join(NL))

      const now = Date.now()
      if (now - s.lastRestartAt > RESTART_COOLDOWN_MS) {
        const svc = deadKeys.map((k) => ACCOUNTS.filter((a) => a.key === k)[0].container).join(' ')
        log('!! 执行自动重启：' + svc)
        try {
          execSync('cd ' + envAbs('NAPCAT_COMPOSE_DIR', repoPath('deploy', 'compose')) + ' && docker compose restart ' + svc, { stdio: 'inherit' })
          s.napcatRestarts++
          s.lastRestartAt = now
          s.lastNote = '不可达自动重启(' + svc + ') @ ' + ts()
          for (const k of deadKeys) s.fail[k] = 0
        } catch (e) {
          log('重启失败:', e.message)
          s.lastNote = '重启失败 @ ' + ts() + ' : ' + e.message
        }
      } else {
        const left = Math.ceil((RESTART_COOLDOWN_MS - (now - s.lastRestartAt)) / 60000)
        log('  重启冷却还剩约 ' + left + ' 分钟')
      }
    }

    // 3b. 有号“进程活着但没登录” -> 发二维码邮件（立刻 + 每 10 分钟）
    if (scanProbes.length) {
      const now = Date.now()
      if (!s.lastQrAlertAt || now - s.lastQrAlertAt >= QR_ALERT_INTERVAL_MS) {
        s.lastQrAlertAt = now
        log('!! 两号都掉线，且 ' + scanProbes.map((p) => p.key).join('/') + ' 需要扫码 -> 发送二维码邮件')
        await sendQrAlert(probes, {})
      } else {
        const left = Math.ceil((QR_ALERT_INTERVAL_MS - (now - s.lastQrAlertAt)) / 60000)
        log('  二维码邮件冷却中，约 ' + left + ' 分钟后发下一封')
      }
    } else if (!deadKeys.length) {
      log('  （两号都没有明确的掉线原因，等下一轮再看）')
    }
  } else if (down.length > 0) {
    // 有一号在线 —— 按用户要求：不告警、不发邮件，只记日志
    const d = down[0]
    const mins = s.accSince[d.key] ? ((Date.now() - s.accSince[d.key]) / 60000).toFixed(1) : '0'
    log('  ' + d.key + ' 已掉线 ' + mins + ' 分钟，但 ' + up.map((p) => p.key).join('/') + ' 仍在线 —— 按策略静默（不告警）')
  }

  // ── 4. 恢复通知（从“两个都挂”变回“至少一个在线”）──────
  if (anyOnline && s.offlineSince) {
    const mins = ((Date.now() - s.offlineSince) / 60000).toFixed(1)
    s.lastOfflineMinutes = Number(mins)
    log('两个号都掉线的状态已结束（持续 ' + mins + ' 分钟），发送恢复通知')
    await notifyBoth('【恢复】QQ 机器人已重新上线',
      '双号全挂时长：' + mins + ' 分钟' + NL +
      '掉线原因：' + (s.offlineReason || '未知') + NL +
      '当前状态：' + probes.map(accLine).join('  |  ') + NL +
      '时间：' + ts())
    s.offlineSince = 0
    s.offlineReason = ''
    s.lastAlertKey = ''
    s.lastQrAlertAt = 0
  }

  // ── 4.5 备用号发送能力自检（每 7 天一次）──────────────
  // 背景：2026-09-26 出过「备用号 B 显示在线、WS 也连着，但发送被 QQ 拒（1006514）」，
  //       主号一挂才发现备用号是哑的。这里定期让 B 真发一条消息，验证发送通道。
  const SPARE_TEST_INTERVAL_MS = 7 * 24 * 60 * 60 * 1000
  const spareAcc = ACCOUNTS.filter((a) => a.key === 'B')[0]
  const spareProbe = probes.filter((p) => p.key === 'B')[0]
  if (up.length >= 2 && spareAcc && spareProbe && spareProbe.online) {
    const nowS = Date.now()
    if (!s.lastSpareTestAt || nowS - s.lastSpareTestAt >= SPARE_TEST_INTERVAL_MS) {
      s.lastSpareTestAt = nowS
      try {
        const r = await apiCall(spareAcc.port, 'send_private_msg', {
          user_id: NOTIFY_USER,
          message: '【自检】备用号 ' + spareAcc.name + ' 发送通道自检，收到即代表备用线路可用。'
        }, 12000)
        s.spareSendOk = !!(r && (r.status === 'ok' || r.retcode === 0))
        s.spareSendAt = ts()
        s.spareSendNote = s.spareSendOk ? '正常' : String((r && (r.message || r.wording)) || '未知错误').slice(0, 200)
        log('  备用号发送自检：' + (s.spareSendOk ? 'OK' : 'NG -> ' + s.spareSendNote))
        if (!s.spareSendOk) {
          await notifyBoth('【告警】备用号无法发送消息（热备已失效）',
            '备用号 ' + spareAcc.name + '(' + spareAcc.uin + ') 显示在线，但实际发送被拒绝：' + NL +
            s.spareSendNote + NL + NL +
            '含义：主号一旦掉线，备用号接管后【同样发不出消息】，双号热备等于失效。' + NL +
            '处理办法（详见 docs/troubleshooting.md 第 3 节）：' + NL +
            '  1. 手机登录该号，手动发一条消息，确认账号本身没被风控' + NL +
            '  2. 让 NapCat 重新运行：POST http://127.0.0.1:' + spareAcc.port + '/set_restart' + NL +
            '  3. 如果出现二维码，扫码重登：bash ' + spareAcc.qrScript + NL +
            '  4. 重测发送，成功后再看下一封时报' + NL +
            '时间：' + ts())
        }
      } catch (e) {
        s.spareSendOk = false
        s.spareSendAt = ts()
        s.spareSendNote = '异常：' + e.message
        log('  备用号发送自检异常：' + e.message)
      }
    } else {
      const left = ((SPARE_TEST_INTERVAL_MS - (nowS - s.lastSpareTestAt)) / 86400000).toFixed(1)
      log('  备用号发送自检：' + (s.spareSendOk === undefined ? '未做过' : (s.spareSendOk ? '正常' : '异常')) +
        '（上次 ' + (s.spareSendAt || '从未') + '，' + left + ' 天后下次）')
    }
  }

  // ── 5. 内存 ──────────────────────────────────────────
  let avail = null
  try {
    avail = memAvailableMB()
    s.lastMemMB = avail
    if (avail < s.memMinMB) s.memMinMB = avail
    if (avail < MEM_ALERT_MB) {
      s.memAlerts++
      log('!! 内存告警：可用仅', avail, 'MB')
      await alertOnce('low_mem_' + Math.floor(avail / 50),
        '【告警】服务器可用内存偏低',
        '当前可用内存：' + avail + ' MB（阈值 ' + MEM_ALERT_MB + ' MB）' + NL +
        '时间：' + ts() + NL +
        '建议检查：docker stats --no-stream')
    }
  } catch (e) { log('内存读取失败:', e.message) }

  saveState(s)
  log('可用内存 ' + avail + ' MB | 在线号数 ' + up.length + '/2 | 重启 ' + s.napcatRestarts + ' 次 | 内存告警 ' + s.memAlerts + ' 次')
})().catch(e => { log('monitor 异常:', e.message); process.exit(1) })
