# 素材获取管线（P5-e：本地导入版）

> **联网部分仍是 stub：不联网、不搜索、不下载**。B 方案（本地导入）已实现：
> 把本机好图导入素材库，并可用一张图手动满足模型愿望。


## 接口

```python
from agi.media_acquisition import (
    search_wishes, list_wishes, try_acquire, add_to_library,
    import_local, import_local_file, fulfill_wish,
)

search_wishes() -> list[dict]
# 读 agi/data/media_wishes.jsonl，返回 [{ts, wish, context, source}, ...]

try_acquire(wish) -> dict
# 默认 {"ok": False, "status": "reserved", "reason": "acquisition_not_enabled"}
# 即使 media_acquisition_enabled=True 也只返回 manual_review_required；绝不发网络请求。

add_to_library(path, name, tags) -> dict
# 把人工确认的外部图片加入 media/stickers/；扩展名白名单 png/jpg/jpeg/gif/webp，
# 单文件 ≤10MB，不允许路径分隔符；成功后调用 emoji.index_assets 索引。

import_local(data: bytes, filename, name="", tags=None, source="local_import")
# 本地导入（B 方案）：内容嗅探 + 扩展名双校验；png/jpg/jpeg/gif/webp；≤2MB；
# SHA256 去重（重复返回 duplicate + 已有路径）；Pillow 验证后剥 EXIF 重编码，
# 落 agi/data/selflearn/media/<sha256>.<ext>；写 agi/data/media_sources.json；索引 emoji_archive。

import_local_file(path, **kwargs)
# 只允许白名单目录：agi/data/selflearn/media/、用户桌面 Desktop、仓库 media/；
# 其它路径返回 path_not_allowed（附 allowed_roots）。

list_wishes() -> list[dict]
# 愿望 + 状态（pending/fulfilled，旁路 media_wishes_status.json，原 jsonl 不动）。

fulfill_wish(wish_id, data=... | path=..., name="", tags=None) -> dict
# 导入一张图并把愿望标记 fulfilled；不删原记录；重复满足返回 already_fulfilled。
```

## 本地导入规则（B 方案）

- **格式**：只收 png/jpg/jpeg/gif/webp（按 Pillow 内容嗅探 + 扩展名双校验，拒绝 svg/html/bmp）
- **大小**：单文件 ≤2MB（空文件拒绝）
- **去重**：SHA256；同 hash 已在库 → `duplicate:true` + 已有路径，不重复落盘
- **隐私**：重新编码保存，**剥离 EXIF/元数据**（GIF 保留动画帧）
- **落盘**：`agi/data/selflearn/media/<sha256>.<ext>`；写 `agi/data/media_sources.json`
  （hash/name/filename/tags/source/imported_at/bytes/ext/path/indexed）；索引进 `emoji_archive`
- **路径导入白名单**：`agi/data/selflearn/media/`、`Desktop`、仓库 `media/`；其它路径拒绝
- **版权/来源自担**：用户只导入自己有权使用的图片；不抓取、不下载

## 后续实现条件（必须先满足）

1. **用户明确授权联网**（默认关闭；`media_acquisition_enabled=False`）；
2. **来源白名单**（只允许可信站点/APIs，记录来源 URL 与时间）；
3. **人工审核环节**（下载内容先进入待审核目录，人工确认后才能 `add_to_library`）；
4. **版权/合规**：不抓取受版权保护/未授权素材；保留来源与许可信息；
5. **微信可发送性**：最终只允许 png/jpg/jpeg/gif/webp、≤10MB；`.wxgf` 只能存不能发；
6. 任何下载失败/超时/内容不合法都不得影响聊天主链，且必须记审计日志。

## 流程（规划）

```
wish（模型说库里没有）→ media_wishes.jsonl
  → search（仅白名单来源，需授权）
  → 审核（人工/规则）
  → add_to_library（白名单+大小校验）
  → emoji.index_assets + media_tags.json 打标签
  → 下一次模型就能在素材清单里看到并选择
```

风险：版权、来源可信度、恶意文件、微信封控/合规；因此默认永远 reserved。
