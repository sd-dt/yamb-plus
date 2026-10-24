"""litematic.py —— 最小 NBT 解析 + Litematica 投影解码（无第三方依赖）

.litematic 是 gzip 压缩的 NBT。结构：
  Regions.<name>.{ Position{x,y,z}, Size{x,y,z}, BlockStatePalette[list],
                   BlockStates[long[]]  /* LitematicaBitArray 连续位打包 */ }
索引顺序：index = (y * sizeZ + z) * sizeX + x
"""

import gzip
import struct

TAG_END, TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG = 0, 1, 2, 3, 4
TAG_FLOAT, TAG_DOUBLE, TAG_BYTE_ARRAY, TAG_STRING = 5, 6, 7, 8
TAG_LIST, TAG_COMPOUND, TAG_INT_ARRAY, TAG_LONG_ARRAY = 9, 10, 11, 12


class _Reader:
    def __init__(self, data):
        self.d = data
        self.i = 0

    def u1(self):
        v = self.d[self.i]
        self.i += 1
        return v

    def take(self, n):
        v = self.d[self.i:self.i + n]
        self.i += n
        return v

    def num(self, fmt, n):
        v = struct.unpack_from(fmt, self.d, self.i)[0]
        self.i += n
        return v

    def string(self):
        n = self.num('>H', 2)
        return self.take(n).decode('utf-8', 'replace')

    def payload(self, tag):
        if tag == TAG_BYTE:
            return self.num('>b', 1)
        if tag == TAG_SHORT:
            return self.num('>h', 2)
        if tag == TAG_INT:
            return self.num('>i', 4)
        if tag == TAG_LONG:
            return self.num('>q', 8)
        if tag == TAG_FLOAT:
            return self.num('>f', 4)
        if tag == TAG_DOUBLE:
            return self.num('>d', 8)
        if tag == TAG_BYTE_ARRAY:
            n = self.num('>i', 4)
            return self.take(n)
        if tag == TAG_STRING:
            return self.string()
        if tag == TAG_LIST:
            sub = self.u1()
            n = self.num('>i', 4)
            return [self.payload(sub) for _ in range(n)]
        if tag == TAG_COMPOUND:
            out = {}
            while True:
                t = self.u1()
                if t == TAG_END:
                    return out
                name = self.string()
                out[name] = self.payload(t)
        if tag == TAG_INT_ARRAY:
            n = self.num('>i', 4)
            if n <= 0:
                return []
            v = list(struct.unpack_from('>%di' % n, self.d, self.i))
            self.i += 4 * n
            return v
        if tag == TAG_LONG_ARRAY:
            n = self.num('>i', 4)
            if n <= 0:
                return []
            v = list(struct.unpack_from('>%dq' % n, self.d, self.i))
            self.i += 8 * n
            return v
        raise ValueError('未知 NBT tag: %d' % tag)


def load_nbt(path):
    raw = gzip.open(path, 'rb').read()
    r = _Reader(raw)
    t = r.u1()
    if t != TAG_COMPOUND:
        raise ValueError('根标签不是 compound')
    r.string()          # 根名（通常为空）
    return r.payload(TAG_COMPOUND)


M64 = 0xFFFFFFFFFFFFFFFF


def _make_getter(longs, bits, msb=False, spanning=True):
    """造一个 index -> 调色板下标 的读取器。

    参考实现 koishi-plugin-litematic-renderer/src/index.ts 的 unpackState 与本机
    4 个真实投影实测一致：**连续打包 + LSB-first**（一个值可以跨两个 long）。
    spanning/msb 只留作兜底开关，正常都走默认值。
    """
    mask = (1 << bits) - 1
    n = len(longs)

    if spanning:
        def get(i):
            bi = i * bits
            sl = bi >> 6
            if sl >= n:
                return 0
            so = bi & 63
            a = longs[sl] & M64
            if msb:
                sh = 64 - so - bits
                if sh >= 0:
                    return (a >> sh) & mask
                b = longs[sl + 1] & M64 if sl + 1 < n else 0
                return (((a << (bits - 64 + so)) & mask) | (b >> (128 - so - bits))) & mask
            if so + bits <= 64:
                return (a >> so) & mask
            b = longs[sl + 1] & M64 if sl + 1 < n else 0
            return ((a >> so) | (b << (64 - so))) & mask
        return get

    vpl = 64 // bits

    def get(i):
        sl = i // vpl
        if sl >= n:
            return 0
        k = i - sl * vpl
        a = longs[sl] & M64
        off = (64 - bits * (k + 1)) if msb else (bits * k)
        return (a >> off) & mask
    return get


def _pick_packing(longs, bits, total):
    """按 BlockStates 长度定打包方式：连续打包需要 ceil(total*bits/64) 个 long，
    每 long 留空的 Minecraft BitStorage 需要 ceil(total/(64//bits)) 个。
    真实文件里两个值通常不相等，所以这个判据是确定性的。"""
    need_span = -(-total * bits // 64)
    need_ns = -(-total // max(1, 64 // bits))
    n = len(longs)
    if n != need_span and n == need_ns:
        return False, need_span, need_ns
    return True, need_span, need_ns


SKIP_BLOCKS = {
    'air', 'cave_air', 'void_air', 'structure_void',
    'barrier', 'light', 'moving_piston',
}


def _clean_name(n):
    return (n or '').split(':')[-1]


def _scan_region(get_idx, names, sx, sy, sz, ox, oy, oz, d, step, max_blocks):
    """扫一个 region，返回 (blocks, aabb, truncated)。

    先按 step=1 全量扫；只有方块数真的超过 max_blocks 才由调用方加大 step 重扫，
    这样"体积大但大部分是空气"的机器（投影最常见的形态）不会被无谓抽稀。
    """
    blocks = {}
    nn = len(names)
    minx = miny = minz = 10 ** 9
    maxx = maxy = maxz = -10 ** 9
    dx, dy, dz = d
    truncated = False
    for y in range(0, sy, step):
        base_y = y * sz
        for z in range(0, sz, step):
            base = (base_y + z) * sx
            for x in range(0, sx, step):
                idx = get_idx(base + x)
                if idx >= nn:
                    continue
                nm = names[idx]
                if nm in SKIP_BLOCKS:
                    continue
                if len(blocks) >= max_blocks:
                    truncated = True
                    break
                wx, wy, wz = ox + x * dx, oy + y * dy, oz + z * dz
                blocks[(wx, wy, wz)] = nm
                if wx < minx: minx = wx
                if wy < miny: miny = wy
                if wz < minz: minz = wz
                if wx > maxx: maxx = wx
                if wy > maxy: maxy = wy
                if wz > maxz: maxz = wz
            if truncated:
                break
        if truncated:
            break
    if not blocks:
        return blocks, (0, 0, 0, 0, 0, 0), truncated
    return blocks, (minx, miny, minz, maxx, maxy, maxz), truncated


def parse_litematic(path, max_blocks=800000):
    """返回 dict：blocks={(x,y,z): block_name}, size=(sx,sy,sz), meta{...}"""
    root = load_nbt(path)
    regions = root.get('Regions') or {}
    meta = root.get('Metadata') or {}

    blocks = {}
    bounds = [10 ** 9, 10 ** 9, 10 ** 9, -10 ** 9, -10 ** 9, -10 ** 9]
    truncated = False
    packings = []

    for rname, reg in regions.items():
        if not isinstance(reg, dict):
            continue
        pos = reg.get('Position') or {}
        size = reg.get('Size') or {}
        pal = reg.get('BlockStatePalette') or []
        longs = reg.get('BlockStates') or []
        names = []
        for entry in pal:
            if isinstance(entry, dict):
                names.append(_clean_name(entry.get('Name')))
            else:
                names.append('air')
        if not names:
            continue

        raw_sx = int(size.get('x', 0))
        raw_sy = int(size.get('y', 0))
        raw_sz = int(size.get('z', 0))
        sx, sy, sz = abs(raw_sx), abs(raw_sy), abs(raw_sz)
        if sx <= 0 or sy <= 0 or sz <= 0:
            continue
        ox = int(pos.get('x', 0))
        oy = int(pos.get('y', 0))
        oz = int(pos.get('z', 0))

        bits = max(2, (len(names) - 1).bit_length())
        total = sx * sy * sz
        spanning, need_span, need_ns = _pick_packing(longs, bits, total)
        get_idx = _make_getter(longs, bits, False, spanning)
        # 负的 Size 表示从 Position 往负方向延伸
        d = (-1 if raw_sx < 0 else 1, -1 if raw_sy < 0 else 1, -1 if raw_sz < 0 else 1)

        step = 1
        while True:
            got, box, trunc = _scan_region(get_idx, names, sx, sy, sz, ox, oy, oz,
                                           d, step, max_blocks)
            if not trunc or step >= 8:
                break
            step = 2 if step == 1 else step * 2
        packings.append({'region': rname, 'bits': bits, 'spanning': spanning,
                         'longs': len(longs), 'need_span': need_span, 'need_ns': need_ns,
                         'step': step})
        if trunc:
            truncated = True
        if got:
            blocks.update(got)
            for i, v in enumerate((box[0], box[1], box[2])):
                if v < bounds[i]:
                    bounds[i] = v
            for i, v in enumerate((box[3], box[4], box[5])):
                if v > bounds[i + 3]:
                    bounds[i + 3] = v

    if not blocks:
        return {'blocks': {}, 'size': (0, 0, 0), 'meta': meta, 'truncated': False,
                'origins': None, 'packings': packings}

    minx, miny, minz, maxx, maxy, maxz = bounds
    size = (maxx - minx + 1, maxy - miny + 1, maxz - minz + 1)
    return {
        'blocks': blocks,
        'size': size,
        'origin': (minx, miny, minz),
        'meta': meta,
        'truncated': truncated,
        'region_count': len(regions),
        'packings': packings,
    }
