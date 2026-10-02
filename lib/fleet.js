// 舰队配置（config/fleet.json）加载器
// QQ 号 / MC 账号名 / bot 显示名 / NapCat 实例 —— 所有"特有的名字"都只存在这份 gitignore 的 JSON 里。
'use strict'
const fs = require('fs')
const path = require('path')
const { env, envAbs, ROOT_DIR } = require('./env')

const FLEET_FILE = envAbs('FLEET_FILE', path.join(ROOT_DIR, 'config', 'fleet.json'))

let fleet
try {
  fleet = JSON.parse(fs.readFileSync(FLEET_FILE, 'utf8'))
} catch (e) {
  console.error('!! 无法读取舰队配置 ' + FLEET_FILE + '：' + e.message)
  console.error('   请复制 config/fleet.example.json 为 config/fleet.json 并按需修改')
  process.exit(1)
}

const ACCOUNTS = (fleet.accounts || []).map((a) => String(a.name))
const PORTS = {}
const ACC_NUM = {}
for (const a of fleet.accounts || []) {
  PORTS[a.name] = a.port
  if (a.num !== undefined && a.num !== null && a.num !== '') ACC_NUM[String(a.num)] = String(a.name)
}

const NAPCAT = (fleet.napcat || []).map((n) => ({
  key: n.key,
  name: n.name || n.key,
  uin: n.uin || '',
  container: n.container,
  port: n.port,
  // qr_script 支持 "monitor/qr.sh <容器名>" 形式：首个 token 按仓库根目录解析成绝对路径
  qrScript: (function (cmd) {
    if (!cmd) return ''
    const parts = String(cmd).split(' ')
    const file = path.isAbsolute(parts[0]) ? parts[0] : path.join(ROOT_DIR, parts[0])
    return [file].concat(parts.slice(1)).join(' ')
  })(n.qr_script || ('monitor/qr.sh ' + (n.container || '')))
}))

const ADMINS = (fleet.admins || []).map(String)
const OWNERS = (fleet.owners || []).map(String)
const SPARE_SELF_IDS = (fleet.spare_self_ids || []).map(String)
const TRUSTED_GROUPS = (fleet.trusted_groups || []).map(String)
const NOTIFY_USER = fleet.notify_user ? String(fleet.notify_user) : ''
const NOTIFY_GROUP = fleet.notify_group ? String(fleet.notify_group) : ''

module.exports = {
  fleet,
  FLEET_FILE,
  ACCOUNTS,
  PORTS,
  ACC_NUM,
  NAPCAT,
  ADMINS,
  OWNERS,
  SPARE_SELF_IDS,
  TRUSTED_GROUPS,
  NOTIFY_USER,
  NOTIFY_GROUP
}
