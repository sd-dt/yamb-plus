"""MC 挂机机器人 QQ 控制插件

指令（全部只接受【管理员私聊】，群内一律静默忽略，操作写日志）：

  !mcbot help                显示帮助
  !mcbot list                列出 4 个账号 + 当前在线情况
  !mcbot start [账号名]       上线（不带参数则沿用上次启用的账号）
  !mcbot stop                下线当前 MC bot
  !mcbot <账号名> / <数字>     翻转这个号的登录状态（不是"只挂它"）
  !mcbot <账号> <动作> …     Carpet 风格动作：attack/use/jump/move/look/pos/dig/stop…
  !mcbot <数字> ...           账号也能写数字：1=sd_dt 2=___donk666___ 3=___Fr0g___ 4=Nekoade_MAP
  !mcbot <账号1> <账号2> <账号3>  翻转这些号的登录状态（最多同时挂 3 个）
  !mcbot <账号名> command <内容>  对指定账号发游戏指令
  !mcbot command <mc指令>     在游戏内执行服务器指令（自动补斜杠）
  !mcbot yamb <yamb命令>      执行 yamb 自己的指令
  !help                      显示帮助（!help yamb 看挂机指令；!mcbot help 同效）
  !admin <QQ号>              把指定 QQ 号加为管理员（拥有全部权限）
  !admin del <QQ号>          移除管理员
  !admin list                查看当前管理员
  !admin wl                  给所有管理员补齐 AstrBot 会话白名单
  !qqbot start / !qqbot stop  开启/停用 AI 聊天（省 token）
  !qqbot add <群号>          把群加进白名单（ID 白名单 + 群分析群列表），使 bot 在该群生效
  !qqbot groups              查看已启用的群
  !qqbot last <群号>         私聊查看该群最近 10 条消息（防撤回）

实现要点：
  · 不使用 AstrBot 的 wake_prefix（默认 '/'），直接解析 message_str，
    因此 '!mcbot' 一定生效，不受 AstrBot 配置影响。
  · 权限判定：私聊 + 发送者在管理员列表内。其余情况 stop_event() 静默吞掉，
    群里连报错都不回，只在日志留痕。
  · !qqbot stop 后，所有非指令消息都会 stop_event()，LLM 完全不参与。
  · 通过 /proc/net/route 自动探测 Docker 宿主机网关，无需写死 IP。
"""

import re
import time
import os
import json
import socket
import struct
import asyncio
import urllib.request
import urllib.error
from collections import deque
from astrbot.core.message.components import At, Plain, Record

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

# ── 配置 ────────────────────────────────────────────────
DEFAULT_ADMINS = [x.strip() for x in os.environ.get("MCBOT_ADMINS", "").split(",") if x.strip()]

# 备用号：除 !mcbot / !qqbot 外一律静默，避免群里两个机器人同时说话
SPARE_IDS = set()  # _p68: 双号已取消，只保留主号
CONTROLLER_PORT = 15100
CONTROLLER_KEY = os.environ.get("MCBOT_CONTROLLER_KEY", "change-me")

ACCOUNTS = ["sd_dt", "___Fr0g___", "___donk666___", "Nekoade_MAP"]

# 账号数字别名（用户指定的顺序，注意和 ACCOUNTS 的顺序不同）
ACCT_NUM = {"1": "sd_dt", "2": "___donk666___", "3": "___Fr0g___", "4": "Nekoade_MAP"}
NUM_OF = {v: k for k, v in ACCT_NUM.items()}


def _fmt_dur(sec):
    """把秒数格式化成人类可读时长（3 小时 12 分 / 12 分 / 45 秒）"""
    try:
        sec = int(sec)
    except Exception:
        return "0 秒"
    if sec < 60:
        return "%d 秒" % sec
    m, _s = divmod(sec, 60)
    if m < 60:
        return "%d 分" % m
    h, m = divmod(m, 60)
    if h < 24:
        return "%d 小时 %d 分" % (h, m)
    d, h = divmod(h, 24)
    return "%d 天 %d 小时" % (d, h)


def acc_of(x):
    """把 1~4 或完整账号名统一成账号名；认不出返回空串"""
    s = str(x or "").strip()
    if s in ACCT_NUM:
        return ACCT_NUM[s]
    if s in ACCOUNTS:
        return s
    return ""


TTS_TRIGGER_RE = re.compile(r"^\s*/?\s*(?:请输入文本|输入文本|tts)", re.IGNORECASE)


def _extract_tts_text(t):
    """从 /请输入文本 xxx 这类消息里取出要朗读的文字；不是该指令则返回 None"""
    s = str(t or "").strip()
    # 去掉开头的 @提及（带不带括号都行），例如 "@bot(123) /请输入文本 你好"
    s = re.sub(r"^@[^\s]*\s*", "", s)
    m = TTS_TRIGGER_RE.match(s)
    if not m:
        return None
    return s[m.end():].lstrip("：:,， \t")


# pkg159: edge-tts 的音色按语种划分，中文音色念日文假名会直接 NoAudioReceived，
# 所以按文字里出现的字符集挑一个能念的音色。
_JA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")   # 平假名 + 片假名
_KO_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")   # 谚文
_RU_RE = re.compile(r"[\u0400-\u04ff]")                # 西里尔
_ZH_RE = re.compile(r"[\u4e00-\u9fff]")                # 汉字
_LATIN_RE = re.compile(r"[A-Za-z]")


def _pick_tts_voice(text):
    """按文字语种返回合适音色；返回 None 表示用配置里的默认音色"""
    if _JA_RE.search(text):
        return "ja-JP-NanamiNeural"
    if _KO_RE.search(text):
        return "ko-KR-SunHiNeural"
    if _RU_RE.search(text):
        return "ru-RU-SvetlanaNeural"
    if _LATIN_RE.search(text) and not _ZH_RE.search(text):
        return "en-US-AvaMultilingualNeural"
    return None

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ASTRBOT_DATA = os.path.dirname(os.path.dirname(PLUGIN_DIR))   # …/astrbot-data（容器内 …/data）
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# AI 聊天停用时吞消息的日志限流间隔（秒）
SWALLOW_LOG_INTERVAL = 60

HELP_LINES = [
    "🤖 挂机 bot 指北",
    "━━━━━━━━━━━━━━",
    "【账号】数字与名字都行",
    " 1 = sd_dt",
    " 2 = ___donk666___",
    " 3 = ___Fr0g___",
    " 4 = Nekoade_MAP",
    "━━━━━━━━━━━━━━",
    "【挂机】最多同时挂 3 个号",
    " !mcbot list",
    "   看 4 个号的启用与在线状态",
    " !mcbot <账号…>",
    "   翻转这些号：在线的下线、离线的上线",
    "   例：!mcbot 1 2 3",
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
    " !qqbot status  查看 AI 聊天是开还是关",
    "━━━━━━━━━━━━━━",
    "【模型】",
    " !ds                 查 DeepSeek API 余额",
    "━━━━━━━━━━━━━━",
    "【群管理】",
    " !qqbot add <群号>   把群加进白名单（会话白名单 + 群分析名单）",
    " !qqbot groups       看已启用的群",
    " !qqbot last <群号>  私聊看该群最近 10 条（防撤回）",
    "━━━━━━━━━━━━━━",
    "【投影渲染】",
    " 群里直接发 .litematic 附件 → 自动回预览图",
    "   俯视投影出 1 张；立体建筑出 2 张（正面 / 背面）",
    " /litematic          看渲染说明与限制",
    "━━━━━━━━━━━━━━",
    "【语音】把文字合成语音发出来",
    " /请输入文本 <文字>   例：/请输入文本 今天也要开心哦",
    "   也可以用 /tts 或 /输入文本，私聊和群里都能用",
    "━━━━━━━━━━━━━━",
    "【帮助】",
    " !qqbot help         全部插件的指令（合成一条聊天记录发送）",
    " !qqbot help <关键词> 搜索，例：!qqbot help 好感",
    " !qqbot help 目录      只看插件清单",
    " !qqbot help 插件 <序号> 单看某个插件",
    " !qqbot help 更新      装了新插件后重建目录",
    "━━━━━━━━━━━━━━",
    "【转钱】仅主人；每天 04:00 自动执行",
    " !mcbot addpay <账号>",
    "   登记转钱小号（免密码：发微软网址+授权码完成登录）",
    " !mcbot payauth <账号>    重发授权码（登录失效时用）",
    " !mcbot delpay <账号>     移除小号",
    " !mcbot paylist           看配置和今日尝试情况",
    " !mcbot paytest [账号]    立刻跑一次转钱流程",
    " !mcbot payset <收款人> <金额>",
    "   设置收款人与金额（全号统一，每天转一次）",
    " !mcbot payon/payoff <账号>",
    "   设定某个小号是否参与转钱（payoff = 仍会登录一次，但不转账）",
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
    "【动作 · Carpet 风格】用法：!mcbot <账号> <动作> [参数]",
    "attack                     左键攻击最近实体（没目标就空挥）",
    "use                        右键使用手上物品",
    "attackentity <玩家名>       左键攻击指定玩家",
    "useentity <玩家名>          空手右键指定玩家（两种右键包都试，试坐其头顶）",
    "slot <1-9>                切换主手快捷栏格子（不带参数=查看当前）",
    "dig <x> <y> <z>            挖指定坐标的方块",
    "useblock <x> <y> <z>       右键指定坐标的方块（开箱/按按钮等）",
    "jump                       跳一下",
    "sneak / sprint             潜行 / 疾跑（开关切换，再发一次就关）",
    "move <方向> [秒]           走动（forward/back/left/right，默认 1 秒）",
    "look <yaw> <pitch>         转视角（角度制）",
    "pos                        查看坐标和朝向",
    "drop                       丢出一个手上的物品（drop all=丢掉背包全部）",
    "stop                       一键停：松开所有持续动作、停掉全部连点",
    "连点通用（attack/use 等）：interval N=每 N tick 一次（20=1秒，最小 2）；",
    "   continuous=持续按住；seconds N=N 秒后自动停；每号最多 2 个连点",
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
TRUSTED_GROUPS = ("1098157223",)

# 主人：只有这些 QQ 能切换挂机账号（stop/start/set/enable/disable/toggle/<账号>）
# 其它管理员即使有全部权限也不能切换，防止有人手滑把号全停
OWNER_IDS = tuple(x.strip() for x in os.environ.get("MCBOT_OWNERS", "").split(",") if x.strip())

# pkg134: 只有这个人能用 /猪；黑名单里的人永远不会被贴 ㊗️
PIG_OWNER = os.environ.get("MCBOT_PIG_OWNER", "")
PIG_BLACKLIST = tuple(x.strip() for x in os.environ.get("MCBOT_PIG_BLACKLIST", "").split(",") if x.strip())

# pkg136: 群里放行的第三方插件指令（目前是“今日老婆/wifepicker”全套）。
# 只有 llm_on=True（未 !qqbot stop）时才放行；stop 时依旧全静默。
GROUP_PASS_CMDS = {
    "今日老婆", "抽老婆", "jrlp", "dailywife", "wife",
    "我的老婆", "抽取历史", "wdlp", "mywife",
    "分手", "fs", "breakup", "离婚",
    "强娶", "qiangqu", "forcemarry",
    "关系图", "gxt", "relations",
    "rbq排行", "rbqph", "wifeleaderboard",
    "花心大萝卜榜", "海王榜", "花心榜", "hxb", "hxdlbb", "hwb",
    "重置记录", "czjl", "重置强娶时间", "czqqsj", "重置求婚时间", "czqhsj",
    "抽老婆帮助", "老婆插件帮助", "clpbz", "wifehelp",
    "求婚", "qh", "propose",
    "挑选老婆", "txlp", "pickwife",
    "查看好感度", "affinity", "查询好感度",
    "调整好感度", "setaffinity",
}


class Plugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.admins = set(DEFAULT_ADMINS)
        self.llm_on = True
        self._primary_at = 0.0
        self._primary_ok = True
        self._swallow_count = 0
        self._last_swallow_log = 0.0
        # 防撤回：每个群最近 10 条消息（内存环形缓冲）
        self._recent_group_msgs = {}
        self._recent_limit = 10
        self._pig_subs = {}
        self._load_pig_subs()
        # pkg180: 主动轮询群消息来贴㊗️，绕开 AstrBot 对普通群消息的过滤
        self._pig_bot = None
        self._pig_reacted = set()
        self._pig_inited = set()
        self._pig_poll_task = None
        # pkg126: !qqbot stop 时把群会话移出白名单，这里记住移走了哪些群
        self.muted_groups = []
        self._load_state()
        logger.info(
            "[mcbot] 插件已加载  控制器=%s  管理员=%s  AI聊天=%s  事件注册=%s",
            CONTROLLER_URL, sorted(self.admins), "开" if self.llm_on else "关", _REG_MODE,
        )
        if _GW is None:
            logger.warning("[mcbot] 未能自动探测宿主机网关，请确认控制器监听在 %s", CONTROLLER_URL)

    # ── 状态持久化 ──────────────────────────────────────
    # ── pkg126: 群聊总开关（直接动 AstrBot 白名单）──────────
    def _apply_group_whitelist(self, enable_group):
        """enable_group=False：把所有 *:GroupMessage:* 会话移出 AstrBot 的 id_whitelist。
        白名单检查发生在插件之前，群消息会被核心直接丢弃，任何插件都不可能再在群里说话。
        enable_group=True：把 state 里记住的群会话加回去。返回 (改动条数, 说明)。"""
        changed = [0]
        notes = []

        def _edit(lst):
            if not isinstance(lst, list):
                return
            if enable_group:
                for gid in list(self.muted_groups or []):
                    if gid not in lst:
                        lst.append(gid)
                        changed[0] += 1
            else:
                keep = [x for x in lst if ":GroupMessage:" not in str(x)]
                changed[0] += len(lst) - len(keep)
                lst[:] = keep

        # ① 内存配置（立即生效）
        try:
            cfg = self.context.get_config()
            found = False
            if hasattr(cfg, "get"):
                for k in ("id_whitelist", "id_white_list", "whitelist"):
                    v = cfg.get(k)
                    if isinstance(v, list):
                        _edit(v); found = True; break
                if not found:
                    for k in list(cfg.keys()):
                        v = cfg.get(k)
                        if isinstance(v, dict):
                            for k2 in ("id_whitelist", "id_white_list", "whitelist"):
                                if isinstance(v.get(k2), list):
                                    _edit(v[k2]); found = True; break
                        if found:
                            break
            if hasattr(cfg, "save_config"):
                cfg.save_config()
            elif hasattr(self.context, "save_config"):
                self.context.save_config()
            notes.append("内存已生效" if found else "内存未找到白名单")
        except Exception as e:
            notes.append("内存失败(%s)" % str(e)[:40])

        # ② 配置文件（重启后仍保持）
        path = self._find_astrbot_config()
        if not path:
            notes.append("未找到配置文件")
            return changed[0], "；".join(notes)
        try:
            data, had_bom = self._read_json(path)
            lst = data.get("id_whitelist")
            if not isinstance(lst, list):
                for k in list(data.keys()):
                    v = data.get(k)
                    if isinstance(v, dict) and isinstance(v.get("id_whitelist"), list):
                        lst = v["id_whitelist"]; break
            if isinstance(lst, list):
                _edit(lst)
                self._write_json(path + ".bak-groupswitch", data, had_bom)
                self._write_json(path, data, had_bom)
                notes.append("文件已更新")
            else:
                notes.append("文件里没有白名单")
        except Exception as e:
            notes.append("文件失败(%s)" % str(e)[:40])
        return changed[0], "；".join(notes)

    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("llm_on"), bool):
                self.llm_on = data["llm_on"]
            if isinstance(data.get("admins"), list) and data["admins"]:
                self.admins = set(str(x) for x in data["admins"])
            if isinstance(data.get("muted_groups"), list):
                self.muted_groups = [str(x) for x in data["muted_groups"]]
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"llm_on": self.llm_on, "admins": sorted(self.admins),
                           "muted_groups": list(self.muted_groups or [])}, f,
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

    # ── 群白名单（!qqbot add）────────────────────────────
    def _group_umo(self, group_id, event=None):
        """按当前平台拼群会话 ID，例如 napcat:GroupMessage:123456"""
        plat = "napcat"
        try:
            origin = getattr(event, "unified_msg_origin", "") or ""
            if origin:
                plat = origin.split(":")[0] or "napcat"
        except Exception:
            pass
        return "%s:GroupMessage:%s" % (plat, group_id)

    def _add_group_to_daily_analysis(self, group_id):
        """把群号加进群分析插件的 basic.group_list（先内存热更，再写文件兜底）

        该插件的 ConfigManager 直接持有 AstrBotConfig（self.config），
        其 reload_config() 是空实现，所以必须改实例内存里的 config 才即时生效。
        """
        notes = []
        cfg_name = "astrbot_plugin_qq_group_daily_analysis_config.json"

        def _push(lst, label):
            if not isinstance(lst, list):
                notes.append(label + "里没有 group_list")
                return False
            if group_id in lst:
                notes.append(label + "已有")
                return False
            lst.append(group_id)
            return True

        # ① 插件实例内存配置（立即生效）
        try:
            for md in (self.context.get_all_stars() or []):
                blob = "%s %s" % (getattr(md, "name", "") or "", getattr(md, "module_path", "") or "")
                if "qq_group_daily_analysis" not in blob:
                    continue
                inst = getattr(md, "star_cls", None)
                if isinstance(inst, type):
                    inst = None
                cfg = getattr(inst, "config", None) if inst is not None else None
                if cfg is None or not hasattr(cfg, "get"):
                    notes.append("群分析实例配置不可达（仅写文件）")
                    break
                basic = cfg.get("basic")
                if not isinstance(basic, dict):
                    notes.append("群分析配置无 basic（仅写文件）")
                    break
                if _push(basic.get("group_list"), "群分析"):
                    try:
                        if hasattr(cfg, "save_config"):
                            cfg.save_config()
                        notes.append("群分析已加(内存)")
                    except Exception as e:
                        notes.append("群分析内存保存失败(%s)" % str(e)[:40])
                break
        except Exception as e:
            notes.append("读群分析实例失败(%s)" % str(e)[:40])

        # ② 配置文件兜底（重启后仍在）
        path = os.path.join(ASTRBOT_DATA, "config", cfg_name)
        if os.path.isfile(path):
            try:
                data, had_bom = self._read_json(path)
                basic = data.get("basic")
                if isinstance(basic, dict) and _push(basic.get("group_list"), "群分析文件"):
                    self._write_json(path + ".bak-qqbotadd", data, had_bom)
                    self._write_json(path, data, had_bom)
                    notes.append("群分析文件已加" + ("(保留BOM)" if had_bom else ""))
            except Exception as e:
                notes.append("改群分析文件失败(%s)" % str(e)[:50])
        else:
            notes.append("未找到群分析配置文件")
        return "；".join(notes)

    def _group_ids_in_whitelist(self):
        """读 cmd_config 的 id_whitelist，返回所有群会话 ID（供回执展示）"""
        path = self._find_astrbot_config()
        if not path:
            return []
        try:
            data, _had = self._read_json(path)
            lst = (data.get("platform_settings") or {}).get("id_whitelist")
            if not isinstance(lst, list):
                for _k, v in data.items():
                    if isinstance(v, dict) and isinstance(v.get("id_whitelist"), list):
                        lst = v["id_whitelist"]
                        break
            return [str(x) for x in (lst or []) if ":GroupMessage:" in str(x)]
        except Exception:
            return []

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
                "jump", "sneak", "sprint", "move", "look", "pos", "drop", "slot", "stop")

    async def _do_action(self, acc, act, args):
        """把 QQ 指令翻译成控制器 /action 的请求体"""
        parts = args.split()
        body = {"account": acc, "act": act}
        usage = None

        # drop all：丢掉背包里所有东西（yamb 的 dropall 动作）
        if act == "drop" and parts and parts[0].lower() == "all":
            act = "dropall"
            body["act"] = "dropall"
            parts = parts[1:]

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
        elif act == "slot":
            if not parts:
                pass  # 不带参数 = 查看当前格子
            else:
                try:
                    n = int(parts[0])
                    if not 1 <= n <= 9:
                        raise ValueError
                    body["slot"] = n
                except Exception:
                    usage = "用法：!mcbot %s slot <1-9>" % acc
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
    def _help_render(self, arg):
        """渲染全插件指令目录（helpcatalog.py，首次调用时按文件路径加载）"""
        try:
            import importlib.util
            here = os.path.dirname(os.path.abspath(__file__))
            mod = globals().get("_HELPCAT_MODULE")
            if mod is None:
                spec = importlib.util.spec_from_file_location(
                    "mcbot_helpcatalog", os.path.join(here, "helpcatalog.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                globals()["_HELPCAT_MODULE"] = mod
            cat = mod.get()
            if not (arg or "").strip():
                return self._help_nodes(mod.render_forward_blocks(cat))
            return mod.render(cat, arg)
        except Exception as e:
            logger.exception("[mcbot] 帮助目录渲染失败")
            return "帮助目录读取失败：%s" % e

    @staticmethod
    def _help_nodes(blocks):
        """把全部指令整合成合并转发（QQ 聊天记录）。

        QQ 单条合并转发的内容总量有上限（实测约 17KB，超过后 NapCat 会返回
        「发送转发消息失败」），因此按体积自动拆成多条转发发送。
        """
        from astrbot.core.message.components import Node, Nodes, Plain
        max_bytes = 14000
        groups, cur, cur_bytes = [], [], 0
        for title, text in blocks:
            size = len(text.encode("utf-8")) + 64
            if cur and cur_bytes + size > max_bytes:
                groups.append(cur)
                cur, cur_bytes = [], 0
            cur.append((title, text))
            cur_bytes += size
        if cur:
            groups.append(cur)
        return [
            Nodes(nodes=[Node(content=[Plain(text)], name=(title or "指令"), uin="10000")
                         for title, text in group])
            for group in groups
        ]

    # ── DeepSeek 余额（!ds）─────────────────────────────
    def _deepseek_key_base(self):
        """从 AstrBot 主配置里取 deepseek 的 key 和 api_base"""
        here = os.path.dirname(os.path.abspath(__file__))       # .../plugins/astrbot_plugin_mcbot
        data_dir = os.path.dirname(os.path.dirname(here))        # .../data
        cfg = os.path.join(data_dir, "cmd_config.json")
        with open(cfg, encoding="utf-8-sig") as f:
            d = json.load(f)
        src = None
        for s in d.get("provider_sources") or []:
            sid = str(s.get("id") or "")
            if sid == "deepseek" or "deepseek" in sid.lower():
                src = s
                break
        if not src:
            raise RuntimeError("cmd_config.json 里找不到 deepseek provider")
        keys = src.get("key")
        if isinstance(keys, list):
            key = keys[0] if keys else ""
        else:
            key = keys or ""
        base = (src.get("api_base") or "https://api.deepseek.com").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return key, base

    async def _deepseek_balance(self):
        """查询 DeepSeek 账户余额"""
        try:
            key, base = self._deepseek_key_base()
        except Exception as e:
            return "❌ 读不到 DeepSeek 配置：%s" % e
        if not key:
            return "❌ DeepSeek 配置里没有 api key"
        url = base + "/user/balance"

        def _get():
            req = urllib.request.Request(
                url,
                headers={"Authorization": "Bearer " + key,
                         "Accept": "application/json"},
                method="GET")
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())

        try:
            data = await asyncio.to_thread(_get)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:120]
            except Exception:
                pass
            return "❌ 查询失败 HTTP %s\n%s" % (e.code, body)
        except Exception as e:
            return "❌ 查询失败：%s" % (str(e) or type(e).__name__)

        lines = ["💰 DeepSeek 余额",
                 "账户可用：%s" % ("✅ 是" if data.get("is_available") else "❌ 否")]
        infos = data.get("balance_infos") or []
        if not infos:
            lines.append("（接口未返回余额明细）")
        for b in infos:
            cur = b.get("currency") or "?"
            lines.append("%s 总余额 %s" % (cur, b.get("total_balance")))
            lines.append("   充值 %s ｜ 赠送 %s" % (b.get("topped_up_balance"),
                                                  b.get("granted_balance")))
        lines.append("查询于 %s（服务器时间）" % time.strftime("%m-%d %H:%M:%S"))
        logger.info("[mcbot] !ds 查询余额成功: %s", data.get("is_available"))
        return "\n".join(lines)

    async def handle_command(self, text, event=None):
        """返回要回复的文本（字符串）。"""
        parts = text.split(None, 1)
        head = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if head == "!qqbot":
            sub = rest.lower()
            _qv = rest.split()[0].lower() if rest else ""
            if _qv in ("help", "帮助", "?", "h", "指令", "命令"):
                _harg = rest.split(None, 1)[1].strip() if len(rest.split(None, 1)) > 1 else ""
                return self._help_render(_harg)
            if _qv in ("add", "加", "添加"):
                _arg = rest.split()[1].strip() if len(rest.split()) > 1 else ""
                if not _arg:
                    return ("用法：!qqbot add <群号>\n"
                            "例如：!qqbot add 123456789\n\n"
                            "会按 napcat:GroupMessage:<群号> 写入「白名单 ID 列表」，\n"
                            "并把群号写入群分析插件的「群组白/黑名单列表」。")
                if not re.fullmatch(r"\d{5,12}", _arg):
                    return ("❌ 群号格式不对：%s\n"
                            "只填纯数字群号（5-12 位），不用带 napcat:GroupMessage: 前缀。" % _arg)
                _umo = self._group_umo(_arg, event)
                _wl = self._whitelist_session(_umo)
                _dg = self._add_group_to_daily_analysis(_arg)
                _gids = self._group_ids_in_whitelist()
                _lines = [
                    "✅ 群 %s 已加入白名单" % _arg,
                    "",
                    "① 白名单 ID 列表（bot 在该群响应）：",
                    "   %s" % _umo,
                    "   %s" % _wl,
                    "② 群组白名单（群分析覆盖范围）：",
                    "   %s" % _dg,
                    "",
                    "当前群白名单（%d 个）：" % len(_gids),
                ]
                for _g in _gids:
                    _lines.append("  · " + _g)
                _lines.append("")
                _lines.append("提示：把 bot 拉进该群后 @它 / 发指令即可。")
                logger.info("[mcbot] qqbot add %s -> %s", _arg, _umo)
                return chr(10).join(_lines)
            if _qv in ("groups", "群列表", "list"):
                _gids = self._group_ids_in_whitelist()
                if not _gids:
                    return "当前没有已启用的群（白名单里没有 GroupMessage 项）"
                _lines = ["=== 已启用群（%d 个）===" % len(_gids)]
                for _g in _gids:
                    _lines.append("  · " + _g)
                _lines.append("")
                _lines.append("添加：!qqbot add <群号>")
                return chr(10).join(_lines)
            if _qv in ("last", "最近", "历史", "消息"):
                _arg = rest.split()[1].strip() if len(rest.split()) > 1 else ""
                return self._last_group_messages(event, _arg)
            if sub in ("stop", "off", "关", "关闭"):
                # pkg134: stop = 群里全部功能停止（含 /猪 系指令与贴㊗️），只留防撤回
                self.llm_on = False
                self._save_state()
                logger.info("[mcbot] 群聊功能已全部停止（仅保留防撤回记录）")
                return ("已停止全部群聊功能 ✅\n"
                        "群里不会再收到任何消息，/猪、/取消猪、/猪列表 也一并停用。\n"
                        "防撤回仍在后台记录：私聊发 !qqbot last <群号> 可回放。\n"
                        "私聊 LLM 不受影响；发 !qqbot on 恢复。")
            if sub in ("start", "on", "开", "开启"):
                self.llm_on = True
                self._save_state()
                logger.info("[mcbot] 群 AI 聊天已开启")
                return "已开启群聊 AI ✅（群里恢复 AI 回复）"
            if sub in ("status", "状态", ""):
                return "AI 聊天当前是【%s】" % ("开启" if self.llm_on else "停用")
            return "用法：!qqbot start  /  !qqbot stop"

        if head in ("!ds", "!余额", "!balance", "!deepseek"):
            return await self._deepseek_balance()

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
            # 【pkg99】按"用户指定编号"排序输出，避免出现 1) 3) 2) 4) 这种跳号
            def _nkey(_a):
                _n = NUM_OF.get(str(_a.get("name")), "")
                return (0, int(_n)) if str(_n).isdigit() else (1, 0)
            for a in sorted(accs, key=_nkey):
                mark = "★" if a.get("enabled") else "　"
                ig = a.get("ingame")
                if ig and ig.get("minecraft", True):
                    extra = "    已进服（在线 %s）" % _fmt_dur(ig.get("uptime") or 0)
                elif a.get("enabled"):
                    extra = "    未进服 / 无响应"
                else:
                    extra = ""
                _nm = str(a.get("name"))
                _num = NUM_OF.get(_nm, "")
                lines.append("%s %s%s%s" % (mark, (_num + ") " if _num else ""), _nm, extra))
            lines.append("")
            lines.append("服务：%s" % ("运行中" if r.get("service") == "running" else "已停止"))
            lines.append("启用账号：%s（最多同时 3 个）" % ("、".join(on) if on else "无"))
            lines.append("")
            lines.append("多号切换：!mcbot <账号…>（1~3 个，例 !mcbot 1 2 3）  （在线的下线、离线的上线）")
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

        # ── 转钱（_p69）：仅主人 ──
        if low in self.PAY_VERBS:
            if str(event.get_sender_id()) not in OWNER_IDS:
                return "❌ 转钱指令只有主人能用"
            return await self._handle_pay(low, tail)

        # !mcbot <账号…>（2~3 个）-> 一起翻转登录状态
        _toks = rest.replace("，", " ").replace(",", " ").split()
        if len(_toks) >= 2:
            _accs = [acc_of(x) for x in _toks]
            if all(_accs):
                if len(set(_accs)) != len(_accs):
                    return "账号写重了：%s" % "、".join(_accs)
                if len(_accs) > 3:
                    return ("一次最多翻转 3 个号（这次写了 %d 个）：%s\n"
                            "例：!mcbot 1 2 3") % (len(_accs), "、".join(_accs))
                r = await self.api("POST", "/switch", {"mode": "toggle", "accounts": _accs}, timeout=150)
                if not r.get("ok"):
                    return "❌ 切换失败：%s" % r.get("message", "未知错误")
                return "已翻转 %d 个号的登录状态 ✅\n%s\n\n（用 !mcbot list 看当前状态）" % (
                    len(_accs), r.get("message", ""))

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
            return ("账号数字只有 1~4：1=sd_dt  2=___donk666___  3=___Fr0g___  4=Nekoade_MAP\n\n%s"
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


    # ── 转钱（_p69）：仅主人 ──
    PAY_VERBS = ("addpay", "delpay", "paylist", "paytest", "payset", "payauth", "payon", "payoff")

    async def _handle_pay(self, verb, tail):
        tail = (tail or "").strip()
        if verb == "addpay":
            if not tail:
                return ("用法：!mcbot addpay <账号>\n"
                        "登记一个转钱小号（仅用于每天凌晨 4 点登录转钱）\n"
                        "无需密码：登记后机器人会发微软官方网址和一次性授权码，\n"
                        "浏览器打开网址输入授权码即可完成登录；token 只存在服务器本地\n"
                        "注意：若 yamb 正在运行会自动重启，在线的号会掉线约 40 秒")
            r = await self.api("POST", "/pay-add", {"account": tail}, timeout=150)
            return r.get("message", "已登记 ✅") if r.get("ok") \
                else "❌ 登记失败：%s" % r.get("message", "未知错误")
        if verb == "payauth":
            if not tail:
                return "用法：!mcbot payauth <账号>　（重新发设备授权码；token 失效/登录失败时用）"
            r = await self.api("POST", "/pay-auth", {"account": tail}, timeout=60)
            return r.get("message", "已发送 ✅") if r.get("ok") \
                else "❌ 发送失败：%s" % r.get("message", "未知错误")
        if verb == "delpay":
            if not tail:
                return "用法：!mcbot delpay <账号>"
            r = await self.api("POST", "/pay-del", {"account": tail}, timeout=30)
            return r.get("message", "已移除 ✅") if r.get("ok") \
                else "❌ 移除失败：%s" % r.get("message", "未知错误")
        if verb == "paylist":
            r = await self.api("GET", "/pay-list", timeout=20)
            return r.get("message", "（空）") if r.get("ok") \
                else "❌ 查询失败：%s" % r.get("message", "未知错误")
        if verb == "paytest":
            r = await self.api("POST", "/pay-run", {"account": tail} if tail else {}, timeout=20)
            return r.get("message", "已触发 ✅") if r.get("ok") \
                else "❌ 触发失败：%s" % r.get("message", "未知错误")
        # payon/payoff（_p73）：单号参与/暂停转钱——直接写 pay.json
        if verb in ("payon", "payoff"):
            if not tail:
                return ("用法：!mcbot payon <账号> ／ !mcbot payoff <账号>\n"
                        "单独设定某个小号是否参与每天 04:00 的自动转钱\n"
                        "payoff = 暂停转账，但每天 04:00 仍会登录进服一次（马上又下线）；!mcbot paylist 可看状态")
            pay_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pay.json")
            try:
                with open(pay_file, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
            if not isinstance(cfg, dict):
                cfg = {}
            if tail not in (cfg.get("accounts") or []):
                return "未知转钱账号：%s（用 !mcbot paylist 查看名单）" % tail
            sw = cfg.get("paySwitch") or {}
            if not isinstance(sw, dict):
                sw = {}
            sw[tail] = (verb == "payon")
            cfg["paySwitch"] = sw
            with open(pay_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
            state = "参与转钱 ✅（每天 04:00 会带上它）" if verb == "payon" \
                else "暂停转钱 ⏸（名单保留；04:00 仍会登录一次但不转账）"
            return "✅ %s %s" % (tail, state)
        # payset：直接写插件目录的 pay.json（容器宿主两侧都可见）
        if not tail:
            r = await self.api("GET", "/pay-list", timeout=20)
            if r.get("ok"):
                return r.get("message", "")
            return "用法：!mcbot payset <收款人> <金额>"
        parts = tail.split()
        if len(parts) < 2:
            return "用法：!mcbot payset <收款人> <金额>\n例如：!mcbot payset sd_dt 100"
        pay_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pay.json")
        cfg = {}
        try:
            with open(pay_file, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["pay_to"] = parts[0]
        cfg["pay_amount"] = parts[1]
        cfg.setdefault("command", "/pay {to} {amount}")
        cfg.setdefault("notify_user", "")
        cfg.setdefault("accounts", [])
        cfg.setdefault("ports", {})
        with open(pay_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return "已设置收款人=%s 金额=%s ✅\n每天 04:00 自动执行；!mcbot paylist 可复查" % (parts[0], parts[1])

    async def _tts_speak(self, event, text):
        """把文字交给 TTS provider 合成，返回语音消息段"""
        # pkg157: AstrBot 的 edge-tts 适配层在 import 时把 User-Agent 改成了
        # astrbot/x.y.z，微软直接 403。这里必须显式写回浏览器 UA（pop 掉也会 403）。
        try:
            from edge_tts import constants as _etc
            _ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/%s Safari/537.36 Edg/%s" % (
                       _etc.CHROMIUM_FULL_VERSION,
                       _etc.CHROMIUM_FULL_VERSION))
            _etc.WSS_HEADERS["User-Agent"] = _ua
            if hasattr(_etc, "BASE_HEADERS"):
                _etc.BASE_HEADERS["User-Agent"] = _ua
        except Exception as e:
            logger.warning("[mcbot] 设置 edge-tts UA 失败: %s", e)
        prov = await self.context.get_using_tts_provider_async(
            event.unified_msg_origin
        )
        if not prov:
            return event.plain_result("语音服务未就绪：provider_tts_settings 里没有可用的 TTS 提供方")
        # pkg159: 日文/韩文/英文等按语种临时切音色，念完恢复默认
        _voice = _pick_tts_voice(text)
        _old_voice = getattr(prov, "voice", None)
        if _voice and _old_voice:
            prov.voice = _voice
        last = None
        try:
            for _ in range(2):
                try:
                    path = await prov.get_audio(text)
                    return event.chain_result([Record(file=path)])
                except Exception as e:
                    last = e
                    await asyncio.sleep(1)
            raise last
        finally:
            if _voice and _old_voice:
                prov.voice = _old_voice

    async def _handle_tts_command(self, event, text):
        """处理 /请输入文本 <文字>，返回结果或 None"""
        if not text:
            return event.plain_result(
                "用法：/请输入文本 要朗读的内容\n例：/请输入文本 今天也要开心哦"
            )
        if len(text) > 300:
            text = text[:300]
        try:
            logger.info("[mcbot] 语音合成：%s", text[:60])
            return await self._tts_speak(event, text)
        except Exception as e:
            logger.warning("[mcbot] 语音合成失败: %s", e)
            _msg = str(e)
            if "No audio was received" in _msg:
                return event.plain_result(
                    "语音合成失败：这段文字没有可朗读的内容（纯符号/表情/特殊字符）"
                )
            return event.plain_result("语音合成失败：%s" % e)

    async def dispatch(self, event: AstrMessageEvent):
        # 防撤回：先记录群消息（图片-only 的消息也会被记下），再走指令/聊天逻辑
        try:
            self._record_group_message(event)
        except Exception as e:
            logger.warning("[mcbot] 记录群消息失败: %s", e)

        # pkg125: 群聊一律静默，只保留私聊功能。记录完防撤回后立刻 stop_event，
        # 覆盖纯图片/空文本/群内指令等所有群消息，核心 LLM 和后续插件都不再处理。
        try:
            _gid = str(event.get_group_id() or "")
        except Exception:
            _gid = ""
        if _gid:
            # pkg134: !qqbot stop 时群里彻底静默（只保留防撤回记录），不发任何消息/表情
            _stopped = not self.llm_on
            try:
                _t = (event.message_str or "").strip()
            except Exception:
                _t = ""
            # pkg160: 1203912947 发的视频/转发视频一律不解析（群内静默）
            if self._is_video_share_from(event):
                logger.info("[mcbot] 忽略视频消息 群=%s uid=1203912947", _gid)
                self._stop(event)
                return
            # pkg138: 单独一个 "/"、"!"、空白等无内容消息一律静默，别触发 LLM
            if not _t.strip("/!／！ \t\r\n") and not self._has_media(event):
                self._stop(event)
                return
            try:
                _sid = str(event.get_sender_id() or "")
                self._pig_bot = self._pig_bot or event.bot
                if self._pig_subs:
                    self._start_pig_poll()
                if self._pig_subs:
                    logger.info("[mcbot][pigdbg] key=%s stopped=%s inlist=%s subs=%s",
                                self._pig_key(event), _stopped, _sid in PIG_BLACKLIST,
                                list(self._pig_subs.keys()))
                if (not _stopped) and _sid not in PIG_BLACKLIST \
                        and self._pig_key(event) in self._pig_subs:
                    mid = str(getattr(event.message_obj, "message_id", "") or "")
                    await self._react(event, mid, True)
            except Exception as e:
                logger.warning("[mcbot] 自动贴㊗️失败: %s", e)
            if _stopped:
                self._log_swallow(event)
                self._stop(event)
                return
            # pkg153: /请输入文本 <文字> —— 只把用户指定的文字合成语音
            _tts_text = _extract_tts_text(_t)
            if _tts_text is not None:
                try:
                    yield await self._handle_tts_command(event, _tts_text)
                except Exception as e:
                    logger.warning("[mcbot] 语音指令异常: %s", e)
                self._stop(event)
                return
            # pkg136: 今日老婆等指令放行给对应插件（不 stop_event，交给后续 handler）
            _first = _t.lstrip("/").split()[0] if _t else ""
            if _first in GROUP_PASS_CMDS:
                logger.info("[mcbot] 群指令放行：%s（交给第三方插件）", _first)
                return
            # pkg133: AstrBot 会吃掉开头的唤醒前缀 "/"，且 @昵称可能带空格，
            # 所以按「行首命令词 + 空白/结尾」判断，再兼顾命令在后、@在前的写法
            _m = re.match(r"^/?(猪列表|取消猪|猪)(?:\s|$)", _t)
            _word = _m.group(1) if _m else ""
            _is_list = (_word == "猪列表") or ("/猪列表" in _t)
            _is_cancel = (_word == "取消猪") or ("/取消猪" in _t)
            _is_pig = (_word == "猪") or ("/猪" in _t and not _is_list and not _is_cancel)
            if _is_list:
                try:
                    yield await self._pig_list(event)
                except Exception as e:
                    logger.warning("[mcbot] /猪列表 处理失败: %s", e)
                self._stop(event)
                return
            if _is_cancel:
                try:
                    yield await self._pig_unsubscribe(event)
                except Exception as e:
                    logger.warning("[mcbot] /取消猪 处理失败: %s", e)
                self._stop(event)
                return
            if _is_pig:
                try:
                    _r = await self._pig_subscribe(event)
                    if _r is not None:
                        yield _r
                except Exception as e:
                    logger.warning("[mcbot] /猪 处理失败: %s", e)
                self._stop(event)
                return
            # pkg137: 群聊功能开启时，普通消息放行给核心 LLM 聊天
            # （!qqbot stop 时上面的 _stopped 分支已吞掉，不会走到这里）
            return
        try:
            text = (event.message_str or "").strip()
        except Exception:
            return

        # pkg160: 1203912947 发的视频/转发视频一律不解析（私聊同样静默）
        if self._is_video_share_from(event):
            logger.info("[mcbot] 忽略视频消息 私聊 uid=1203912947")
            self._stop(event)
            return

        if not text:
            # pkg138: 私聊也忽略纯符号/空消息，避免空 prompt 触发回复
            if not self._has_media(event):
                self._stop(event)
            return

        # pkg153: /请输入文本 <文字>（私聊同样适用）
        _tts_text = _extract_tts_text(text)
        if _tts_text is not None:
            try:
                yield await self._handle_tts_command(event, _tts_text)
            except Exception as e:
                logger.warning("[mcbot] 语音指令异常: %s", e)
            self._stop(event)
            return

        # pkg175: 前缀容错。AstrBot 会把行首的唤醒前缀 "/" 吃掉，用户也可能只打
        #         「qqbot help」或用全角「！」，统一剥掉前缀再还原成 "!xxx" 规范写法，
        #         避免「/qqbot help」被当成普通聊天、LLM 又刚好限流时表现为「无响应」
        _norm = text.lstrip("/!！／＃# \t").strip()
        cmd_text = "!" + _norm
        low = cmd_text.lower()
        is_cmd = (low.startswith("!mcbot") or low.startswith("!qqbot")
                  or low.startswith("!admin") or low.startswith("!help")
                  or low.startswith("!ds") or low.startswith("!余额")
                  or low.startswith("!balance") or low.startswith("!deepseek"))

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
                if isinstance(reply, (list, tuple)):
                    for _seg in reply:
                        if _seg:
                            yield event.plain_result(_seg)
                else:
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
                _t = cmd_text.split(None, 2)
                _v = ""
                if len(_t) > 1:
                    _pp = _t[1].strip().split(None, 1)
                    _v = _pp[0].lower() if _pp else ""
                if _v in ACCOUNTS or _v in ("stop", "start", "set", "enable", "disable", "toggle",
                                            "addpay", "delpay", "paylist", "paytest", "payset",
                                            "payauth", "payon", "payoff"):
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
                reply = await self.handle_command(cmd_text, event)
            except Exception as e:
                logger.error("[mcbot] 指令执行异常: %s", e)
                reply = "❌ 内部错误：%s" % e
            self._stop(event)
            if reply:
                if isinstance(reply, (list, tuple)):
                    from astrbot.core.message.message_event_result import MessageChain
                    for _seg in reply:
                        if _seg is None:
                            continue
                        if isinstance(_seg, str):
                            if not _seg:
                                continue
                            yield event.plain_result(_seg)
                        else:
                            # pkg-help: 合并转发整条体积受 QQ 限制，超长会被拆成多条，
                            # 这里直接 send，保证每一条都真正发出（不依赖后续 respond 阶段）
                            await event.send(MessageChain([_seg]))
                else:
                    if isinstance(reply, str):
                        yield event.plain_result(reply)
                    else:
                        yield event.chain_result([reply])
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

        # 2) 非指令：AI 聊天关掉时全部吞掉（留痕，别静默——否则"bot 为什么不回话"极难查）
        # pkg127: llm_on 只管群聊；私聊的 LLM 对话始终保留，不再被吞
        if not self.llm_on and str(event.get_group_id() or ""):
            self._log_swallow(event)
            self._stop(event)
            return

    @staticmethod
    def _has_media(event) -> bool:
        """消息里是否有图片/语音/文件（纯文本为空的消息不该进 LLM）"""
        try:
            segs = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:
            return False
        for seg in segs:
            try:
                t = getattr(seg, "type", None)
                name = getattr(t, "name", str(t)).lower()
            except Exception:
                name = ""
            if name in ("image", "record", "file", "video"):
                return True
        return False

    # pkg160: 这个号发的视频/视频卡片不解析（不回复、不进 LLM、也不交给 parser 插件）
    NO_VIDEO_UIDS = ("1203912947",)

    @staticmethod
    def _has_video_share(event) -> bool:
        """消息里是否有视频、合并转发或视频分享卡片"""
        try:
            segs = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:
            return False
        for seg in segs:
            try:
                t = getattr(seg, "type", None)
                name = getattr(t, "name", str(t)).lower()
            except Exception:
                continue
            if name in ("video", "forward", "nodes"):
                return True
            if name == "json":
                try:
                    body = json.dumps(getattr(seg, "data", None), ensure_ascii=False).lower()
                except Exception:
                    body = str(getattr(seg, "data", "")).lower()
                if any(k in body for k in ("video", "视频", "structmsg", "multimsg")):
                    return True
        return False

    def _is_video_share_from(self, event) -> bool:
        try:
            uid = str(event.get_sender_id() or "")
        except Exception:
            uid = ""
        return uid in self.NO_VIDEO_UIDS and self._has_video_share(event)

    @staticmethod
    def _raw_text(event) -> str:
        """消息的原始文本（保留开头的 / ! 等唤醒前缀，message_str 已被剥掉）"""
        try:
            segs = getattr(getattr(event, "message_obj", None), "message", None) or []
            parts = []
            for seg in segs:
                t = getattr(seg, "type", None)
                name = getattr(t, "name", str(t)).lower()
                if name == "plain" or name == "text":
                    parts.append(str(getattr(seg, "text", "") or ""))
            if parts:
                return "".join(parts).strip()
        except Exception:
            pass
        try:
            return (event.message_str or "").strip()
        except Exception:
            return ""

    @filter.on_llm_request(priority=99999)
    async def _block_empty_llm(self, event: AstrMessageEvent, req) -> None:
        """pkg139: 单独一个 "/"、"!"、空白等消息会在唤醒前缀后变成空 prompt，
        这里在 LLM 请求阶段直接终止，避免空消息触发回复。"""
        try:
            _t = (event.message_str or "").strip()
        except Exception:
            _t = ""
        if not _t.strip("/!／！ \t\r\n") and not self._has_media(event):
            logger.info("[mcbot] 空消息/纯符号消息：已阻止 LLM 请求")
            event.stop_event()
            return
        # pkg140: 群里以 "/" 或 "!" 开头的消息是指令写法，没人处理就不要让 LLM 接话
        try:
            _in_group = bool(event.get_group_id())
        except Exception:
            _in_group = False
        _raw = self._raw_text(event)
        if _in_group and _raw[:1] in ("/", "／", "!", "！"):
            logger.info("[mcbot] 群内未知指令 %r：已阻止 LLM 请求", _raw[:24])
            event.stop_event()

    # ── pkg132: /猪 订阅式贴 ㊗️（带回执与列表）────────────
    def _pig_path(self):
        return os.path.join(PLUGIN_DIR, "pig_subs.json")

    def _load_pig_subs(self):
        try:
            with open(self._pig_path(), encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            raw = {}
        fixed = {}
        for _k, v in raw.items():
            try:
                g, u = str(v.get("group") or ""), str(v.get("user") or "")
                if g and u and u not in PIG_BLACKLIST:
                    fixed["%s:%s" % (g, u)] = v
            except Exception:
                pass
        self._pig_subs = fixed

    def _save_pig_subs(self):
        try:
            with open(self._pig_path(), "w", encoding="utf-8") as f:
                json.dump(self._pig_subs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[mcbot] 保存 /猪 订阅失败: %s", e)

    def _pig_key(self, event):
        return "%s:%s" % (event.get_group_id(), event.get_sender_id())

    async def _member_name(self, event, uid):
        try:
            res = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=int(event.get_group_id()), user_id=int(uid),
            )
            if isinstance(res, dict):
                return str(res.get("card") or res.get("nickname") or "")
        except Exception:
            pass
        return ""

    def _at_target(self, event):
        """取第一条不是 bot 自己的 @ 成员 QQ（raw dict 优先，组件用枚举名兜底）"""
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if isinstance(raw, dict):
                for seg in (raw.get("message") or []):
                    if isinstance(seg, dict) and seg.get("type") == "at":
                        q = str((seg.get("data") or {}).get("qq") or "")
                        if q and q != str(event.get_self_id()):
                            return q
        except Exception:
            pass
        try:
            for comp in (getattr(event.message_obj, "message", None) or []):
                tname = str(getattr(comp, "type", "") or "").lower()
                if "at" in tname and "atall" not in tname:
                    q = str(getattr(comp, "qq", "") or "")
                    if q and q != str(event.get_self_id()):
                        return q
        except Exception:
            pass
        return ""

    async def _latest_msg_id(self, event, uid):
        try:
            res = await event.bot.api.call_action(
                "get_group_msg_history", group_id=int(event.get_group_id()), count=20
            )
            for m in reversed((res or {}).get("messages") or []):
                if str((m.get("sender") or {}).get("user_id")) == str(uid):
                    return str(m.get("message_id") or "")
        except Exception as e:
            logger.warning("[mcbot] 查群历史失败: %s", e)
        return ""

    async def _react(self, event, mid, on=True):
        """给指定消息贴/取消 ㊗️（emoji_id=12951）"""
        if not mid:
            logger.warning("[mcbot] 贴㊗️没有目标消息")
            return
        try:
            mid2 = int(mid)
        except Exception:
            mid2 = mid
        try:
            fn = getattr(event.bot, "set_msg_emoji_like", None)
            if callable(fn):
                await fn(message_id=mid2, emoji_id="12951", set=on)
            else:
                await event.bot.api.call_action(
                    "set_msg_emoji_like", message_id=mid2, emoji_id="12951", set=on
                )
            logger.info("[mcbot] %s ㊗️ 群=%s 消息=%s", "贴上" if on else "取消", event.get_group_id(), mid)
        except Exception as e:
            logger.warning("[mcbot] 贴㊗️失败: %s", e)

    def _start_pig_poll(self):
        try:
            if self._pig_poll_task is None or self._pig_poll_task.done():
                self._pig_poll_task = asyncio.get_event_loop().create_task(self._pig_poll_loop())
        except Exception as e:
            logger.warning("[mcbot] 启动贴⛔轮询失败: %s", e)

    async def _pig_poll_loop(self):
        logger.info("[mcbot] 贴⛔轮询已启动")
        while True:
            await asyncio.sleep(4)
            try:
                if not self._pig_subs or self._pig_bot is None:
                    continue
                groups = sorted({k.split(":", 1)[0] for k in self._pig_subs})
                for gid in groups:
                    res = await self._pig_bot.api.call_action(
                        "get_group_msg_history", group_id=int(gid), count=20)
                    msgs = (res or {}).get("messages") or []
                    if gid not in self._pig_inited:
                        for m in msgs:
                            self._pig_reacted.add(str(m.get("message_id") or ""))
                        self._pig_inited.add(gid)
                        continue
                    for m in msgs:
                        uid = str((m.get("sender") or {}).get("user_id") or "")
                        if ("%s:%s" % (gid, uid)) not in self._pig_subs:
                            continue
                        mid = str(m.get("message_id") or "")
                        if not mid or mid in self._pig_reacted:
                            continue
                        self._pig_reacted.add(mid)
                        if len(self._pig_reacted) > 500:
                            self._pig_reacted = set(list(self._pig_reacted)[-200:])
                        try:
                            fn = getattr(self._pig_bot, "set_msg_emoji_like", None)
                            if callable(fn):
                                await fn(message_id=int(mid), emoji_id="12951", set=True)
                            else:
                                await self._pig_bot.api.call_action(
                                    "set_msg_emoji_like", message_id=int(mid),
                                    emoji_id="12951", set=True)
                            logger.info("[mcbot] 轮询贴上 群=%s 消息=%s", gid, mid)
                        except Exception as e:
                            logger.warning("[mcbot] 轮询贴失败: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[mcbot] 贴轮询异常: %s", e)

    async def _pig_subscribe(self, event):
        _sid = str(event.get_sender_id() or "")
        if _sid != PIG_OWNER:
            logger.info("[mcbot] /猪 拒绝：%s 不是授权使用者", _sid)
            return None
        uid = self._at_target(event)
        if not uid:
            return event.plain_result("用法：/猪 @群友名 —— 让 bot 一直给这个人贴㊗️\n/猪列表 查看本群正在被贴的人")
        if uid in PIG_BLACKLIST:
            logger.info("[mcbot] /猪 拒绝：%s 在黑名单", uid)
            return event.chain_result([At(qq=str(uid)), Plain(" 在黑名单里，不能贴㊗️。")])
        key = "%s:%s" % (event.get_group_id(), uid)
        name = await self._member_name(event, uid)
        label = ("%s(%s)" % (name, uid)) if name else str(uid)
        already = key in self._pig_subs
        self._pig_subs[key] = {
            "group": str(event.get_group_id()), "user": uid, "ts": int(time.time()),
        }
        self._pig_inited.discard(str(event.get_group_id()))
        self._start_pig_poll()
        self._save_pig_subs()
        mid = await self._latest_msg_id(event, uid)
        await self._react(event, mid or str(getattr(event.message_obj, "message_id", "") or ""), True)
        logger.info("[mcbot] /猪 订阅 %s，共 %d 个", key, len(self._pig_subs))
        chain = [At(qq=str(uid)), Plain(" 已%s贴㊗️，他今后在群里发言会自动贴上。\n发 /取消猪 @他 可停止，/猪列表 看全部。" % ("开始" if already else "开始"))]
        return event.chain_result(chain)

    async def _pig_unsubscribe(self, event):
        uid = self._at_target(event)
        if not uid:
            return event.plain_result("用法：/取消猪 @群友名")
        key = "%s:%s" % (event.get_group_id(), uid)
        name = await self._member_name(event, uid)
        label = ("%s(%s)" % (name, uid)) if name else str(uid)
        if key not in self._pig_subs:
            return event.chain_result([At(qq=str(uid)), Plain(" 当前没有被贴㊗️，无需取消。")])
        del self._pig_subs[key]
        self._save_pig_subs()
        mid = await self._latest_msg_id(event, uid)
        if mid:
            await self._react(event, mid, False)
        logger.info("[mcbot] /取消猪 已取消 %s，剩 %d 个", key, len(self._pig_subs))
        return event.chain_result([At(qq=str(uid)), Plain(" 已取消贴㊗️。")])

    async def _pig_list(self, event):
        gid = str(event.get_group_id())
        rows = [k for k in self._pig_subs if k.startswith(gid + ":")]
        if not rows:
            return event.plain_result("本群当前没有人被贴㊗️。\n用法：/猪 @群友名")
        lines = ["本群正在被贴㊗️（%d 人）：" % len(rows)]
        for k in rows:
            uid = k.split(":", 1)[1]
            name = await self._member_name(event, uid)
            lines.append("· %s%s" % (("%s " % name) if name else "", uid))
        lines.append("取消：/取消猪 @某人")
        return event.plain_result("\n".join(lines))

    # ── 防撤回：群消息环形缓冲 + 私聊回放 ────────────────
    def _record_group_message(self, event):
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if not gid:
            return

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        mid = ""
        ts = int(time.time())
        segs = None
        sender = {}
        if isinstance(raw, dict):
            mid = str(raw.get("message_id") or "")
            try:
                ts = int(raw.get("time") or ts)
            except Exception:
                pass
            if isinstance(raw.get("message"), list):
                segs = raw["message"]
            if isinstance(raw.get("sender"), dict):
                sender = raw["sender"]
        if segs is None:
            txt = (event.message_str or "").strip()
            segs = [{"type": "text", "data": {"text": txt or "[消息]"}}]

        try:
            uid = str(event.get_sender_id() or sender.get("user_id") or "")
        except Exception:
            uid = str(sender.get("user_id") or "")
        name = str(sender.get("card") or sender.get("nickname") or "")
        if not name:
            try:
                name = str(event.get_sender_name() or "")
            except Exception:
                name = ""
        name = name or uid or "未知"

        parts = []
        images = []
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text":
                parts.append(str(data.get("text") or ""))
            elif stype == "image":
                parts.append("[图片]")
                url = data.get("url") or data.get("file")
                if url and url not in images:
                    images.append(url)
            elif stype == "at":
                qq = str(data.get("qq") or "")
                parts.append("@全体成员" if qq == "all" else "@" + qq)
            elif stype in ("face", "mface"):
                parts.append("[表情]")
            elif stype == "record":
                parts.append("[语音]")
            elif stype == "video":
                parts.append("[视频]")
            elif stype == "file":
                parts.append("[文件%s]" % ((":" + str(data.get("name") or "")) if data.get("name") else ""))
            elif stype in ("json", "markdown"):
                parts.append("[卡片]")
            elif stype == "forward":
                parts.append("[合并转发]")
            elif stype == "reply":
                continue
            else:
                parts.append("[%s]" % stype)

        buf = self._recent_group_msgs.get(gid)
        if buf is None:
            buf = deque(maxlen=self._recent_limit)
            self._recent_group_msgs[gid] = buf
        if mid and buf and buf[-1].get("mid") == mid:
            return
        text = "".join(parts).strip() or ("[图片]" if images else "[消息]")
        buf.append({
            "mid": mid,
            "ts": ts,
            "uid": uid,
            "name": name,
            "text": text,
            "images": images[:9],
        })

    def _last_group_messages(self, event, arg):
        if event is None:
            return "该指令只能在私聊使用"
        if not arg:
            return ("用法：!qqbot last <群号>\n"
                    "例如：!qqbot last 123456789\n\n"
                    "每个群只在内存里保留最近 10 条消息；\n"
                    "消息被撤回后仍可回看（bot 重启后记录清空）。")
        if not re.fullmatch(r"\d{5,12}", arg):
            return ("❌ 群号格式不对：%s\n"
                    "只填纯数字群号（5-12 位），不用带 napcat:GroupMessage: 前缀。" % arg)
        buf = self._recent_group_msgs.get(arg)
        if not buf:
            return ("暂无群 %s 的消息记录\n"
                    "bot 只能记录自己收到过的群消息（最多 10 条，重启后清空）。" % arg)

        results = [Plain("=== 群 %s 最近 %d 条消息 ===\n" % (arg, len(buf)))]
        for i, m in enumerate(buf, 1):
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(m.get("ts") or time.time()))
            line = "%d. [%s] %s(%s): %s" % (
                i, when, m.get("name") or "未知", m.get("uid") or "?", m.get("text") or "[消息]"
            )
            imgs = m.get("images") or []
            if imgs:
                line += "  [图片: " + " , ".join(imgs[:9]) + "]"
            results.append(Plain(line + "\n"))
        results.append(Plain("—— 记录结束 ——"))
        logger.info("[mcbot] qqbot last %s -> %d 条", arg, len(buf))
        return event.chain_result(results)

    @staticmethod
    def _stop(event):
        try:
            event.stop_event()
        except Exception:
            pass

    def _log_swallow(self, event):
        """AI 聊天停用时会吞掉所有非指令消息，这里限流打 WARNING 留痕"""
        try:
            now = time.time()
            self._swallow_count += 1
            if self._last_swallow_log and now - self._last_swallow_log < SWALLOW_LOG_INTERVAL:
                return
            gap = (now - self._last_swallow_log) if self._last_swallow_log else 0
            self._last_swallow_log = now
            try:
                gid = event.get_group_id() or ""
            except Exception:
                gid = ""
            try:
                sender = event.get_sender_id()
            except Exception:
                sender = "?"
            logger.warning(
                "[mcbot] AI 聊天已停用，%s吞掉 %d 条非指令消息（群=%s 发送者=%s）。"
                "要恢复请私聊发送 !qqbot start",
                ("最近 %.0fs " % gap) if gap else "启动后",
                self._swallow_count, gid or "私聊", sender,
            )
            self._swallow_count = 0
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
