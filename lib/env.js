// 零依赖 .env 加载器 —— 从本文件所在目录向上定位仓库根目录（以 .env 为标志），
// 把其中的 KEY=VALUE 注入 process.env（已存在的环境变量优先，方便 systemd/容器覆盖）。
'use strict'
const fs = require('fs')
const path = require('path')

let _root = path.resolve(__dirname, '..')
for (let i = 0; i < 6; i++) {
  if (fs.existsSync(path.join(_root, '.env'))) break
  const parent = path.dirname(_root)
  if (parent === _root) break
  _root = parent
}
const ROOT_DIR = fs.existsSync(path.join(_root, '.env')) ? _root : path.resolve(__dirname, '..')
const ENV_FILE = path.join(ROOT_DIR, '.env')

if (fs.existsSync(ENV_FILE)) {
  for (const line of fs.readFileSync(ENV_FILE, 'utf8').split(/\r?\n/)) {
    const t = line.trim()
    if (!t || t.startsWith('#')) continue
    const m = t.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/)
    if (!m) continue
    if (m[1] in process.env) continue
    let v = m[2]
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1)
    process.env[m[1]] = v
  }
}

function env (key, fallback) {
  const v = process.env[key]
  return v === undefined || v === '' ? (fallback === undefined ? '' : fallback) : v
}
env.int = function (key, fallback) {
  const v = parseInt(process.env[key] || '', 10)
  return Number.isFinite(v) ? v : fallback
}
env.require = function (key) {
  const v = process.env[key]
  if (!v) {
    console.error('!! 缺少必填配置 ' + key + ' —— 请复制 .env.example 为 .env 并填写后再启动')
    process.exit(1)
  }
  return v
}
// 读取路径型配置：相对路径一律按仓库根目录解析，绝对路径原样使用
function envAbs (key, fallback) {
  const v = process.env[key]
  const p = v === undefined || v === '' ? fallback : v
  return path.isAbsolute(p) ? p : path.join(ROOT_DIR, p)
}
function repoPath (...p) { return path.join(ROOT_DIR, ...p) }

module.exports = { ROOT_DIR, ENV_FILE, env, envAbs, repoPath }
