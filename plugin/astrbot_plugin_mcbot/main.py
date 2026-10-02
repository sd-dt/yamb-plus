"""MC 挂机机器人 QQ 控制插件

指令（全部只接受【管理员私聊】，群内一律静默忽略，操作写日志）：

  !mcbot help                显示帮助
  !mcbot list                列出所有账号 + 当前在线情况
  !mcbot start [账号名]       上线（不带参数则沿用上次启用的账号）
  !mcbot stop                下线当前 MC bot
  !mcbot <账号名> / <数字>     翻转这个号的登录状态（不是"只挂它"）
  !mcbot <账号> <动作> …     Carpet 风格动作：attack/use/jump/move/look/pos/dig/stop…
  !mcbot <数字> ...           账号也能写数字（数字->账号映射见 !mcbot help）
  !mcbot <账号1> <账号2>       翻转这两个号的登录状态（最多同时挂 2 个）
  !mcbot <账号名> command <内容>  对指定账号发游戏指令
  !mcbot command <mc指令>     在游戏内执行服务器指令（自动补斜杠）
  !mcbot yamb <yamb命令>      执行 yamb 自己的指令
  !help                      显示帮助（!help yamb 看挂机指令；!mcbot help 同效）
  !admin <QQ号>              把指定 QQ 号加为管理员（拥有全部权限）
  !admin del <QQ号>          移除管理员
  !admin list                查看当前管理员
  !admin wl                  给所有管理员补齐 AstrBot 会话白名单
  !qqbot start / !qqbot stop  开启/停用 AI 聊天（省 token）

实现要点：
  · 不使用 AstrBot 的 wake_prefix（默认 '/'），直接解析 message_str，
    因此 '!mcbot' 一定生效，不受 AstrBot 配置影响。
  · 权限判定：私聊 + 发送者在管理员列表内。其余情况 stop_event() 静默吞掉，
    群里连报错都不回，只在日志留痕。
  · !qqbot stop 后，所有非指令消息都会 stop_event()，LLM 完全不参与。
  · 通过 /proc/net/route 自动探测 Docker 宿主机网关，无需写死 IP。
"""

import time
import os
import json
import socket
import struct
import asyncio
import urllib.request
import urllib.error

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

# ── 配置 ────────────────────────────────────────────────
# 全部来自环境变量 + fleet.json，代码里没有 QQ 号/MC 账号
def _load_fleet():
    """舰队配置接口：FLEET_FILE 指向 config/fleet.json（compose 里挂载为 /opt/mcbot-config）"""
    _p = os.environ.get("FLEET_FILE", "/opt/mcbot-config/fleet.json")
    try:
        with open(_p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as _e:
        logger.warning("[mcbot] 读取舰队配置失败 (%s): %s —— 账号/管理员列表为空" % (_p, _e))
        return {}


_FLEET = _load_fleet()

# 管理员 QQ 号列表（fleet.json -> admins）
DEFAULT_ADMINS = [str(x) for x in _FLEET.get("admins", [])]

# 备用号：除 !mcbot / !qqbot 外一律静默，避免群里两个机器人同时说话
SPARE_IDS = {str(x) for x in _FLEET.get("spare_self_ids", [])}
CONTROLLER_PORT = int(os.environ.get("CTRL_PORT", "15100"))
CONTROLLER_KEY = os.environ.get("CTRL_KEY", "")

ACCOUNTS = [str(a["name"]).strip() for a in _FLEET.get("accounts", []) if a.get("name")]

# 账号数字别名（fleet.json 里每个账号的 num 字段）
ACCT_NUM = {str(a["num"]): str(a["name"]) for a in _FLEET.get("accounts", [])
            if a.get("num") not in (None, "") and a.get("name")}
NUM_OF = {v: k for k, v in ACCT_NUM.items()}


def acc_of(x):
    """把 1~4 或完整账号名统一成账号名；认不出返回空串"""
    s = str(x or "").strip()
    if s in ACCT_NUM:
        return ACCT_NUM[s]
    if s in ACCOUNTS:
        return s
    return ""

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ASTRBOT_DATA = os.path.dirname(os.path.dirname(PLUGIN_DIR))   # …/astrbot-data（容器内 …/data）
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

HELP_LINES = [
    "🤖 挂机 bot 指北",
    "━━━━━━━━━━━━━━",
    "【账号】数字与名字都行",
] + [" %s = %s" % (k, ACCT_NUM[k]) for k in sorted(ACCT_NUM, key=str)] + [
    "━━━━━━━━━━━━━━",
    "【挂机】最多同时挂 2 个号",
    " !mcbot list",
    "   看所有号的启用与在线状态",
    " !mcbot <账号…>",
    "   翻转这些号：在线的下线、离线的上线",
    "   例：!mcbot 1 2",
    " !mcbot stop",
    "   全部下线；保活不会自动拉起（按你的令）",
    " !mcbot start",
    "   按上次的账号集合启动",
    " !mcbot set <账号…>",
    "   只挂这些，其余全部下线",
    " !mcbot enable|disable <账号…>",
    "   追加启用 / 单独关闭",
    " !mcbot <账号> command <指令>",
    "   以该号身份发一条游戏指令（含 / ）",
    "━━━━━━━━━━━━━━",
    "【动作】攻击/使用/移动/挖方块…",
    " !mcbot yamb help",
    "   列出全部动作和用法",
    "━━━━━━━━━━━━━━",
    "【聊天】",
    " !qqbot on   开启接话（群里也聊）",
    " !qqbot off  关闭，只响应指令",
    "【消息互联】游戏公屏 ↔ QQ 私聊",
    " !connect start",
    "   首次会让你设用户名；之后公屏发言推给你",
    "   你发的纯文字会以「用户名：内容」上公屏",
    " !connect stop",
    "   关闭。任何时候 / 或 # 开头都会被拒绝",
    "━━━━━━━━━━━━━━",
    "【管理】仅管理员可用",
    " !admin <QQ号>",
    "   加管理员；并自动重启让白名单生效",
    " !admin list   看名单",
    " !admin wl     校准会话白名单",
    "━━━━━━━━━━━━━━",
    " !help 看这页",
]
YAMB_HELP = [
    "=== 挂机指令（用法：!mcbot yamb <命令>）===",
    "",
    "【状态】",
    "status                    状态、运行时长、当前坐标",
    "help / 帮助                游戏内帮助",
    "",
    "【移动】",
    "phome <传送点>             传送到预设传送点（需先配 waypoints）",
    "forward <消息>             发公屏并转发随后 2 秒的系统消息",
    "",
    "【控制】",
    "lock                      锁定（防被推动，仅接受 /tpa）",
    "lock hover                滞空锁定（悬空固定）",
    "unlock                    解锁",
    "",
    "【骑乘】",
    "mount [游戏名]             骑乘附近实体（船/马等）",
    "unmount                   下马",
    "cart                      上附近矿车",
    "attack [游戏名]            攻击附近实体",
    "",
    "【背包与容器】",
    "inv                       查看背包",
    "store <容器> <物品> [数量]  存入容器（需先 node reg 登记）",
    "take <容器> <物品> [数量]   从容器取出",
    "drop <物品> [数量]          丢弃物品",
    "",
    "【管理员】",
    "node reg <别名> <x> <y> <z> [-g 区域]   登记方块节点",
    "node list / info <别名> / remove <别名>",
    "brew start <配方> / status / cancel / stop / reload",
    "add <游戏名> / remove <游戏名>            白名单增删",
    "",
    "【QQ 侧 · 白名单管理】",
    "!mcbot yamb list                     查看 4 个 bot 的管理员名单",
    "!mcbot yamb add <游戏名>             加到当前运行的 bot",
    "!mcbot yamb del <游戏名>             从当前运行的 bot 删除",
    "!mcbot yamb alladd <游戏名>          加到全部 4 个 bot",
    "!mcbot yamb alldel <游戏名>          从全部 4 个 bot 删除",
    "!mcbot <账号名> yamb add <游戏名>    加到指定账号（list/del 同理）",
    "!mcbot yamb reload                   重启 yamb 让改动生效（掉线约 40 秒）",
    "",
    "【QQ 侧 · 循环指令】",
    "!mcbot command loop <内容> <间隔tick> 循环发送（1 tick = 50ms，最小 20 tick）",
    "!mcbot command loop list             查看所有循环",
    "!mcbot command loop del <编号>       删除某条循环",
    "!mcbot command loop clear            清空全部循环",
]

# 探测 Docker 宿主机网关（容器默认路由的网关就是宿主机）
def _default_gateway():
    try:
        with open("/proc/net/route", "r") as f:
            for line in f.readlines()[1:]:
                fields = line.strip().split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except Exception:
        pass
    return None


_GW = _default_gateway()
CONTROLLER_URL = "http://%s:%d" % (_GW or "172.17.0.1", CONTROLLER_PORT)


# ── 事件类型过滤器（兼容不同 AstrBot 版本）────────────────
_EMT = filter.EventMessageType


def _mk_event_filter(name):
    value = getattr(_EMT, name, None)
    if value is None:
        return None
    try:
        return filter.event_message_type(value)
    except Exception:
        return None


_D_ALL = _mk_event_filter("ALL")
_D_PRIVATE = _mk_event_filter("PRIVATE_MESSAGE")
_D_GROUP = _mk_event_filter("GROUP_MESSAGE") or _mk_event_filter("GROUP")



def _format_reply(r, fallback):
    """把控制器的回执拼成给人看的多行文本"""
    out = ["已发送：%s ✅" % r.get("sent", fallback)]
    rep = r.get("replies") or []
    if rep:
        out.append("")
        out.append("游戏内回复：")
        for x in rep:
            out.append("  " + str(x))
    else:
        out.append("")
        out.append("（4 秒内没有收到游戏内回复）")
    return chr(10).join(out)


# 信任群：这些群里【所有人】都被当作管理员（可用全部指令）
# 想再加群就往后加；想撤销就把它删掉
TRUSTED_GROUPS = tuple(str(x) for x in _FLEET.get("trusted_groups", []))

# 主人：只有这些 QQ 能切换挂机账号（stop/start/set/enable/disable/toggle/<账号>）
# 其它管理员即使有全部权限也不能切换，防止有人手滑把号全停
OWNER_IDS = tuple(str(x) for x in _FLEET.get("owners", []))


class Plugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.admins = set(DEFAULT_ADMINS)
        self.llm_on = True
        self._primary_at = 0.0
        self._primary_ok = True
        self._load_state()
        logger.info(
            "[mcbot] 插件已加载  控制器=%s  管理员=%s  AI聊天=%s  事件注册=%s",
            CONTROLLER_URL, sorted(self.admins), "开" if self.llm_on else "关", _REG_MODE,
        )
        if _GW is None:
            logger.warning("[mcbot] 未能自动探测宿主机网关，请确认控制器监听在 %s", CONTROLLER_URL)

    # ── 状态持久化 ──────────────────────────────────────
    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("llm_on"), bool):
                self.llm_on = data["llm_on"]
            if isinstance(data.get("admins"), list) and data["admins"]:
                self.admins = set(str(x) for x in data["admins"])
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"llm_on": self.llm_on, "admins": sorted(self.admins)}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[mcbot] 保存状态失败: %s", e)

    # ── 控制器 HTTP ────────────────────────────────────
    @staticmethod
    def _sync_request(method, url, body, timeout):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("x-api-key", CONTROLLER_KEY)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    async def api(self, method, path, body=None, timeout=40):
        url = CONTROLLER_URL + path
        try:
            return await asyncio.to_thread(self._sync_request, method, url, body, timeout)
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8", "replace"))
                return {"ok": False, "message": detail.get("message", "HTTP %d" % e.code)}
            except Exception:
                return {"ok": False, "message": "HTTP %d" % e.code}
        except Exception as e:
            return {"ok": False, "message": "连不上控制器（%s）：%s" % (url, e)}

    # ── 权限 ───────────────────────────────────────────
    def is_admin_private(self, event) -> bool:
        """管理员私聊判定；另外：TRUSTED_GROUPS 里的群，所有人一律放行"""
        try:
            sender = str(event.get_sender_id())
        except Exception:
            return False
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        # 例外：指定群里所有人都是管理员（用 !admin 之外的一切指令）
        if gid and gid in TRUSTED_GROUPS:
            return True
        if sender not in self.admins:
            return False
        return not gid

    # ── AstrBot 会话白名单（!admin 自动放行）───────────
    def _find_astrbot_config(self):
        """找 AstrBot 的 cmd_config.json（数据目录下，可能在根或 config/ 里）"""
        for c in (os.path.join(ASTRBOT_DATA, "cmd_config.json"),
                  os.path.join(ASTRBOT_DATA, "config", "cmd_config.json")):
            if os.path.isfile(c):
                return c
        try:
            for root, dirs, files in os.walk(ASTRBOT_DATA):
                dirs[:] = [d for d in dirs if d not in ("plugins", "plugin_data", "temp", "__pycache__")]
                for f in files:
                    if not f.endswith(".json"):
                        continue
                    p = os.path.join(root, f)
                    try:
                        with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                            if "id_whitelist" in fh.read(300000):
                                return p
                    except Exception:
                        pass
        except Exception:
            pass
        return None

    @staticmethod
    def _read_json(path):
        """读 JSON 并自动处理 UTF-8 BOM（AstrBot 的 cmd_config.json 就带 BOM）。
        返回 (数据, 原来有没有 BOM)"""
        with open(path, "rb") as fh:
            raw = fh.read()
        had_bom = raw[:3] == b"\xef\xbb\xbf"
        txt = raw.decode("utf-8-sig") if had_bom else raw.decode("utf-8", "replace")
        return json.loads(txt), had_bom

    @staticmethod
    def _write_json(path, data, had_bom):
        """写 JSON，原来有 BOM 就保留（不改动文件风格）"""
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        tmp = path + ".tmp-wl"
        with open(tmp, "wb") as fh:
            if had_bom:
                fh.write(b"\xef\xbb\xbf")
            fh.write(body)
        os.replace(tmp, path)

    def _whitelist_session(self, session_id):
        """把会话加进 AstrBot 的 id_whitelist：先改内存配置（立即生效），再写配置文件（重启后仍在）"""
        notes = []
        # ① 内存配置（不保证拿到的是全局配置，尽量试：顶层 + 子对象）
        try:
            cfg = self.context.get_config()
            lst = None
            key = None
            if hasattr(cfg, "get"):
                for k in ("id_whitelist", "id_white_list", "whitelist"):
                    v = cfg.get(k)
                    if isinstance(v, list):
                        lst = v
                        key = k
                        break
                if lst is None:
                    for k in list(cfg.keys()):
                        v = cfg.get(k)
                        if isinstance(v, dict):
                            for k2 in ("id_whitelist", "id_white_list", "whitelist"):
                                if isinstance(v.get(k2), list):
                                    lst = v[k2]
                                    key = k + "." + k2
                                    break
                        if lst is not None:
                            break
            if isinstance(lst, list):
                if session_id in lst:
                    notes.append("内存里已有")
                else:
                    lst.append(session_id)
                    ok = False
                    if hasattr(cfg, "save_config"):
                        try:
                            cfg.save_config()
                            ok = True
                        except Exception as e:
                            notes.append("内存保存失败(%s)" % str(e)[:40])
                    if not ok and hasattr(self.context, "save_config"):
                        try:
                            self.context.save_config()
                            ok = True
                        except Exception as e:
                            notes.append("context 保存失败(%s)" % str(e)[:40])
                    notes.append("内存已加(" + str(key) + ")" + ("" if ok else "（未确认落盘）"))
            else:
                notes.append("内存配置里没有白名单键（只改文件，重启后生效）")
        except Exception as e:
            notes.append("读内存配置失败(%s)" % str(e)[:40])
        # ② 配置文件
        path = self._find_astrbot_config()
        if not path:
            notes.append("没找到 AstrBot 配置文件")
            return "；".join(notes)
        try:
            data, had_bom = self._read_json(path)
            cur = data.get("id_whitelist")
            where = "顶层"
            if not isinstance(cur, list):
                for k in list(data.keys()):
                    v = data.get(k)
                    if isinstance(v, dict) and isinstance(v.get("id_whitelist"), list):
                        cur = v["id_whitelist"]
                        where = k
                        break
            if not isinstance(cur, list):
                notes.append("配置文件里找不到 id_whitelist 数组")
            elif session_id in cur:
                notes.append("文件里已有")
            else:
                cur.append(session_id)
                try:
                    self._write_json(path + ".bak-adminwl", data, had_bom)
                except Exception:
                    pass
                self._write_json(path, data, had_bom)
                notes.append("文件已加(" + where + (",保留BOM" if had_bom else "") + ")" +
                             ("" if data.get("enable_id_white_list", True) else "（白名单开关是关的，本来也不拦）"))
        except Exception as e:
            notes.append("改配置文件失败(%s)" % str(e)[:60])
        return "；".join(notes)

    async def _restart_astrbot(self, delay=3):
        """通知控制器延迟重启 AstrBot，让写进文件的会话白名单立刻生效。
        延迟是为了让"已加管理员"的回执先发出去。失败不抛异常，只记日志。"""
        note = ''
        try:
            r = await self.api("POST", "/restart-astrbot", {"delay": delay}, timeout=10)
            if r and r.get("ok"):
                note = "已安排 %s 秒后自动重启 AstrBot（约 25 秒后恢复）" % delay
            else:
                note = "⚠️ 自动重启请求失败：%s" % (r or {}).get("message", "未知错误")
        except Exception as e:
            note = "⚠️ 自动重启请求异常：%s" % str(e)[:60]
        logger.info("[mcbot] %s", note)
        return note

    def _session_id_for(self, qq, event=None):
        """按当前平台拼出私聊会话 ID，例如 napcat:FriendMessage:123456"""
        plat = "napcat"
        try:
            origin = getattr(event, "unified_msg_origin", "") or ""
            if origin:
                plat = origin.split(":")[0] or "napcat"
        except Exception:
            pass
        return "%s:FriendMessage:%s" % (plat, qq)

    # ── 管理员增删查（!admin）───────────────────────────
    def _admin_lines(self):
        lines = ["=== 管理员（QQ 号）==="]
        for q in sorted(self.admins):
            lines.append("  · %s%s" % (q, "（默认，不可移除）" if q in DEFAULT_ADMINS else ""))
        lines.append("")
        lines.append("添加：!admin <QQ号>       移除：!admin del <QQ号>")
        lines.append("被添加的人拥有全部机器人权限（同样只认私聊，群里静默）")
        return lines

    async def _handle_admin_cmd(self, rest, event=None):
        parts = rest.split(None, 1)
        sub = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("list", "列表", "查看", ""):
            return chr(10).join(self._admin_lines())

        if sub in ("wl", "whitelist", "白名单", "fix", "修复"):
            rows = []
            for q in sorted(self.admins):
                sid = self._session_id_for(q, event)
                rows.append("  · %s -> %s\n      %s" % (q, sid, self._whitelist_session(sid)))
            note = await self._restart_astrbot(3)
            return "会话白名单核对（把不在名单里的管理员补进去）：\n" + chr(10).join(rows) + \
                   chr(10) + chr(10) + note

        if sub in ("del", "delete", "rm", "remove", "删", "删除", "移除"):
            qq = arg
            if not qq:
                return "用法：!admin del <QQ号>"
            if not (qq.isdigit() and 5 <= len(qq) <= 12):
                return "QQ 号看起来不对：%s\n只接受 5~12 位纯数字。" % qq
            if qq in DEFAULT_ADMINS:
                return "❌ %s 是默认管理员，不允许移除（防止把自己锁在外面）。" % qq
            if qq not in self.admins:
                return "（%s 本来就不在管理员列表里）\n\n%s" % (qq, chr(10).join(self._admin_lines()))
            self.admins.discard(qq)
            self._save_state()
            logger.info("[mcbot] 移除管理员 %s（当前 %s）", qq, sorted(self.admins))
            return "✅ 已移除管理员 %s\n\n%s" % (qq, chr(10).join(self._admin_lines()))

        qq = arg if sub in ("add", "添加", "加") else sub
        if not qq:
            return "用法：!admin <QQ号>  /  !admin del <QQ号>  /  !admin list"
        if not (qq.isdigit() and 5 <= len(qq) <= 12):
            return "QQ 号看起来不对：%s\n只接受 5~12 位纯数字。\n\n%s" % (qq, chr(10).join(self._admin_lines()))
        if qq in self.admins:
            sid = self._session_id_for(qq, event)
            wl = self._whitelist_session(sid)
            note = await self._restart_astrbot(3)
            return ("（%s 已经是管理员了；顺手核对了会话白名单）\n"
                    "会话白名单 %s：%s\n%s\n\n%s"
                    ) % (qq, sid, wl, note, chr(10).join(self._admin_lines()))
        self.admins.add(qq)
        self._save_state()
        logger.info("[mcbot] 新增管理员 %s（当前 %s）", qq, sorted(self.admins))
        # 顺带把这个人的私聊会话加进 AstrBot 白名单（否则消息进不到插件）
        sid = self._session_id_for(qq, event)
        wl = self._whitelist_session(sid)
        logger.info("[mcbot] 会话白名单 %s -> %s", sid, wl)
        note = await self._restart_astrbot(3)
        return ("✅ 已把 %s 加为管理员（拥有全部权限）\n"
                "会话白名单 %s：%s\n"
                "%s\n"
                "（约 25 秒后让他私聊发 !help 试试）\n\n%s"
                ) % (qq, sid, wl, note, chr(10).join(self._admin_lines()))

    # ── 动作（Carpet 风格：!mcbot <账号|数字> <动作> [参数]）──────
    ACT_CMDS = ("attack", "use", "attackentity", "useentity", "dig", "useblock",
                "jump", "sneak", "sprint", "move", "look", "pos", "drop", "stop")

    async def _do_action(self, acc, act, args):
        """把 QQ 指令翻译成控制器 /action 的请求体"""
        parts = args.split()
        body = {"account": acc, "act": act}
        usage = None

        if act in ("attackentity", "useentity"):
            if not parts:
                usage = "用法：!mcbot %s %s <玩家名>" % (acc, act)
            else:
                body["target"] = parts[0]
        elif act in ("dig", "useblock"):
            if len(parts) < 3:
                usage = "用法：!mcbot %s %s <x> <y> <z>" % (acc, act)
            else:
                try:
                    body["x"], body["y"], body["z"] = int(parts[0]), int(parts[1]), int(parts[2])
                except Exception:
                    usage = "坐标要填整数，例如 !mcbot %s %s 100 64 -20" % (acc, act)
        elif act == "move":
            if not parts:
                usage = "用法：!mcbot %s move <forward|back|left|right> [秒]" % acc
            else:
                body["dir"] = parts[0].lower()
                if len(parts) > 1:
                    try:
                        body["seconds"] = float(parts[1])
                    except Exception:
                        pass
        elif act == "look":
            if len(parts) < 2:
                usage = "用法：!mcbot %s look <yaw角度> <pitch角度>" % acc
            else:
                try:
                    body["yaw"], body["pitch"] = float(parts[0]), float(parts[1])
                except Exception:
                    usage = "角度要填数字（度），例如 !mcbot %s look 90 0" % acc
        if usage:
            return "❌ " + usage

        # 连点参数：interval N（tick，20 tick = 1 秒）/ continuous / seconds N
        i = 0
        while i < len(parts):
            w = parts[i].lower()
            if w == "interval" and i + 1 < len(parts):
                try:
                    body["interval"] = int(parts[i + 1])
                except Exception:
                    return "❌ interval 后面要填 tick 数，例如 interval 20（=每秒一次）"
                i += 2
                continue
            if w == "continuous":
                body["continuous"] = True
                i += 1
                continue
            if w == "seconds" and i + 1 < len(parts):
                try:
                    body["seconds"] = float(parts[i + 1])
                except Exception:
                    pass
                i += 2
                continue
            i += 1

        r = await self.api("POST", "/action", body, timeout=25)
        if not r.get("ok"):
            return "❌ %s" % r.get("message", "动作失败")
        return "✅ %s\n（%s）" % (r.get("message", "已执行"), acc)

    # ── 消息互联（!connect，任何好友都能用；这是唯一对非管理员开放的入口）──
    def _connect_pending_map(self):
        m = getattr(self, "_connect_pending", None)
        if m is None:
            m = {}
            self._connect_pending = m
        return m

    def _sender_of(self, event):
        try:
            return str(event.get_sender_id())
        except Exception:
            return ""

    async def _handle_connect(self, event, text):
        """!connect start / stop / 状态"""
        parts = text.replace("！", "!").split(None, 1)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        if sub in ("help", "帮助", "?"):
            return ("消息互联用法：\n"
                    "  !connect start   开启（第一次会问你要个用户名）\n"
                    "  !connect stop    关闭\n"
                    "  !connect         查看状态\n"
                    "开启后：游戏公屏里别人的发言会私聊推给你；\n"
                    "你直接发文字，就会以「用户名：内容」发到游戏公屏。\n"
                    "⚠️ 不能发指令（/ 或 # 开头的会被挡掉）")
        try:
            gid = event.get_group_id()
        except Exception:
            gid = ""
        if gid:
            return "消息互联只在私聊里用哦（群里不开放）"
        qq = self._sender_of(event)
        if not qq:
            return "拿不到你的 QQ 号，稍后再试"
        pend = self._connect_pending_map()

        if sub in ("stop", "off", "关", "关闭"):
            pend.pop(qq, None)
            r = await self.api("POST", "/connect", {"qq": qq, "action": "stop"}, timeout=15)
            return (r.get("message") or "已关闭") if r.get("ok") else ("❌ " + str(r.get("message", "关闭失败")))

        if sub in ("start", "on", "开", "开启"):
            r = await self.api("POST", "/connect", {"qq": qq, "action": "start"}, timeout=15)
            if r.get("ok") and r.get("need_username"):
                pend[qq] = True
                return ("请发送你要用的用户名（2~16 个中英文/数字/下划线，不能有空格或冒号）\n"
                        "游戏里你说话会显示成「用户名：内容」")
            pend.pop(qq, None)
            if r.get("ok"):
                return "✅ %s\n直接发文字就会发到游戏公屏，!connect stop 可关闭" % r.get("message", "")
            return "❌ " + str(r.get("message", "开启失败"))

        r = await self.api("POST", "/connect", {"qq": qq, "action": "status"}, timeout=15)
        if not r.get("ok"):
            return "❌ " + str(r.get("message", "查询失败"))
        if r.get("enabled"):
            return "消息互联：【已开启】\n用户名：%s\n（!connect stop 关闭）" % r.get("username", "")
        return ("消息互联：【未开启】\n"
                "发 !connect start 开启（第一次会让你设用户名）\n"
                "开启后：游戏公屏的发言会推给你，你发的文字会以「用户名：内容」发到游戏里")

    async def _relay_to_game(self, event, text):
        """已开启互联的好友发来的纯文字 -> 游戏公屏。
        返回 None = 不归我管（继续走 AI 聊天）；返回 "" = 已处理且不用回复；返回字符串 = 回复它。"""
        try:
            if event.get_group_id():
                return None
        except Exception:
            pass
        qq = self._sender_of(event)
        if not qq:
            return None
        pend = self._connect_pending_map()

        if pend.get(qq):
            r = await self.api("POST", "/connect",
                              {"qq": qq, "action": "start", "username": text.strip()}, timeout=15)
            if r.get("ok") and not r.get("need_username"):
                pend.pop(qq, None)
                return "✅ %s\n直接发文字就会发到游戏公屏，!connect stop 可关闭" % r.get("message", "")
            return "❌ %s\n再发一次用户名试试（2~16 个中英文/数字/下划线）" % str(r.get("message", "用户名不合法"))

        r = await self.api("POST", "/mcsay", {"qq": qq, "text": text}, timeout=20)
        if not r.get("ok"):
            msg = str(r.get("message", ""))
            if "还没开启互联" in msg:
                return None          # 没开启的人：交回 AI 聊天，不打扰
            return "❌ " + msg        # 是互联用户，但被拦了（指令/超长）
        return ""                    # 发送成功：游戏里能看到，不在这儿刷屏

    # ── 指令解析与执行 ──────────────────────────────────
    async def handle_command(self, text, event=None):
        """返回要回复的文本（字符串）。"""
        parts = text.split(None, 1)
        head = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if head == "!qqbot":
            sub = rest.lower()
            if sub in ("stop", "off", "关", "关闭"):
                self.llm_on = False
                self._save_state()
                logger.info("[mcbot] AI 聊天已停用")
                return "已停用 AI 聊天 ✅\n之后只有 !mcbot / !qqbot 指令会响应，其他消息我不再接话。"
            if sub in ("start", "on", "开", "开启"):
                self.llm_on = True
                self._save_state()
                logger.info("[mcbot] AI 聊天已开启")
                return "已开启 AI 聊天 ✅"
            if sub in ("status", "状态", ""):
                return "AI 聊天当前是【%s】" % ("开启" if self.llm_on else "停用")
            return "用法：!qqbot start  /  !qqbot stop"

        if head in ("!admin", "!admins", "!管理员"):
            return await self._handle_admin_cmd(rest, event)

        if head in ("!help", "!帮助", "!h"):
            # 直接复用 !mcbot help 的解析，避免两处维护
            return await self.handle_command("!mcbot help " + rest)

        if head != "!mcbot":
            return None

        first_word = rest.split()[0].lower() if rest else ""
        if not rest or first_word in ("help", "帮助", "?"):
            second_word = rest.split()[1].lower() if len(rest.split()) > 1 else ""
            if second_word in ("yamb", "挂机", "游戏"):
                return chr(10).join(YAMB_HELP)
            return chr(10).join(HELP_LINES)

        verb, _, tail = rest.partition(" ")
        verb = verb.strip()
        tail = tail.strip()
        low = verb.lower()

        # 数字别名：verb 一定是账号位；tail 只在"整体就是个账号"时才换
        _v = acc_of(verb)
        if _v:
            verb = _v
        _t = acc_of(tail)
        if _t:
            tail = _t

        if low == "list" or verb == "列表":
            r = await self.api("GET", "/status", timeout=20)
            if not r.get("ok"):
                return "❌ 查询失败：%s" % r.get("message", "未知错误")

            lines = ["=== MC 账号状态 ==="]
            accs = r.get("accounts", [])
            on = r.get("enabledList") or ([r.get("enabled")] if r.get("enabled") else [])
            for a in accs:
                mark = "★" if a.get("enabled") else "　"
                ig = a.get("ingame")
                if ig:
                    extra = "    已进服（在线 %d 秒）" % int(ig.get("uptime") or 0)
                elif a.get("enabled"):
                    extra = "    未进服 / 无响应"
                else:
                    extra = ""
                _nm = str(a.get("name"))
                _num = NUM_OF.get(_nm, "")
                lines.append("%s %s%s%s" % (mark, (_num + ") " if _num else ""), _nm, extra))
            lines.append("")
            lines.append("服务：%s" % ("运行中" if r.get("service") == "running" else "已停止"))
            lines.append("启用账号：%s（最多同时 2 个）" % ("、".join(on) if on else "无"))
            lines.append("")
            lines.append("双号切换：!mcbot <账号1> <账号2>  （在线的下线、离线的上线）")
            lines.append("指定操作：!mcbot <账号名> command <内容> ／ !mcbot <账号名> yamb <命令>")
            return "\n".join(lines)

        # !mcbot toggle/enable/disable/set <账号...>
        if low in ("toggle", "enable", "disable", "set") and tail:
            accs = [acc_of(x) for x in tail.replace("，", " ").replace(",", " ").split() if x]
            accs = [x for x in accs if x]
            bad = [x for x in accs if x not in ACCOUNTS]
            if bad:
                return "未知账号：%s\n可用：%s" % ("、".join(bad), " / ".join(ACCOUNTS))
            r = await self.api("POST", "/switch", {"mode": low, "accounts": accs}, timeout=150)
            if not r.get("ok"):
                return "❌ 操作失败：%s" % r.get("message", "未知错误")
            label = {"toggle": "已翻转", "enable": "已追加启用", "disable": "已关闭", "set": "已设为只挂"}[low]
            return "%s ✅\n%s" % (label, r.get("message", ""))

        if low == "stop":
            r = await self.api("POST", "/stop", {}, timeout=60)
            if not r.get("ok"):
                return "❌ 下线失败：%s" % r.get("message", "未知错误")
            return "已下线当前 MC bot ✅\n%s" % r.get("message", "")

        if low == "start":
            body = {"account": tail} if tail else {}
            r = await self.api("POST", "/start", body, timeout=90)
            if not r.get("ok"):
                return "❌ 上线失败：%s" % r.get("message", "未知错误")
            return "已上线 %s ✅\n（约 15~25 秒后进服，用 !mcbot list 确认）" % (tail or "上次的账号")

        if low == "command":
            if not tail:
                return "用法：!mcbot command <mc指令>\n例如：!mcbot command phome 工业区"
            r = await self.api("POST", "/command", {"command": tail}, timeout=30)
            if not r.get("ok"):
                return "❌ 发送失败：%s" % r.get("message", "未知错误")
            return _format_reply(r, tail)

        if low == "yamb":
            if not tail:
                return "用法：!mcbot yamb <yamb命令>\n例如：!mcbot yamb status"
            r = await self.api("POST", "/yamb", {"command": tail}, timeout=30)
            if not r.get("ok"):
                return "❌ 发送失败：%s" % r.get("message", "未知错误")
            return _format_reply(r, tail)

        # 指定账号的挂机/白名单指令：!mcbot <账号名> yamb <命令>
        # （白名单管理会给 /yamb 带上 account 字段，交给控制器改对应 yaml）
        # !mcbot <账号1> <账号2>  ->  两个号一起翻转登录状态
        if verb in ACCOUNTS and tail in ACCOUNTS:
            if verb == tail:
                return ("两个账号名一样：%s\n"
                        "单个操作请用：!mcbot %s（只挂它）  或  !mcbot toggle %s") % (verb, verb, verb)
            r = await self.api("POST", "/switch", {"mode": "toggle", "accounts": [verb, tail]}, timeout=150)
            if not r.get("ok"):
                return "❌ 切换失败：%s" % r.get("message", "未知错误")
            return "已翻转这两个号的登录状态 ✅\n%s\n\n（用 !mcbot list 看当前状态）" % r.get("message", "")

        # ── !mcbot <账号|数字> <动作> [参数]（Carpet 风格）──
        if verb in ACCOUNTS and tail:
            _parts = tail.split(None, 1)
            _act = _parts[0].lower()
            if _act in self.ACT_CMDS:
                return await self._do_action(verb, _act, _parts[1].strip() if len(_parts) > 1 else "")

        if verb in ACCOUNTS and tail:
            sub, _, subtail = tail.partition(" ")
            sub = sub.strip().lower()
            if sub in ("yamb", "挂机", "游戏"):
                cmd = subtail.strip()
                if not cmd:
                    return "用法：!mcbot %s yamb <yamb命令>\n例如：!mcbot %s yamb list" % (verb, verb)
                r = await self.api("POST", "/yamb", {"command": cmd, "account": verb}, timeout=30)
                if not r.get("ok"):
                    return "❌ 发送失败：%s" % r.get("message", "未知错误")
                return _format_reply(r, cmd)
            if sub in ("command", "cmd", "指令"):
                cmd = subtail.strip()
                if not cmd:
                    return "用法：!mcbot %s command <内容>\n例如：!mcbot %s command phome 工业区" % (verb, verb)
                r = await self.api("POST", "/command", {"command": cmd, "account": verb}, timeout=30)
                if not r.get("ok"):
                    return "❌ 发送失败：%s" % r.get("message", "未知错误")
                return _format_reply(r, cmd)

            return "未知指令：%s %s\n\n%s" % (verb, tail, "\n".join(HELP_LINES))

        if verb in ACCOUNTS:
            # 翻转这个号自己的状态（和 !mcbot 1 2 的语义一致） _p32
            r = await self.api("POST", "/switch", {"mode": "toggle", "accounts": [verb]}, timeout=150)
            if not r.get("ok"):
                return "❌ 操作失败：%s" % r.get("message", "未知错误")
            return "已翻转 %s 的登录状态 ✅\n%s\n\n（用 !mcbot list 看结果；想「只挂它」用 !mcbot set %s）" % (verb, r.get("message", ""), verb)

        if verb.isdigit():
            return ("未知的账号数字。可用：" + "  ".join("%s=%s" % (k, ACCT_NUM[k]) for k in sorted(ACCT_NUM, key=str)) + "\n\n%s"
                    % "\n".join(HELP_LINES))
        return "未知指令：%s\n\n%s" % (verb, "\n".join(HELP_LINES))

    # ── 统一入口 ────────────────────────────────────────
    @staticmethod
    def _self_id(event) -> str:
        try:
            return str(event.message_obj.self_id)
        except Exception:
            try:
                return str(event.get_self_id())
            except Exception:
                return ""

    def _is_spare(self, event) -> bool:
        return self._self_id(event) in SPARE_IDS

    async def _primary_online(self) -> bool:
        """主号(号A)是否在线。在线则备用号保持静默；掉线则备用号接管群聊。"""
        now = time.time()
        if now - self._primary_at < 30:
            return self._primary_ok
        try:
            r = await self.api("GET", "/primary", timeout=8)
            self._primary_ok = bool(r.get("ok") and r.get("online"))
        except Exception:
            self._primary_ok = True
        self._primary_at = now
        return self._primary_ok

    async def dispatch(self, event: AstrMessageEvent):
        try:
            text = (event.message_str or "").strip()
        except Exception:
            return

        if not text:
            return

        low = text.lower()
        is_cmd = (low.startswith("!mcbot") or low.startswith("!qqbot")
                  or low.startswith("!admin") or low.startswith("!help"))

        # 备用号：主号在线时静默；主号掉线时接管（含群聊 AI）
        if not is_cmd and self._is_spare(event):
            if await self._primary_online():
                self._stop(event)
                return

        # 0) 消息互联：!connect 对任何好友开放（唯一对非管理员开放的入口）
        if low.startswith("!connect") or low.startswith("！connect"):
            try:
                logger.info("[mcbot] 互联指令: %s", text[:120])
                reply = await self._handle_connect(event, text)
            except Exception as e:
                logger.error("[mcbot] 互联指令异常: %s", e)
                reply = "❌ 内部错误：%s" % e
            self._stop(event)
            if reply:
                yield event.plain_result(reply)
            return

        # 1) 指令：仅管理员私聊
        if is_cmd:
            if not self.is_admin_private(event):
                try:
                    logger.info("[mcbot] 忽略非管理员私聊的指令  来自=%s  群=%s  内容=%s",
                                event.get_sender_id(), event.get_group_id() or "(私聊)", text[:120])
                except Exception:
                    logger.info("[mcbot] 忽略一条非管理员指令: %s", text[:120])
                self._stop(event)
                return
            try:
                logger.info("[mcbot] 执行指令: %s", text[:200])
                # ── 切换类指令：主人独占 ──
                _t = text.split(None, 2)
                _v = ""
                if len(_t) > 1:
                    _pp = _t[1].strip().split(None, 1)
                    _v = _pp[0].lower() if _pp else ""
                if _v in ACCOUNTS or _v in ("stop", "start", "set", "enable", "disable", "toggle"):
                    try:
                        _sender = str(event.get_sender_id())
                    except Exception:
                        _sender = ""
                    if _sender not in OWNER_IDS:
                        logger.info("[mcbot] 拒绝切换指令（非主人 %s）：%s", _sender, text[:80])
                        self._stop(event)
                        yield event.plain_result(
                            "❌ 「切换挂机账号」只有主人能用\n"
                            "（想看状态：!mcbot list）")
                        return
                reply = await self.handle_command(text, event)
            except Exception as e:
                logger.error("[mcbot] 指令执行异常: %s", e)
                reply = "❌ 内部错误：%s" % e
            self._stop(event)
            if reply:
                yield event.plain_result(reply)
            return

        # 1.5) 非指令：如果这个好友开了消息互联，就把文字发到游戏公屏
        try:
            fwd = await self._relay_to_game(event, text)
        except Exception as e:
            logger.error("[mcbot] 互联转发异常: %s", e)
            fwd = None
        if fwd is not None:
            self._stop(event)
            if fwd:
                yield event.plain_result(fwd)
            return

        # 2) 非指令：AI 聊天关掉时全部吞掉
        if not self.llm_on:
            self._stop(event)
            return

    @staticmethod
    def _stop(event):
        try:
            event.stop_event()
        except Exception:
            pass


# ── 注册（优先用 ALL，缺失时退化成私聊+群聊两个入口）──────
if _D_ALL is not None:
    Plugin.on_message = _D_ALL(Plugin.dispatch)
    _REG_MODE = "ALL"
else:
    _modes = []
    if _D_PRIVATE is not None:
        Plugin.on_private_message = _D_PRIVATE(Plugin.dispatch)
        _modes.append("PRIVATE_MESSAGE")
    if _D_GROUP is not None:
        Plugin.on_group_message = _D_GROUP(Plugin.dispatch)
        _modes.append("GROUP_MESSAGE")
    _REG_MODE = "+".join(_modes) if _modes else "无"

if _REG_MODE == "无":
    print("[mcbot] !! 事件过滤器全部不可用，插件将不会响应任何消息（请把下面这行发给作者）")
    print("[mcbot] !! EventMessageType 成员: %s" % [x for x in dir(_EMT) if not x.startswith("_")])
else:
    print("[mcbot] 事件注册方式 = %s" % _REG_MODE)
