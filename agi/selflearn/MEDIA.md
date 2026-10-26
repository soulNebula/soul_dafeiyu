# 媒体解析与安全上限（P5-b）

> **是否发媒体由大模型决定**：本地不做概率决策。模型可在回复末尾输出媒体指令；
> 本地只负责「告诉它有哪些素材 → 解析指令 → 安全上限 → 交给 P5-a 发送」。

## 1. 指令协议（给模型的说明原文）

```
可选媒体素材（按需使用，不是每条都要发；不想发就完全不输出这一段）：
- 你可以在回复末尾**单独一行**输出媒体指令（0~N 行）：
  [[media:sticker:名字]]  或  [[media:emoji:😄]]
- 表情包可以连发 2–3 个表达情绪，最多 3 个；emoji 也可以直接写在正文里。
- 心情差/严肃/负面话题不要发搞笑贴纸；不要每条都发；只能用下面清单里的名字。
- 如果清单里没有你想发的表情，输出 [[media:wish:想表达"无语但好笑"]]，
  本次不会发，会登记愿望给后续补素材。
素材：
666（好笑、赞）
亲亲（亲昵）
OK（同意）
...
```

- 素材清单最多 **40 条**（`media_material_limit`），来自 `emoji_archive` 的
  `wx_core/emoji_assets/` + `media/stickers/`，按 `name（tags）` 展示；
  哈希文件名的素材不进入给模型的清单。
- 关闭 `media_enabled` 时**整段不注入**，prompt 与改动前逐字节一致。

## 2. 解析规则

- `parse_media_directive(reply) -> (clean_reply, plan|None)`：
  - 支持 0~N 行、`sticker` / `emoji` / `wish` 三类；
  - **剥离所有** `[[media:...]]`（含无效格式），正文不留残渣；
  - 无指令 = 不发；多个 emoji 只取第一个；sticker 全部收集，上限在 build 阶段截断。
- `build_plan(path, directives, text, state, recent_media, allow_sticker)`：
  - sticker 最多 **3 个**（`media_max_stickers`）；超出的丢弃；
  - 贴纸按模型给出的顺序作为第二、三、四个气泡；emoji 加在正文句末（或直接写在正文里）；
  - 无效名字/不在素材库 → 丢弃；若是不在库的名字，额外记一条 wish。

## 3. 安全上限（本地只防事故）

| 上限 | 规则 |
| --- | --- |
| crisis | 禁一切 media（stickers/emoji/wish 都不发；不记 wish） |
| 连续媒体 | 最近连续 2 条已带 media → 本条**最多 1 个** sticker（emoji 也受 1 个总媒体上限） |
| 低情绪 | mood<0.35 且贴纸 tags 命中搞笑类（好笑/搞笑/恶搞/赞/嘲讽）→ 丢弃 |
| 同贴纸 7 天 | 按 `emoji_usage` 去重（`media_sticker_cooldown_days=7`） |
| 素材库 | 名字必须存在于 `emoji_archive`；否则拒绝/记 wish |
| 被动回复 | 默认 `media_sticker_in_reply=false` → 只允许 emoji，不允许 sticker |

连续媒体历史是进程内记录（`media_choice.record_media/recent_media`），重启清零。

## 4. wish（缺素材）

- 模型输出 `[[media:wish:...]]` → `record_wish()` 追加到
  `agi/data/media_wishes.jsonl`：`{ts, wish, context, source}`；**本次不发**。
- 获取管线：**B 方案本地导入已实现**（联网部分仍是 stub）——`agi/media_acquisition.py`
  - `list_wishes()` 读愿望 + pending/fulfilled 状态（旁路 `agi/data/media_wishes_status.json`，原记录不动）；
  - `import_local(data, filename, ...)` / `import_local_file(path, ...)`：本地图片导入素材库
    （白名单扩展名 + ≤2MB + SHA256 去重 + Pillow 校验 + 剥 EXIF + 索引）；
  - `fulfill_wish(wish_id, data|path, ...)`：用一张本地图手动满足愿望并标记 fulfilled；
  - `try_acquire(wish)` 仍默认返回 `reserved`，**绝不发网络请求**；
  - `add_to_library(path, name, tags)` 人工确认后加入 `media/stickers/`（白名单+大小校验）。
- 配置 `media_acquisition_enabled=False`；即使打开也需人工审核；完整管线见 task-63。

## 5. 素材与标签

- 资产目录：`wx_core/emoji_assets/`（110 个带名表情）、`media/stickers/`（png/gif）、
  `agi/data/selflearn/media/`（可选）；`emoji.index_assets` 幂等索引到 `emoji_archive`。
- 标签表：`agi/data/media_tags.json`（可手改）：
  ```json
  {"version":1,"tags":{"666":["好笑","赞"],"亲亲":["亲昵"],"OK":["同意"]}}
  ```
- 手动加素材：把图片放进上述目录 → 在 `media_tags.json` 加标签 → 下次索引/决策生效。

## 6. 配置

```json
{
  "media_enabled": false,
  "media_sticker_in_reply": false,
  "media_material_limit": 40,
  "media_max_stickers": 3,
  "media_sticker_cooldown_days": 7,
  "media_max_consecutive": 2,
  "media_tag_file": "agi/data/media_tags.json",
  "media_wish_file": "agi/data/selflearn/media_wishes.jsonl",
  "media_acquisition_enabled": false
}
```

## 7. 与 P5-a 的契约

`media.bubbles` 是完整顺序：`[{type:"text"/"emoji_text",text}, {type:"sticker"/"gif",name,path}...]`。
worker 已发文本后，把非文本项交给 `gateway.send_bubbles(contact, items, proactive=True)`；
`media_enabled=false` 时完全跳过，失败不影响已发文本。

## 8. P6-a 内容收口（真机 4 条问题）

- **正文元话题禁令**：media 开启时 prompt 明确禁止正文出现 `表情包/素材/清单/表情库/系统/指令/代码块/[[media`；想发只输出指令行。
- **文本—媒体一致性**：正文引用贴纸（"那个/这个表情/表情包"）但本条 media 为空 → 重生成 1 次；仍不一致 → 删除该句。反向：media 已发但正文在描述"我要发表情"→ 删描述句。
- **内心独白禁令**（P6-a 用户追加）：`心里…轻了/暖/凉/一沉`、`悬着的心放下`、`心头一暖`、`眼眶热/湿`、`百感交集`、`说不出滋味` 一律重生成；仍命中删整句，删空走 kind 兜底。
  - 正确替代：❌心里一下子轻了 → ✅终于弄完了，舒服；❌心里暖暖的 → ✅这话听着挺舒服；❌悬着的心放下了 → ✅总算放心了。
- **多气泡自足**：第一条必须有具体事由（"资料还没理完，有点烦" OK；"心里一下子轻了" 不 OK）；后续气泡只能承接第一条或用户上条真实消息，不用"那个/它/这事"指代没出现的东西；悬空指代删句。
- **情绪极性**：负面词 + `哈哈哈/笑死` 冲突 → 重生成 1 次，仍冲突去笑词。
- **观测**：tick 返回 `media{bubbles,wishes,reason,dropped}` 与 `text_media_check{issues,fixed,final_issues}`；worker `proactive_send`/`proactive_media` 日志带这些字段。
- 关闭 media 时整段 prompt 不注入、文本收口仍生效（保证无 media 时正文也不出现表情包相关词），prompt 基线锁保持绿。
