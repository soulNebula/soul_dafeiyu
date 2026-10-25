# 素材获取管线（P5-e 预留，stub）

> 当前**不联网、不下载**。只提供接口与安全默认；完整管线由 task-63（P5-e）实现。

## 接口

```python
from agi.media_acquisition import search_wishes, try_acquire, add_to_library

search_wishes() -> list[dict]
# 读 agi/data/media_wishes.jsonl，返回 [{ts, wish, context, source}, ...]

try_acquire(wish) -> dict
# 默认 {"ok": False, "status": "reserved", "reason": "acquisition_not_enabled"}
# 即使 media_acquisition_enabled=True 也只返回 manual_review_required；绝不发网络请求。

add_to_library(path, name, tags) -> dict
# 把人工确认的外部图片加入 media/stickers/；扩展名白名单 png/jpg/jpeg/gif/webp，
# 单文件 ≤10MB，不允许路径分隔符；成功后调用 emoji.index_assets 索引。
```

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
