#!/usr/bin/env node
/* _p70: Microsoft 设备码授权（转钱小号免密码预登录）
 * 用法: node ms-device-auth.js <账号名>
 * 流程: 向微软申请设备码 -> 写 <账号>.code.json（官方网址 + 一次性授权码）
 *       -> 等用户在浏览器完成登录 -> getMinecraftJavaToken 把 token 写进本地缓存
 *       -> 写 <账号>.done.json + NapCat 私聊通知
 * 安全要点:
 *   - 与 yamb 登录完全同构：同缓存目录(MC_PROFILES_FOLDER)、同 flow('live')、同 authTitle，
 *     token 只落在服务器本地缓存文件，不进 QQ 聊天、不进配置文件、不进日志
 *   - code/done 状态文件只含网址和一次性授权码（15 分钟过期），不含 token/device_code
 * 环境变量: MC_PROFILES_FOLDER / PAY_AUTH_DIR / PAY_NAPCAT_PORT / PAY_NAPCAT_TOKEN / PAY_DIR
 */
'use strict'
const fs = require('fs')
const path = require('path')
const http = require('http')
const { Authflow, Titles } = require('/opt/qqbot/yamb/node_modules/prismarine-auth')

const username = String(process.argv[2] || '').trim()
if (!username || /[\/\\]|\s/.test(username)) {
  console.error('用法: node ms-device-auth.js <账号名>')
  process.exit(2)
}

const CACHE_DIR = process.env.MC_PROFILES_FOLDER || '/opt/qqbot/mc/auth-cache'
const AUTH_DIR = process.env.PAY_AUTH_DIR || '/opt/qqbot/mcbot/pay-auth'
const NAPCAT_PORT = parseInt(process.env.PAY_NAPCAT_PORT || '3000', 10)
const NAPCAT_TOKEN = process.env.PAY_NAPCAT_TOKEN || ''
const PAY_DIR = process.env.PAY_DIR || '/opt/qqbot/astrbot-data/plugins/astrbot_plugin_mcbot'
const CODE_FILE = path.join(AUTH_DIR, username + '.code.json')
const DONE_FILE = path.join(AUTH_DIR, username + '.done.json')

const ts = () => new Date().toLocaleString('zh-CN', { hour12: false })
const log = (...a) => console.log('[' + ts() + '][auth:' + username + ']', ...a)

function writeJson (f, obj) {
  fs.writeFileSync(f, JSON.stringify(obj, null, 2) + '\n', { mode: 0o600 })
}

function notify (text) {
  let user = 0
  try { user = parseInt(String(JSON.parse(fs.readFileSync(path.join(PAY_DIR, 'pay.json'), 'utf8')).notify_user || ''), 10) || 0 } catch (e) {}
  if (!user || !NAPCAT_TOKEN) { log('未配置通知目标/token，跳过私聊通知'); return Promise.resolve() }
  return new Promise((resolve) => {
    const payload = JSON.stringify({ user_id: user, message: text })
    const req = http.request({
      host: '127.0.0.1', port: NAPCAT_PORT, path: '/send_private_msg', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload), Authorization: 'Bearer ' + NAPCAT_TOKEN },
      timeout: 20000
    }, (res) => {
      let d = ''
      res.on('data', (c) => { d += c })
      res.on('end', () => { log('通知结果: ' + d.slice(0, 120)); resolve() })
    })
    req.on('error', (e) => { log('通知失败: ' + e.message); resolve() })
    req.on('timeout', () => req.destroy())
    req.write(payload)
    req.end()
  })
}

;(async () => {
  fs.mkdirSync(AUTH_DIR, { recursive: true, mode: 0o700 })
  try { fs.unlinkSync(CODE_FILE) } catch (e) {}
  try { fs.unlinkSync(DONE_FILE) } catch (e) {}

  // 与 minecraft-protocol 的登录参数完全同构（同 username/同缓存目录/同 flow），
  // 这里产出的缓存 yamb 登录时直接复用，不会再要密码
  const flow = new Authflow(username, CACHE_DIR,
    { flow: 'live', authTitle: Titles.MinecraftNintendoSwitch },
    (resp) => {
      writeJson(CODE_FILE, {
        verification_uri: resp.verification_uri || 'https://www.microsoft.com/link',
        user_code: resp.user_code || '',
        message: resp.message || '',
        at: Date.now()
      })
      log('设备码已生成: ' + (resp.verification_uri || '') + ' code=' + (resp.user_code || ''))
    })

  try {
    const r = await flow.getMinecraftJavaToken({ fetchProfile: true })
    const name = (r && r.profile && r.profile.name) || ''
    writeJson(DONE_FILE, { ok: true, profile: name, at: Date.now() })
    log('授权成功' + (name ? ': ' + name : ''))
    await notify('✅ 转钱小号「' + username + '」微软授权成功' + (name ? '（游戏 ID: ' + name + '）' : '') +
      '，token 已保存到服务器本地缓存。\n以后每天 04:00 自动登录转钱；可随时发 !mcbot paylist 查看状态。')
  } catch (e) {
    writeJson(DONE_FILE, { ok: false, message: String(e.message || e).slice(0, 300), at: Date.now() })
    log('授权失败: ' + e.message)
    await notify('❌ 转钱小号「' + username + '」微软授权失败：' + String(e.message || e).slice(0, 120) +
      '\n可稍后发 !mcbot payauth ' + username + ' 重新获取授权码。')
    process.exit(1)
  }
})().catch((e) => { log('异常退出: ' + (e.stack || e.message)); process.exit(1) })
