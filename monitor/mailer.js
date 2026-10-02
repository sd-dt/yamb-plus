#!/usr/bin/env node
// mailer.js —— 带外告警邮件通道（不依赖 QQ，NapCat 挂了也能报警）
//
// 作为模块用： const { sendMail } = require('./mailer')
//             const r = await sendMail('主题', '正文')
// 命令行用：   node monitor/mailer.js "主题" "正文"
//
// 配置在 monitor/mail.conf.json（权限 600，由 mail-setup.sh 生成；.env 的 MAIL_CONF 可覆盖路径）
const fs = require('fs')
const path = require('path')

const CONF = process.env.MAIL_CONF || path.join(__dirname, 'mail.conf.json')

function loadConf () {
  try {
    const c = JSON.parse(fs.readFileSync(CONF, 'utf8'))
    if (!c.enabled) return null
    if (!c.host || !c.user || !c.pass || !c.to) return null
    return c
  } catch (e) { return null }
}

async function sendMail (subject, text, attachments) {
  const c = loadConf()
  if (!c) return { ok: false, reason: '邮件未配置或已禁用（检查 monitor/mail.conf.json）' }

  let nodemailer
  try {
    nodemailer = require('nodemailer')
  } catch (e) {
    return { ok: false, reason: '未安装 nodemailer。请执行：cd <仓库>/monitor && npm install nodemailer' }
  }

  try {
    const t = nodemailer.createTransport({
      host: c.host,
      port: c.port || 465,
      secure: c.secure !== false,
      auth: { user: c.user, pass: c.pass },
      connectionTimeout: 15000,
      greetingTimeout: 15000,
      socketTimeout: 25000
    })
    const info = await t.sendMail({
      from: c.from || c.user,
      to: c.to,
      subject: subject,
      text: text,
      attachments: attachments || undefined
    })
    return { ok: true, messageId: info.messageId }
  } catch (e) {
    return { ok: false, reason: e.message }
  }
}

module.exports = { sendMail, loadConf }

if (require.main === module) {
  const subject = process.argv[2] || '【QQ机器人服务器】邮件告警测试'
  const text = process.argv[3] ||
    ('这是一封测试邮件。\n时间：' + new Date().toLocaleString('zh-CN', { hour12: false }))
  sendMail(subject, text).then(r => {
    if (r.ok) { console.log('✅ 邮件发送成功:', r.messageId); process.exit(0) }
    console.log('❌ 邮件发送失败:', r.reason)
    process.exit(1)
  })
}
