"""astrbot_plugin_litematic —— 群里发 .litematic 投影，自动渲染成预览图

功能对标 https://github.com/halfkite/koishi-plugin-litematic-renderer
（那边是靠 Java + 官方资源包真渲染），这里改成纯 Python + Pillow：
不依赖 Java / GPU / 显卡，代价是画面为"方块平均色 + 三面明暗"的示意图风格。

用法：把 .litematic 直接发到群里（作为聊天附件，不是群文件），bot 回图。
"""

import asyncio
import os
import sys
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, register

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import litematic as lit  # noqa: E402
import render as rnd  # noqa: E402

PLUGIN_NAME = 'astrbot_plugin_litematic'
DEFAULTS = {
    'enabled_groups': [],        # 空 = 所有群都生效
    'max_file_mb': 30,
    'max_blocks': 300000,
    'image_max_px': 1400,
    'cooldown_seconds': 5,
}
CACHE_KEEP_SECONDS = 3600


@register(PLUGIN_NAME, 'yamb-plus', '把 .litematic 投影渲染成预览图', 'v1.0.0')
class LitematicRenderer(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.config = config
        self._lock = asyncio.Lock()
        self._last = {}
        self.work_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
        os.makedirs(self.work_dir, exist_ok=True)
        self._purge_cache()
        n = len(rnd.load_palette())
        logger.info('[litematic] 就绪，调色板 %d 种方块', n)

    # ── 配置读取（兼容 AstrBotConfig 与本地 dict）──
    def _cfg(self, key):
        v = None
        try:
            if self.config is not None:
                v = self.config.get(key)
        except Exception:
            v = None
        if v is None:
            v = DEFAULTS.get(key)
        return v

    def _group_ok(self, gid):
        wl = self._cfg('enabled_groups') or []
        if not wl:
            return True
        return str(gid) in {str(x) for x in wl}

    def _purge_cache(self):
        """清掉 1 小时前的旧图，避免 cache 目录无限长大"""
        try:
            now = time.time()
            for fn in os.listdir(self.work_dir):
                p = os.path.join(self.work_dir, fn)
                if fn.startswith('lit_') and fn.endswith('.png') and \
                        now - os.path.getmtime(p) > CACHE_KEEP_SECONDS:
                    os.remove(p)
        except Exception:
            pass

    # ── 主入口：监听所有消息，挑出 .litematic 附件 ──
    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_message(self, event: AstrMessageEvent):
        comps = getattr(event.message_obj, 'message', None) or []
        targets = []
        for c in comps:
            if isinstance(c, File):
                nm = (getattr(c, 'name', '') or '').lower()
                if nm.endswith('.litematic'):
                    targets.append(c)
        if not targets:
            return

        gid = ''
        try:
            gid = event.get_group_id() or ''
        except Exception:
            gid = ''
        if not self._group_ok(gid):
            return

        cd = int(self._cfg('cooldown_seconds') or 0)
        if cd > 0 and gid:
            now = time.time()
            if now - self._last.get(gid, 0) < cd:
                logger.info('[litematic] 群 %s 冷却中，跳过', gid)
                return
            self._last[gid] = now

        for f in targets:
            async for r in self._render_one(event, f):
                yield r

    async def _render_one(self, event: AstrMessageEvent, comp: File):
        name = getattr(comp, 'name', '') or 'projection.litematic'
        max_mb = int(self._cfg('max_file_mb') or 30)
        max_blocks = int(self._cfg('max_blocks') or 300000)
        max_px = int(self._cfg('image_max_px') or 1400)

        if self._lock.locked():
            yield event.plain_result('前面还有一个投影在渲染，稍等一下～')
            return

        await self._lock.acquire()
        src = None
        try:
            try:
                src = await comp.get_file()
            except Exception as e:
                logger.warning('[litematic] 取文件失败: %s', e)
                yield event.plain_result('文件下载失败，重发一次试试？')
                return
            if not src or not os.path.exists(src):
                yield event.plain_result('拿不到文件内容（QQ 可能没给下载地址）。')
                return

            size_mb = os.path.getsize(src) / 1048576.0
            if size_mb > max_mb:
                yield event.plain_result(
                    '投影太大：%.1f MB（上限 %d MB）' % (size_mb, max_mb))
                return

            logger.info('[litematic] 开始渲染 %s (%.2f MB)', name, size_mb)
            t0 = time.time()

            def _work():
                parsed = lit.parse_litematic(src, max_blocks=max_blocks)
                img, info = rnd.render(parsed, max_px=max_px)
                out_path = os.path.join(
                    self.work_dir, 'lit_%d_%d.png' % (int(time.time() * 1000), os.getpid()))
                img.save(out_path, 'PNG', optimize=True)
                info['meta'] = parsed.get('meta') or {}
                info['packings'] = parsed.get('packings') or []
                return out_path, info

            try:
                out, info = await asyncio.wait_for(asyncio.to_thread(_work), timeout=240)
            except asyncio.TimeoutError:
                yield event.plain_result('渲染超时（超过 4 分钟），这个投影太大了。')
                return
            except Exception as e:
                logger.exception('[litematic] 渲染失败')
                yield event.plain_result('渲染失败：%s' % str(e)[:120])
                return

            cost = time.time() - t0
            meta = info.get('meta') or {}
            sx, sy, sz = info['size']
            mode = '俯视' if info.get('mode') == 'top' else '二轴测'
            lines = ['📐 %s' % name]
            total_txt = str(info['blocks_total'])
            mt = meta.get('TotalBlocks')
            if isinstance(mt, int) and mt != info['blocks_total']:
                total_txt += '（投影自带统计 %d）' % mt
            lines.append('尺寸 %d × %d × %d ｜ 方块 %s（绘制 %d）'
                         % (sx, sy, sz, total_txt, info['blocks_drawn']))
            lines.append('视角 %s ｜ 耗时 %.1fs ｜ 出图 %d×%d'
                         % (mode, cost, info['image'][0], info['image'][1]))
            author = meta.get('Author')
            if author:
                lines.append('作者 %s' % author)
            if info['truncated']:
                lines.append('⚠ 方块数超过上限，已抽稀绘制')
            lines.append('（示意图：按方块平均色 + 三面明暗，非材质渲染）')

            logger.info('[litematic] 完成 %s -> %s  %.1fs %s',
                        name, out, cost, info.get('packings'))
            yield event.plain_result('\n'.join(lines))
            yield event.image_result(out)
            self._purge_cache()
        finally:
            try:
                if src and os.path.exists(src) and '/tmp' in src:
                    os.remove(src)
            except Exception:
                pass
            if self._lock.locked():
                self._lock.release()

    @filter.command('litematic')
    async def cmd_litematic(self, event: AstrMessageEvent):
        """litematic 渲染说明"""
        yield event.plain_result(
            '把 .litematic 投影文件当聊天附件发到群里（或私聊我），我会渲染成预览图。\n'
            '限制：单文件 ≤ %d MB、方块数 ≤ %d。\n'
            '风格：按方块平均色 + 三面明暗的示意图，不是材质渲染。\n'
            '注：发"群文件"不会触发，要像发图片那样当附件发。'
            % (int(self._cfg('max_file_mb') or 30), int(self._cfg('max_blocks') or 300000)))
