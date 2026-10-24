# astrbot_plugin_litematic

群里发 `.litematic` 投影文件 → bot 自动渲染成预览图回给你。

对标 [koishi-plugin-litematic-renderer](https://github.com/halfkite/koishi-plugin-litematic-renderer)，
但那边是 Java + 官方资源包真渲染；这边是**纯 Python + Pillow**，不需要 JDK / GPU，
代价是画面为"方块平均色 + 三面明暗"的示意图风格，没有材质贴图。

## 用法

把 `.litematic` 当**聊天附件**发到群里（或私聊 bot）。注意：发"群文件"不会触发，
那是 `notice.group_upload`，不是消息段。指令 `/litematic` 可查看当前限制。

## 文件

| 文件 | 作用 |
|---|---|
| `main.py` | AstrBot 插件入口：监听 `File` 消息段 → 下载 → 渲染 → 回图 |
| `litematic.py` | 最小 NBT 解析 + Litematica 投影解码（无第三方依赖） |
| `render.py` | 二轴测 / 俯视渲染（Pillow 画多边形） |
| `palette.json` | 1165 种方块的平均色，由 1.21.11 客户端 jar 的贴图与方块模型离线算出 |
| `_conf_schema.json` | 插件配置（群白名单 / 大小上限 / 方块上限 / 出图边长 / 冷却） |

## 实现要点

- **`BlockStates` = 连续打包 + LSB-first**（bit0 是 long 的最低位，一个值可跨两个 long）。
  打包方式用 `len(BlockStates) == ceil(total*bits/64)` 判定；正确性用
  `解码出的非空方块数 == Metadata.TotalBlocks` 校验。
- `bits = max(2, ceil(log2(palette.length)))`；负 `Size` 表示从 `Position` 往负方向延伸。
- 先 `step=1` 全量扫，只有真超过 `max_blocks` 才加倍 step 重扫（投影通常"包围盒大、绝大部分是空气"）。
- 格子边长按**可见方块的投影范围**（`U=x-z`、`V=(x+z)/2-y`）反算，2× 超采样后
  LANCZOS 降采样，消掉相邻多边形的 1px 接缝。
- `size.y <= 2`（像素画 / 地面图）自动走俯视视角。

## 部署

```bash
cp -r plugin/astrbot_plugin_litematic /opt/qqbot/astrbot-data/plugins/
cd /opt/qqbot/napcat && docker compose restart astrbot
```

> 本仓库这份是脱敏版：`metadata.yaml` 的 `author` 与 `main.py` 里 `@register` 的作者字段
> 写的是 `yamb-plus`，线上部署时按自己的名字改回去即可。
