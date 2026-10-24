"""render.py —— 把 litematic 体素画成预览图

两种视角：
  iso  二轴测（等轴测）—— 有高度的结构
  top  俯视 1 格 1 色块 —— 很扁的投影（像素画、地面图）比二轴测清楚得多

不依赖 GPU / Minecraft 资源包：每个方块用调色板里的平均色 + 三面明暗模拟体积感。
"""

import json
import os

from PIL import Image, ImageChops, ImageDraw

_PALETTE = None
FALLBACK = (150, 150, 155)

# 画的时候要按这些后缀回退到"基础方块"的颜色
_SUFFIX = (
    '_slab', '_stairs', '_wall', '_fence_gate', '_fence', '_door', '_trapdoor',
    '_button', '_pressure_plate', '_carpet', '_pane', '_sign', '_banner', '_bed',
    '_head', '_foot', '_hanging_sign', '_wall_sign', '_wall_banner',
)

TOP_K, LEFT_K, RIGHT_K = 1.0, 0.78, 0.58

# 单个方块的像素边长上下限
T_MIN, T_MAX = 1.0, 28.0


def load_palette(path=None):
    global _PALETTE
    if _PALETTE is not None:
        return _PALETTE
    p = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'palette.json')
    try:
        with open(p, encoding='utf-8') as f:
            _PALETTE = json.load(f)
    except Exception:
        _PALETTE = {}
    return _PALETTE


def _hue_of(name):
    h = 0
    for ch in name:
        h = (h * 131 + ord(ch)) & 0xFFFFFF
    return (110 + h % 90, 110 + (h >> 8) % 90, 110 + (h >> 16) % 90)


def color_of(name, pal=None):
    pal = pal or load_palette()
    c = pal.get(name)
    if c:
        return tuple(c)
    n = name
    for suf in _SUFFIX:
        if n.endswith(suf):
            base = n[:-len(suf)]
            c = pal.get(base) or pal.get(base + 's') or pal.get(base + 'es')
            if c:
                return tuple(c)
            # stone_brick_slab -> stone_bricks
            if not base.endswith('s'):
                c = pal.get(base + 's')
                if c:
                    return tuple(c)
            break
    if n.endswith('s'):
        c = pal.get(n[:-1])
        if c:
            return tuple(c)
    return _hue_of(name)


def _shade(rgb, k):
    return (min(255, int(rgb[0] * k)), min(255, int(rgb[1] * k)), min(255, int(rgb[2] * k)))


def _fit(img, max_px):
    """兜底：万一还是超出 max_px，整体等比缩到以内"""
    w, h = img.size
    m = max(w, h)
    if m <= max_px or m <= 0:
        return img
    k = max_px / float(m)
    return img.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)


def _trim(img, bg, pad=6):
    """裁掉四周空白（几何算准时基本是空操作，留作保险）"""
    try:
        box = ImageChops.difference(img, Image.new('RGB', img.size, bg)).getbbox()
    except Exception:
        box = None
    if not box:
        return img
    x0, y0, x1, y1 = box
    w, h = img.size
    return img.crop((max(0, x0 - pad), max(0, y0 - pad),
                     min(w, x1 + pad), min(h, y1 + pad)))


def _finish(img, info, max_px, bg):
    img = _fit(_trim(img, bg), max_px)
    info['image'] = img.size
    return img, info


def render_iso(parsed, max_px=1400, bg=(22, 23, 28), palette=None):
    """二轴测：x 往右、z 往左、y 往上，三面明暗。返回 (PIL.Image, info)"""
    blocks = parsed.get('blocks') or {}
    if not blocks:
        raise ValueError('投影里没有方块')
    sx, sy, sz = parsed['size']
    ox, oy, oz = parsed.get('origin', (0, 0, 0))
    pal = palette or load_palette()

    vox = {}
    for (x, y, z), nm in blocks.items():
        vox[(x - ox, y - oy, z - oz)] = nm

    # 只画"表面"方块：6 个邻居都在体内就看不见
    surface = []
    for (x, y, z), nm in vox.items():
        if ((x + 1, y, z) in vox and (x - 1, y, z) in vox and
                (x, y + 1, z) in vox and (x, y - 1, z) in vox and
                (x, y, z + 1) in vox and (x, y, z - 1) in vox):
            continue
        surface.append((x, y, z, nm))
    if not surface:
        surface = [(x, y, z, nm) for (x, y, z), nm in vox.items()]

    # ── 按"实际可见方块"的投影范围反算格子边长 T，画面不浪费空白 ──
    # 方块中心的屏幕坐标（单位 = T）：U = x - z，V = (x + z)/2 - y
    # 一个方块占 U ± 1、V +0..+2
    umin = vmin = 10 ** 9
    umax = vmax = -10 ** 9
    for x, y, z, _nm in surface:
        cu = x - z
        cv = (x + z) * 0.5 - y
        if cu < umin: umin = cu
        if cu > umax: umax = cu
        if cv < vmin: vmin = cv
        if cv > vmax: vmax = cv

    w_units = (umax + 1) - (umin - 1)          # = umax - umin + 2
    h_units = (vmax + 2) - vmin                # = vmax - vmin + 2
    T = min((max_px - 6) / max(1.0, w_units), (max_px - 6) / max(1.0, h_units))
    T = max(T_MIN, min(T_MAX, T))

    # 2 倍超采样再降采样：既能消掉相邻多边形的 1px 接缝，也能把 T 很小时的锯齿抹平
    SS = 2
    TS = float(max(2 * SS, int(round(T * SS))))
    W = int(w_units * TS) + 4 * SS
    H = int(h_units * TS) + 6 * SS
    # 让 (umin - 1) 落在 x = 2*SS，(vmin) 落在 y = 2*SS
    off_x = 2 * SS - (umin - 1) * TS
    off_y = 2 * SS - vmin * TS
    half = TS / 2.0

    img = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(img)

    # 画家算法：x+y+z 越小越远，先画
    surface.sort(key=lambda b: b[0] + b[1] + b[2])

    for x, y, z, nm in surface:
        c = color_of(nm, pal)
        u = (x - z) * TS + off_x
        v = (x + z) * half - y * TS + off_y
        # 顶面
        d.polygon([(u, v), (u + TS, v + half), (u, v + TS), (u - TS, v + half)],
                  fill=_shade(c, TOP_K))
        # 左侧面（朝 -x）
        d.polygon([(u - TS, v + half), (u, v + TS), (u, v + 2 * TS), (u - TS, v + half * 3)],
                  fill=_shade(c, LEFT_K))
        # 右侧面（朝 +z）
        d.polygon([(u, v + TS), (u + TS, v + half), (u + TS, v + half * 3), (u, v + 2 * TS)],
                  fill=_shade(c, RIGHT_K))

    img = img.resize((max(1, W // SS), max(1, H // SS)), Image.LANCZOS)

    info = {
        'size': (sx, sy, sz),
        'blocks_total': len(vox),
        'blocks_drawn': len(surface),
        'truncated': bool(parsed.get('truncated')),
        'tile_px': round(T, 2),
        'image': img.size,
        'mode': 'iso',
    }
    return _finish(img, info, max_px, bg)


def render_top(parsed, max_px=1400, bg=(22, 23, 28), palette=None):
    """俯视图：每个方块一个色块（矮/扁的投影比二轴测清楚得多）"""
    blocks = parsed.get('blocks') or {}
    if not blocks:
        raise ValueError('投影里没有方块')
    sx, sy, sz = parsed['size']
    ox, oy, oz = parsed.get('origin', (0, 0, 0))
    pal = palette or load_palette()

    cells = {}
    for (x, y, z), nm in blocks.items():
        X, Y, Z = x - ox, y - oy, z - oz
        old = cells.get((X, Z))
        if old is None or Y > old[0]:   # 取每列最高的那块
            cells[(X, Z)] = (Y, nm)
    if not cells:
        raise ValueError('投影里没有方块')

    xs = [k[0] for k in cells]
    zs = [k[1] for k in cells]
    x0, x1 = min(xs), max(xs)
    z0, z1 = min(zs), max(zs)
    cw = x1 - x0 + 1
    ch = z1 - z0 + 1

    T = max(1, min(16, int((max_px - 4) / max(1, max(cw, ch)))))
    W = cw * T + 2
    H = ch * T + 2
    img = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(img)

    for (X, Z), (_y, nm) in cells.items():
        c = color_of(nm, pal)
        # z 越小越靠"上"（北），与游戏地图朝向一致
        px = (X - x0) * T + 1
        py = (Z - z0) * T + 1
        d.rectangle([px, py, px + T - 1, py + T - 1], fill=c)

    return _finish(img, {
        'size': (sx, sy, sz),
        'blocks_total': len(blocks),
        'blocks_drawn': len(cells),
        'truncated': bool(parsed.get('truncated')),
        'tile_px': T,
        'image': (W, H),
        'mode': 'top',
    }, max_px, bg)


def render(parsed, max_px=1400, bg=(22, 23, 28), palette=None, mode=None):
    """选视角：mode 可强制 'iso'/'top'；None = 很扁（≤2 格高）走俯视"""
    if mode is None:
        mode = 'top' if parsed['size'][1] <= 2 else 'iso'
    if mode == 'top':
        return render_top(parsed, max_px=max_px, bg=bg, palette=palette)
    return render_iso(parsed, max_px=max_px, bg=bg, palette=palette)
