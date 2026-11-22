"""helpcatalog.py —— 全插件指令总目录

说明来源按优先级：
  1. handler 的 docstring（`@filter.command("x")` 下面那个函数的文档字符串）—— 最准
  2. 插件 README（表格 / 列表 / 路径三种写法都吃）—— 补 docstring 没写的
  3. CURATED 人工校对 —— 兜底没有 README、README 是英文的

指令是否"存在"以代码里真实注册的为准（@filter.command / @filter.command_group + alias），
保证帮助里写出来的指令都能用。
"""

import ast
import io
import json
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS_DIR = os.path.dirname(HERE)
CACHE_PATH = os.path.join(HERE, 'help_catalog.json')

# AstrBot 主配置目录（插件配置在 <data>/config/<plugin>_config.json）
CONFIG_DIR = os.path.join(os.path.dirname(PLUGINS_DIR), 'config')

PER_PAGE = 16

CMD_RE = re.compile(r'(?<![\w/!<])([/!][A-Za-z\u4e00-\u9fa5][\w\-\u4e00-\u9fa5]{0,24})')
CODE_CMD_RE = re.compile(r'@(?:filter\.)?(?:command|command_group)\s*\(\s*["\']([^"\']{1,24})["\']')
# pkg162: 子指令装饰器 @mcomp.command("status")、@self.x.command(...) 也要扫到
ANY_CMD_RE = re.compile(r'@\s*([\w\.\-]+?)\s*\.\s*(command|command_group)\s*\(\s*["\']([^"\']{1,24})["\']')
# command_group 的定义：mcomp = filter.command_group("mcomp")
GROUP_DEF_RE = re.compile(r'([\w\.]+)\s*=\s*(?:filter\.)?command_group\s*\(\s*["\']([^"\']{1,24})["\']')
# 装饰器写法：@filter.command_group("mcomp") 下一行 def mcomp(...)
GROUP_DEC_RE = re.compile(r'@(?:filter\.)?command_group\s*\(\s*["\']([^"\']{1,24})["\']\s*\)\s*(?:\r?\n\s*)?(?:async\s+)?def\s+(\w+)')
CODE_ALIAS_RE = re.compile(r'alias\s*=\s*[\[\{\(]([^\]\}\)]*)[\]\}\)]')
ALIAS_ITEM_RE = re.compile(r'["\']([^"\']{1,18})["\']')
DOC_RE = re.compile(r'def\s+\w+\s*\([^)]*\)[^:]{0,80}:\s*(?:\r?\n\s*)?(?:"""|\'\'\')(.{0,300}?)(?:"""|\'\'\')', re.S)

STOP = {
    'div', 'span', 'br', 'p', 'a', 'img', 'li', 'ul', 'ol', 'table', 'tr', 'td',
    'code', 'pre', 'details', 'summary', 'strong', 'em', 'b', 'i', 'h1', 'h2',
    'h3', 'h4', 'h5', 'h6', 'tip', 'note', 'warning', 'important', 'caution',
    'info', 'api', 'path', 'assets', 'config', 'data', 'plugins', 'v1', 'v2',
    'search', 'usr', 'home', 'tmp', 'etc', 'var', 'root', 'opt', 'app', 'sid',
    'astrbot', 'cmd_config', 'astrbot_plugin',
}

# AstrBot 框架内置指令：不属于任何插件，单独归到「内置」一节
BUILTIN = {
    '/help': '显示框架帮助 / 指令列表',
    '/new': '新建并切换会话',
    '/reset': '重置当前会话上下文',
    '/set': '设置当前会话变量',
    '/unset': '删除当前会话变量',
    '/stats': '查看当前会话 token 统计',
    '/name': '设置当前会话的显示别名（管理员）',
    '/provider': '查看或切换模型供应商（管理员）',
    '/sid': '获取当前会话的完整 UMO（配置主动消息/官机时用）',
    '/persona': '切换人格',
    '/dashboard_update': '更新 AstrBot WebUI（管理员）',
    '/websearch': '开关联网搜索',
    '/stop': '停止当前正在进行的生成 / 任务',
    '/history': '查看当前会话的历史消息',
}

PASSIVE_NOTE = '（无指令：装上就自动生效，行为全在插件配置里调）'

CURATED = {
    '/litematic': '把 .litematic 投影当聊天附件发，自动出预览图（俯视 1 张 / 立体 2 张）',
    '/affection_status': '查看当前好感度状态',
    '/learning_status': '查看学习状态与统计',
    '/start_learning': '手动启动一次学习',
    '/force_learning': '强制执行一次学习',
    '/remember': '手动记住一段对话上下文',
    '/set_mood': '设置 bot 当前情绪',
    '/stop_learning': '停止正在进行的自动学习',
    '/调试网格': '生成一张网格调试图，用来校准表情包格子尺寸',
    '/查看api': '查看已配置的 API 聚合信息',
    '/点歌': '点歌 <歌名>（网易/QQ/酷狗/酷我/咪咕等音源）',
    '/debug_graph': '生成关系图调试数据',
    '/总结群聊': '总结本群最近一段时间的聊天（也支持自然语言触发）',
    '/戳': '戳 @某人 / 我 / 全体成员',
    '/zl': '群友语料蒸馏：/zl add|set|del|list|distill|export|on|off（别名 /蒸馏）',
    '/onelast': '把图片转成 One Last Kiss 卢浮宫风格（可带参数 JSON）',
    '/复读机': '群复读机：开 / 关 / 阈值 / 冷却 / 状态',
    '/setu': '随机涩图（支持标签、数量、分级）',
    '/随机本子': '随机本子（别名 /本子、/doujinshi）',
    '/今日运势': '查看今日运势（每天一次）',
    '/肥鱼说': '让肥鱼举牌说一句话（别名：肥鱼举牌）',
    '/肥鱼帮助': '查看举牌插件用法（别名：举牌帮助）',
    '/今日小猪': '抽取或查看今天的小猪（每人每天一只，重复查询不换猪）',
    '/昨日小猪': '查看昨天的抽猪结果',
    '/明日小猪': '明天运势预测，不会提前解锁',
    '/本周小猪': '生成本周七日小猪周报',
    '/随机小猪': '随机展示小猪，不影响今日结果',
    '/今日烤猪': '把今天的小猪做成趣味料理卡',
    '/随机烤群友': '从今天在本群抽过猪的群友中随机挑一位',
    '/吃群友': '吃掉被烤熟的群友，失败会把自己也吃掉',
    '/随机吃群友': '随机点名可吃群友，成败都会出现吃掉状态',
    '/打点后厨': '对 @某人 使用后门口令（每日一次，超管可强行点火）',
    '/猪圈日报开启': '管理员开启本群猪圈日报自动推送',
    '/猪圈日报关闭': '管理员关闭本群猪圈日报自动推送',
    '/猪圈日报状态': '查看本群日报开关、推送时间与状态',
}

MANUAL_PLUGINS = {
    'astrbot_plugin_mcbot': {
        'name': 'MC 挂机控制（本插件）',
        'desc': '私聊管理员控制 MC 挂机号、转钱、群白名单',
        'cmds': [
            ('!mcbot list', '看 4 个挂机号的启用与在线状态'),
            ('!mcbot <账号…>', '翻转这些号：在线→下线，离线→上线（最多同时挂 3 个，例：!mcbot 1 2 3）'),
            ('!mcbot stop / start', '全部下线 / 按上次的账号集合启动'),
            ('!mcbot set <账号…>', '只挂这些号，其余全部下线'),
            ('!mcbot enable|disable <账号…>', '追加启用 / 单独关闭某个号'),
            ('!mcbot <账号> command <指令>', '以该号身份往游戏里发一条指令'),
            ('!mcbot yamb help', '列出全部游戏动作与用法'),
            ('!mcbot addpay <账号>', '登记转钱小号（发微软网址 + 授权码完成登录）'),
            ('!mcbot payauth <账号>', '重发授权码（登录失效时用）'),
            ('!mcbot paylist', '看转钱配置与今日尝试情况'),
            ('!mcbot paytest [账号]', '立刻跑一次转钱流程'),
            ('!mcbot payset <收款人> <金额>', '设置收款人与金额（全号统一，每天一次）'),
            ('!mcbot payon|payoff <账号>', '开关某个号的自动转钱'),
            ('!qqbot start / stop', '开启 / 停用 AI 聊天（停用后只响应指令）'),
            ('!qqbot add <群号>', '把群加入白名单（会话白名单 + 群分析名单）'),
            ('!qqbot groups', '查看已启用的群'),
            ('!qqbot help', '全插件指令总目录'),
        ],
    },
    'astrbot_plugin_litematic': {
        'name': '投影渲染',
        'desc': '群里发 .litematic 附件自动出预览图',
    },
    'astrbot_plugin_group_distiller': {
        'name': '我要蒸馏群友',
        'desc': '收集群友语料并蒸馏成可导出的人设 / 风格',
        'cmds': [
            ('/zl', '查看面板与进度（别名 /蒸馏）'),
            ('/zl add <群号> <QQ号> [昵称]', '添加一个蒸馏目标'),
            ('/zl set <群号> <QQ号> [昵称]', '替换目标列表（保留已收集的语料）'),
            ('/zl del <QQ号> [purge]', '删除目标；加 purge 连语料档案一起删'),
            ('/zl list', '列出当前所有蒸馏目标'),
            ('/zl distill [目标]', '立刻蒸馏（不填则蒸馏全部）'),
            ('/zl export', '导出蒸馏结果'),
            ('/zl on / off', '开启 / 关闭自动采集'),
        ],
    },
}


# ── 基础 ────────────────────────────────────────────────────
def _read(p):
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            return io.open(p, encoding=enc).read()
        except Exception:
            continue
    return ''


def _meta(d):
    m = {}
    p = os.path.join(d, 'metadata.yaml')
    if os.path.exists(p):
        for ln in _read(p).split('\n'):
            mo = re.match(r'^\s*(name|display_name|desc|version|author)\s*:\s*(.+)$', ln)
            if mo:
                m[mo.group(1)] = mo.group(2).strip().strip('"\'')
    return m


def _pick_readme(d):
    try:
        cands = [f for f in os.listdir(d) if f.lower().startswith('readme')]
    except Exception:
        return None
    if not cands:
        return None
    cands.sort(key=lambda f: (0 if f.lower() == 'readme.md' else
                              1 if 'en' not in f.lower() else 2, len(f)))
    return os.path.join(d, cands[0])


def _doc_lines(d, base_lines):
    """说明检索语料：README + docs/**/*.md（有些插件把完整指令表放 docs 里）
    注意：docs 只用来查说明，不用来发现指令，避免收录文档里已废弃的命令。"""
    out = list(base_lines)
    docs = os.path.join(d, 'docs')
    if not os.path.isdir(docs):
        return out
    for root, _dirs, files in os.walk(docs):
        for fn in sorted(files):
            if not fn.lower().endswith('.md'):
                continue
            p = os.path.join(root, fn)
            try:
                if os.path.getsize(p) > 300 * 1024:
                    continue
            except Exception:
                continue
            out.extend(_read(p).split('\n'))
    return out


def _clean(s):
    s = s or ''
    s = s.replace('`', '').replace('**', '')
    s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)
    s = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', s)
    s = re.sub(r'<[^>]+>', '', s)
    s = re.sub(r'^\s*[-*+>|#]+\s*', '', s)
    s = re.sub(r'\s*\|\s*$', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _junk_desc(d):
    """明显不是说明的：配置键名、纯路径、纯符号、别名罗列"""
    if not d or len(d) < 2:
        return True
    if re.fullmatch(r'[a-z0-9_\.\-]+', d):          # enable / enable_daily_comic
        return True
    # 纯路径（不含空格/@等），如 data/plugins/group_summary/
    if re.fullmatch(r'[\w\.\-\u4e00-\u9fa5]*(/[\w\.\-\u4e00-\u9fa5]+)+/?', d or ''):
        return True
    if d.count('、') >= 3:                          # 别名列被当成了说明
        return True
    if d in ('用户', '管理员', '所有人', '主人', '-', '无', '是', '否'):
        return True
    # 纯英文长句多半是 docs/CHANGELOG 里的技术说明，不是给用户看的用法
    if len(d) > 26 and not re.search(r'[\u4e00-\u9fa5]', d):
        return True
    return False


# ────────────────────────────────────────────── 说明清洗 ──────────────────────────────────────────────
PARAM_HEAD_RE = re.compile(r'^\[\s*[^\]]{1,20}\]\s*|^<\s*[^>]{1,20}>\s*')
SELF_SEP_RE = re.compile(r'^[\s:：\-—–|,，、]+')


def _strip_self_prefix(cmd, desc):
    """去掉说明里自带的指令名 / 参数占位前缀。

    docstring 常写成 "^群杯子 — 每天随机抽取…" 或 "[快捷名称] — 快捷查询…"，
    直接抓首行会把 "^群杯子 — " 一起带进帮助列表。
    """
    if not desc:
        return desc
    d = (desc or '').strip()
    base = re.sub(r'^[/!^.]+', '', (cmd or '').strip())
    if base:
        for pre in ('/', '!', '^', '.', ''):
            m = re.match(re.escape(pre + base) + r'(?![\w\u4e00-\u9fa5])', d)
            if m:
                rest = SELF_SEP_RE.sub('', d[m.end():].lstrip()).strip()
                if len(rest) >= 4:
                    d = rest
                    break
    m = PARAM_HEAD_RE.match(d)
    if m:
        rest = SELF_SEP_RE.sub('', d[m.end():]).strip()
        if len(rest) >= 4:
            d = rest
        elif not rest:
            return ''          # 只剩一个占位符（如 "[操作]"）等于没说明
    return d


# ── README 抽说明 ───────────────────────────────────────────
def _desc_from_readme(lines, tok):
    pat = re.compile(re.escape(tok) + r'(?![\w\-])')
    table_desc, table_alias, plain = '', [], ''

    for ln in lines:
        if tok not in ln:
            continue
        if ln.count('|') >= 2:
            cells = [_clean(c) for c in ln.strip().strip('|').split('|')]
            hit = next((i for i, c in enumerate(cells) if pat.search(c)), None)
            if hit is not None and len(cells) >= 2:
                others = [c for c in cells[hit + 1:]
                          if c and not pat.fullmatch(c) and not _junk_desc(c)
                          and not c.startswith(('/', '!'))]      # 示例列常是 /cmd 参数
                if others:
                    # 说明一般在最后一列，但末列可能是"示例"，取最长的更像说明
                    best = max(others, key=len)
                    if len(best) > len(table_desc):
                        table_desc = best
                if not table_alias:
                    table_alias = [c for c in cells[hit + 1:-1]
                                   if re.fullmatch(r'[\w\-\u4e00-\u9fa5|、,，]{1,18}', c or '')]
                continue
        m = pat.search(ln)
        if not m:
            continue
        # 只认"行首就是这条指令"或"指令被反引号包着"的行，
        # 否则容易抓到顺手提到命令名的普通句子（例如插件列表介绍）
        if not (re.match(r'^\s*[-*+>|]?\s*`?' + re.escape(tok) + r'(?![\w\-])', ln)
                or re.search(r'`' + re.escape(tok) + r'[^`]*`', ln)):
            continue
        tail = _clean(ln[m.end():])
        tail = re.sub(r'^[\s:：\-—–|/、]+', '', tail)
        tail = re.sub(r'^(用法|示例|例如|说明)[:：]\s*', '', tail)
        if not _junk_desc(tail) and len(tail) > len(plain):
            plain = tail
    return (table_desc or plain)[:46], table_alias


# ── 代码扫描 ────────────────────────────────────────────────
def _docstring_after(src, pos):
    m = DOC_RE.search(src[pos:pos + 700])
    if not m:
        return ''
    txt = m.group(1).strip()
    first = txt.split('\n')[0].strip()
    first = re.sub(r'^[\s:：\-—–]+', '', first)
    return '' if _junk_desc(first) else first[:46]


def _eval_str(node, consts):
    """把 AST 表达式还原成字符串：常量 / 变量 / f-string / 字符串相加"""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                s = _eval_str(v.value, consts)
                if s is None:
                    return None
                out.append(s)
            else:
                return None
        return ''.join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        l = _eval_str(node.left, consts)
        r = _eval_str(node.right, consts)
        if l is None or r is None:
            return None
        return l + r
    return None


def _eval_list(node, consts):
    if node is None:
        return []
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out = []
        for e in node.elts:
            s = _eval_str(e, consts)
            if s:
                out.append(s)
        return out
    s = _eval_str(node, consts)
    return [s] if s else []


def _py_files(d):
    files = []
    for root, _dirs, fs in os.walk(d):
        if '__pycache__' in root or (os.sep + '.git') in root:
            continue
        for fn in sorted(fs):
            if fn.endswith('.py'):
                text = _read(os.path.join(root, fn))
                if text:
                    files.append(text)
    return files


def _collect_consts(files, consts=None):
    """pkg172: 收集模块级字符串常量（含 f-string 常量），
    这样 @filter.command(f"{CHARACTER_NAME}说") 才能还原成「肥鱼说」。
    跨文件同名常量一起收，够用了。"""
    consts = consts if consts is not None else {}
    for text in files:
        try:
            tree = ast.parse(text)
        except Exception:
            continue
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                continue
            s = _eval_str(node.value, consts)
            if s:
                consts[node.targets[0].id] = s
    return consts


def _code_cmds(d):
    """pkg172: 用 AST 扫真实注册的指令。

    比正则版多认：
      · f-string 指令名（@filter.command(f"{CHARACTER_NAME}说")）
      · 常量拼接名（@filter.command(NAME + "说")）
      · 多行装饰器、alias= / aliases= 两种写法
    AST 解析不了的文件退回原来的正则扫描。
    """
    files = _py_files(d)
    if not files:
        return {}
    consts = _collect_consts(files)

    groups = {}
    trees = []
    for text in files:
        try:
            tree = ast.parse(text)
        except Exception:
            tree = None
        trees.append(tree)
        if tree is None:
            continue
        # 组可能定义在类里（@filter.command_group("meme") 下一行 def meme），
        # 所以用 ast.walk 而不是只看模块顶层
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                v = node.value
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                        and v.func.attr == 'command_group' and v.args):
                    g = _eval_str(v.args[0], consts)
                    if g:
                        groups[node.targets[0].id] = g
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                            and dec.func.attr == 'command_group' and dec.args):
                        g = _eval_str(dec.args[0], consts)
                        if g:
                            groups[node.name] = g

    found = {}
    parsed_any = False
    for tree in trees:
        if tree is None:
            continue
        parsed_any = True
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                if dec.func.attr not in ('command', 'command_group') or not dec.args:
                    continue
                name = _eval_str(dec.args[0], consts)
                if not name:
                    continue
                obj = dec.func.value
                grp = groups.get(obj.id) if isinstance(obj, ast.Name) else None
                tok = ('/%s %s' % (grp, name)) if (grp and grp != name) else ('/' + name)
                ent = found.setdefault(tok, {'alias': [], 'doc': ''})
                for kw in dec.keywords:
                    if kw.arg in ('alias', 'aliases'):
                        for a in _eval_list(kw.value, consts):
                            if a not in ent['alias']:
                                ent['alias'].append(a)
                if not ent['doc']:
                    try:
                        doc = ast.get_docstring(node) or ''
                    except Exception:
                        doc = ''
                    first = doc.strip().split('\n')[0].strip() if doc.strip() else ''
                    first = re.sub(r'^[\s:：\-—–]+', '', first)
                    if first and not _junk_desc(first):
                        ent['doc'] = first[:46]

    if not parsed_any or not found:
        return _code_cmds_regex(d)
    return found


def _code_cmds_regex(d):
    """正则版：AST 全部解析失败时兜底"""
    """返回 {cmd: {'alias': [...], 'doc': '...'}}"""
    found = {}
    # 先收集本插件所有 command_group 变量名 -> 组名，
    # 这样 @mcomp.command("status") 能还原成 /mcomp status
    groups = {}
    for root, _dirs, files in os.walk(d):
        if '__pycache__' in root or (os.sep + '.git') in root:
            continue
        for fn in files:
            if not fn.endswith('.py'):
                continue
            src = _read(os.path.join(root, fn))
            if not src:
                continue
            for m in GROUP_DEF_RE.finditer(src):
                groups[m.group(1).split('.')[-1]] = m.group(2).strip()
            for m in GROUP_DEC_RE.finditer(src):
                groups[m.group(2)] = m.group(1).strip()

    def tok_of(obj, name):
        grp = groups.get(obj.split('.')[-1])
        if grp and grp != name:
            return '/%s %s' % (grp, name.lstrip('/'))
        return '/' + name.lstrip('/')

    for root, _dirs, files in os.walk(d):
        if '__pycache__' in root or (os.sep + '.git') in root:
            continue
        for fn in files:
            if not fn.endswith('.py'):
                continue
            src = _read(os.path.join(root, fn))
            if not src:
                continue
            for m in ANY_CMD_RE.finditer(src):
                name = m.group(3).strip()
                if not name:
                    continue
                tok = tok_of(m.group(1), name)
                ent = found.setdefault(tok, {'alias': [], 'doc': ''})
                if not ent['doc']:
                    ent['doc'] = _docstring_after(src, m.end())
            for m in CODE_ALIAS_RE.finditer(src):
                items = [a.strip() for a in ALIAS_ITEM_RE.findall(m.group(1)) if a.strip()]
                if not items:
                    continue
                prev = None
                for pm in ANY_CMD_RE.finditer(src[:m.start()]):
                    prev = tok_of(pm.group(1), pm.group(3).strip())
                if not prev:
                    continue
                ent = found.setdefault(prev, {'alias': [], 'doc': ''})
                for a in items:
                    if a not in ent['alias']:
                        ent['alias'].append(a)
    return found


def _readme_cmds(lines):
    out = []
    for ln in lines:
        for m in CMD_RE.finditer(ln):
            tok = m.group(1)
            if tok.lstrip('/!').lower() in STOP:
                continue
            if not (re.match(r'^\s*[-*+>|]?\s*`?' + re.escape(tok) + r'(?![\w\-])', ln)
                    or re.match(r'^\s*\|\s*`?' + re.escape(tok), ln)):
                continue
            if tok not in out:
                out.append(tok)
    return out


# ── 提示词命令（裸词，不用 / 前缀）──────────────────────────
# 这类命令不走 @filter.command，而是通用监听器拿「消息第一个词」去查表
# （portrayal 的 get_portrayal：event.message_str.partition(" ")[0]）。
# 权威来源：插件目录下的 builtin_prompts.yaml + 插件配置里的 entry_storage。
PROMPT_FILES = ('builtin_prompts.yaml', 'builtin_prompts.yml', 'prompts.yaml')


def _prompt_entries(key, d):
    """返回 [(命令, prompt 正文)]"""
    out = []

    def _collect(data, depth=0, name=None):
        if depth > 4:
            return
        if isinstance(data, list):
            for it in data:
                _collect(it, depth + 1)
            return
        if not isinstance(data, dict):
            return
        c = data.get('command')
        body = data.get('content') or data.get('prompt')
        if isinstance(c, str) and c.strip():
            out.append((c.strip(), body if isinstance(body, str) else ''))
            return
        # 形如 {画像: {content: ...}} / {画像: "..."}
        if isinstance(name, str) and isinstance(body, str) and body.strip():
            out.append((name.strip(), body))
            return
        for k, v in data.items():
            if isinstance(v, (dict, list)) and k not in ('command', 'content', 'prompt'):
                _collect(v, depth + 1, k if isinstance(k, str) else None)

    for fn in PROMPT_FILES:
        p = os.path.join(d, fn)
        if not os.path.exists(p):
            continue
        txt = _read(p)
        if not txt:
            continue
        try:
            import yaml
            _collect(yaml.safe_load(txt))
        except Exception:
            # 没装 yaml 就退化成抓 command: 行
            for m in re.finditer(r'^\s*command\s*:\s*(.+)$', txt, re.M):
                out.append((m.group(1).strip().strip('"\''), ''))

    # 插件配置里用户自己加的（结构同 yaml：列表里的 dict 带 command）
    cfg = os.path.join(CONFIG_DIR, key + '_config.json')
    if os.path.exists(cfg):
        try:
            data = json.loads(_read(cfg))
            for v in (data or {}).values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and 'command' in v[0]:
                    _collect(v)
        except Exception:
            pass

    # 去重保序
    seen, uniq = set(), []
    for c, body in out:
        if c in seen:
            continue
        seen.add(c)
        uniq.append((c, body))
    return uniq


def _desc_for_bare(lines, cmd):
    """在 README 表格里找首格首词正好是 cmd 的那一行，取最后一列当说明"""
    for ln in lines:
        if ln.count('|') < 2 or cmd not in ln:
            continue
        cells = [_clean(c) for c in ln.strip().strip('|').split('|')]
        if not cells:
            continue
        first = cells[0].split()[0] if cells[0].split() else ''
        if first != cmd:
            continue
        others = [c for c in cells[1:] if c and not _junk_desc(c)]
        if others:
            return max(others, key=len)[:46]
    return ''


def scan_plugin(d):
    key = os.path.basename(d)
    meta = _meta(d)
    rp = _pick_readme(d)
    readme_lines = (_read(rp) if rp else '').split('\n')
    lines = _doc_lines(d, readme_lines)     # 查说明用的语料（含 docs/*.md）

    code = _code_cmds(d)
    doc = _readme_cmds(readme_lines)

    # 代码里注册过的别名：README 里出现的繁体/异体名其实只是别名，别再算一条指令
    alias_pool = set()
    for ent in code.values():
        for a in (ent.get('alias') or []):
            alias_pool.add(a)
            alias_pool.add('/' + a)
            alias_pool.add('!' + a)

    cmds, seen = [], set()
    # 只有"从 README 发现"的 token 才做别名过滤；代码里真实注册的命令一律保留
    extra_doc = [t for t in doc if t not in code
                 and t not in alias_pool and t.lstrip('/!') not in alias_pool]
    for tok in list(code.keys()) + extra_doc:
        if tok in seen or tok in BUILTIN:
            continue
        seen.add(tok)
        ent = code.get(tok) or {'alias': [], 'doc': ''}
        rdesc, ralias = _desc_from_readme(lines, tok)
        # README 可能只写裸名（^cmd / cmd），代码键却带 /，补一次裸名查询
        if not rdesc and tok[:1] in '/!':
            _bar_d, _bar_a = _desc_from_readme(lines, tok.lstrip('/!'))
            if _bar_d:
                rdesc = _bar_d
                if not ralias:
                    ralias = _bar_a
        # 主名在 README 里查不到说明时，用它的别名去查（常见：代码简体、文档繁体）
        if not rdesc:
            for a in (ent.get('alias') or []):
                for cand in (a, '/' + a):
                    rdesc, _x = _desc_from_readme(lines, cand)
                    if rdesc:
                        break
                if rdesc:
                    break
        desc = (CURATED.get(tok)
                or _strip_self_prefix(tok, rdesc)
                or _strip_self_prefix(tok, ent['doc']))
        aliases = list(ent['alias'])
        for a in ralias:
            if a and a not in aliases and a != tok.lstrip('/'):
                aliases.append(a)
        cmds.append({'cmd': tok, 'desc': desc, 'alias': aliases[:4]})

    # 提示词命令（裸词，不用 / ! 前缀）：builtin_prompts.yaml + 配置 entry_storage
    for c, body in _prompt_entries(key, d):
        if c in seen:
            continue
        seen.add(c)
        desc = CURATED.get(c) or _desc_for_bare(lines, c)
        if not desc:
            first = (body.strip().split('\n')[0] or '').strip()
            desc = ('自定义提示词：' + _clean(first))[:52]
        cmds.append({'cmd': c, 'desc': desc, 'alias': [], 'bare': True})

    manual = MANUAL_PLUGINS.get(key)
    if manual and manual.get('cmds'):
        cmds = [{'cmd': c, 'desc': t, 'alias': []} for c, t in manual['cmds']]

    name = (meta.get('display_name') or meta.get('name') or key).split('#')[0].strip()
    desc = (meta.get('desc') or '').strip().split('#')[0].strip()
    if manual:
        name = manual.get('name') or name
        desc = manual.get('desc') or desc
    cmds.sort(key=lambda x: x['cmd'])
    return {'key': key, 'name': name[:24], 'desc': desc[:60],
            'readme': os.path.basename(rp) if rp else '',
            'cmds': cmds, 'passive': not cmds}


def build():
    cat = []
    for name in sorted(os.listdir(PLUGINS_DIR)):
        d = os.path.join(PLUGINS_DIR, name)
        if not os.path.isdir(d) or name == '__pycache__':
            continue
        try:
            cat.append(scan_plugin(d))
        except Exception as e:
            # 解析 README 出错时，至少把代码里注册的指令列出来，别整段消失
            cmds = []
            try:
                meta = _meta(d)
                for tok in sorted(_code_cmds(d)):
                    cmds.append({'cmd': tok, 'desc': CURATED.get(tok, ''), 'alias': []})
            except Exception:
                meta = {}
            cat.append({'key': name,
                        'name': (meta.get('display_name') or name)[:24],
                        'desc': '解析异常：%s（指令按代码补全）' % str(e)[:60],
                        'cmds': cmds, 'passive': not cmds, 'error': True})
    cat.append({'key': '__builtin__', 'name': 'AstrBot 内置', 'desc': '框架自带，不依赖任何插件',
                'readme': '', 'passive': False,
                'cmds': [{'cmd': k, 'desc': v, 'alias': []} for k, v in BUILTIN.items()]})
    io.open(CACHE_PATH, 'w', encoding='utf-8').write(json.dumps(cat, ensure_ascii=False, indent=1))
    return cat


def get(force=False):
    if not force and os.path.exists(CACHE_PATH):
        try:
            return json.load(io.open(CACHE_PATH, encoding='utf-8'))
        except Exception:
            pass
    return build()


# ── 渲染 ────────────────────────────────────────────────────
def _chunks(text, size=1300):
    out, cur = [], ''
    for ln in text.split('\n'):
        if len(cur) + len(ln) + 1 > size and cur:
            out.append(cur.rstrip())
            cur = ''
        cur += ln + '\n'
    if cur.strip():
        out.append(cur.rstrip())
    return out


def stats(cat):
    withcmd = [p for p in cat if not p.get('passive')]
    return len(withcmd), sum(len(p['cmds']) for p in cat), len(cat)


def render_index(cat):
    n_pl, n_cmd, n_all = stats(cat)
    lines = ['📚 全插件指令总目录',
             '%d 个插件 ｜ %d 条指令（%d 个被动插件无指令）' % (n_all, n_cmd, n_all - n_pl),
             '━' * 12]
    for i, p in enumerate(cat, 1):
        if p.get('passive'):
            lines.append('%2d. %s · 被动' % (i, p['name']))
        else:
            # pkg162: 目录里也把裸词指令（画像/找对象 这类）带出来
            picks = [c['cmd'] for c in p['cmds'][:3]]
            for c in p['cmds']:
                if c.get('bare') and c['cmd'] not in picks:
                    picks.append(c['cmd'])
                if len(picks) >= 5:
                    break
            sample = '、'.join(picks)
            lines.append('%2d. %s（%d）' % (i, p['name'], len(p['cmds'])))
            lines.append('     %s%s' % (sample, ' …' if len(p['cmds']) > len(picks) else ''))
    lines += ['━' * 12,
              '用法：',
              ' !qqbot help <序号>     看该插件全部指令',
              ' !qqbot help <关键词>   搜索（中英文都行）',
              ' !qqbot help all        列出全部（分页）',
              ' !qqbot help 更新       重新扫描插件目录',
              '注：指令前缀以插件实际注册为准，多数是 /']
    return '\n'.join(lines)


def _fmt_cmd(c):
    alias = ('（别名 %s）' % '、'.join(c['alias'][:3])) if c.get('alias') else ''
    bare = '（不用加 /）' if c.get('bare') else ''
    return (' %s%s%s\n    %s' % (c['cmd'], bare, alias, c['desc'])) if c.get('desc') \
        else (' %s%s%s' % (c['cmd'], bare, alias))


def render_plugin(cat, idx, page=1):
    if idx < 1 or idx > len(cat):
        return '没有第 %d 个插件。发 !qqbot help 看目录。' % idx
    p = cat[idx - 1]
    head = ['📦 %d. %s' % (idx, p['name'])]
    if p['desc']:
        head.append('   %s' % p['desc'])
    if p.get('passive'):
        head.append(PASSIVE_NOTE)
        return '\n'.join(head)
    cmds = p['cmds']
    pages = max(1, (len(cmds) + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, pages))
    sl = cmds[(page - 1) * PER_PAGE: page * PER_PAGE]
    head.append('━' * 12)
    body = '\n'.join(_fmt_cmd(c) for c in sl)
    tail = ['━' * 12, '第 %d/%d 页 ｜ 共 %d 条' % (page, pages, len(cmds))]
    if pages > 1:
        tail.append('翻页：!qqbot help %d %d' % (idx, page + 1 if page < pages else 1))
    return '\n'.join(head) + '\n' + body + '\n' + '\n'.join(tail)


def _w(s):
    """显示宽度：中日韩全角算 2"""
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in (s or ''))


def _pad(s, width):
    return (s or '') + ' ' * max(0, width - _w(s))


CMD_COL_MAX = 22
PAGE_CHARS = 1100


def _full_blocks(cat):
    """按插件切块：每块 = 插件标题 + 指令行（指令列对齐）"""
    blocks = []
    for p in cat:
        if p.get('passive') or not p.get('cmds'):
            continue
        lines = ['▌%s · %d 条' % (p['name'], len(p['cmds']))]
        w = min(CMD_COL_MAX, max(_w(c['cmd']) for c in p['cmds']) + 2)
        for c in p['cmds']:
            desc = c.get('desc') or ''
            cmd_disp = c['cmd'] + ('（不用加 /）' if c.get('bare') else '')
            if c.get('alias'):
                desc = (desc + '（别名 %s）' % '、'.join(c['alias'][:2])) if desc \
                    else '（别名 %s）' % '、'.join(c['alias'][:2])
            if _w(cmd_disp) + 2 > w:
                lines.append('  ' + cmd_disp)
                if desc:
                    lines.append('  ' + _pad('', w) + desc)
            elif desc:
                lines.append('  ' + _pad(cmd_disp, w) + desc)
            else:
                lines.append('  ' + cmd_disp)
        lines.append('')
        blocks.append(lines)
    return blocks


def _paginate(blocks, limit=PAGE_CHARS):
    pages, cur, cur_len = [], [], 0
    for b in blocks:
        blen = sum(len(x) + 1 for x in b)
        if cur and cur_len + blen > limit:
            pages.append(cur)
            cur, cur_len = [], 0
        cur.extend(b)
        cur_len += blen
    if cur:
        pages.append(cur)
    return pages or [[]]


def render_all(cat, page=1):
    """默认视图：全部指令，按插件分组、指令列对齐、分页"""
    blocks = _full_blocks(cat)
    pages = _paginate(blocks)
    n = len(pages)
    page = max(1, min(page, n))
    n_pl, n_cmd, n_all = stats(cat)
    head = ['📚 全部指令（第 %d/%d 页）' % (page, n),
            '%d 个插件 ｜ %d 条指令 ｜ 被动插件 %d 个' % (n_all, n_cmd, n_all - n_pl),
            '━' * 14]
    tail = ['━' * 14]
    if page == n:
        passive = [p['name'] for p in cat if p.get('passive')]
        if passive:
            tail.append('被动（无指令，装上就生效）：%s' % '、'.join(passive))
    if page < n:
        tail.append('下一页：!qqbot help %d' % (page + 1))
    else:
        tail.append('回到开头：!qqbot help')
    tail.append('搜索：!qqbot help <关键词> ｜ 单插件：!qqbot help 插件 <序号> ｜ 目录：!qqbot help 目录')
    return '\n'.join(head + pages[page - 1] + tail)


def render_forward_blocks(cat):
    """合并转发用：返回 [(节点标题, 节点正文), ...]，一条聊天记录装下全部指令"""
    n_pl, n_cmd, n_all = stats(cat)
    blocks = [('总览', '\n'.join([
        '📚 全部指令',
        '%d 个插件 ｜ %d 条指令 ｜ 被动插件 %d 个' % (n_all, n_cmd, n_all - n_pl),
        '━' * 14,
        '搜索：!qqbot help <关键词>',
        '单插件：!qqbot help 插件 <序号>',
        '目录：!qqbot help 目录',
    ]))]
    for b in _full_blocks(cat):
        if not b:
            continue
        title = b[0].lstrip('▌ ').split('·')[0].strip() or '指令'
        blocks.append((title[:20], '\n'.join(b)))
    passive = [p['name'] for p in cat if p.get('passive')]
    if passive:
        blocks.append(('被动插件', '被动（无指令，装上就生效）：\n' + '、'.join(passive)))
    return blocks


def search(cat, kw):
    kw = (kw or '').strip().lower()
    if not kw:
        return render_index(cat)
    hits = []
    for i, p in enumerate(cat, 1):
        if kw in p['name'].lower() or kw in (p['desc'] or '').lower():
            hits.append((i, p['name'], None))
        for c in p['cmds']:
            hay = (c['cmd'] + ' ' + ' '.join(c.get('alias') or []) + ' ' + (c.get('desc') or '')).lower()
            if kw in hay:
                hits.append((i, p['name'], c))
    if not hits:
        return '没找到和「%s」相关的指令。发 !qqbot help 看目录。' % kw
    lines = ['🔍 「%s」匹配 %d 条' % (kw, len(hits)), '━' * 12]
    for i, pname, c in hits[:22]:
        if c is None:
            lines.append('[%d] %s（整个插件）' % (i, pname))
        else:
            lines.append('[%d] %s — %s' % (i, c['cmd'], pname))
            if c.get('desc'):
                lines.append('    %s' % c['desc'])
    if len(hits) > 22:
        lines.append('… 还有 %d 条，换个更具体的关键词' % (len(hits) - 22))
    lines += ['━' * 12, '看插件详情：!qqbot help <序号>']
    return '\n'.join(lines)


def render(cat, arg):
    a = (arg or '').strip()
    if not a:
        return _chunks(render_all(cat, 1))
    low = a.lower()
    if low in ('更新', 'update', 'reload', '重建'):
        newcat = build()
        _n, n_cmd, n_all = stats(newcat)
        return ['🔄 指令目录已重建：%d 个插件、%d 条指令' % (n_all, n_cmd),
                '发 !qqbot help 看全部指令（第 1 页）。']
    if low in ('目录', 'index', '索引', '列表'):
        return _chunks(render_index(cat))
    if a.isdigit():                      # 纯数字 = 页码
        return _chunks(render_all(cat, int(a)))
    m = re.match(r'^(?:插件|plugin)\s*(.+)$', a, re.I)   # 插件 <序号|名字>
    if m:
        key = m.group(1).strip()
        if key.isdigit():
            return _chunks(render_plugin(cat, int(key), 1))
        for i, p in enumerate(cat, 1):
            if key in p['name'] or key in p['key']:
                return _chunks(render_plugin(cat, i, 1))
        return ['没找到插件「%s」。发 !qqbot help 目录 看列表。' % key]
    return _chunks(search(cat, a))
