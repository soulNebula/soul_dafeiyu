# 原生贴纸 / 收藏表情 UI 自动化调研（P5-c）

> 结论先说：**系统表情（默认表情 tab）可以原生点选发送；用户收藏的贴纸气泡（自定义表情）无法可靠枚举/发送**。
> `media/stickers/*.wxgf` 是微信专有格式，只能经微信收藏面板渲染，**不能当文件/图片发成贴纸气泡**。
> 因此本任务交付 **预留接口**（默认安全关闭），P5-a 的"贴纸当图片"继续作为降级方案。

## 1. 环境与前置

| 项 | 值 |
| --- | --- |
| 微信 PC | `mmui::MainWindow`（当前账号昵称「小鱼」），窗口 1082×791 时聊天区可见 |
| wxauto4 | **41.1.7**（`agi/data/wechat/.venv`，Python 3.13） |
| wxauto4 能力 | `ChatBox` 只有 `send_msg / send_text / send_file / clear_edit / get_info` 等方法，**无表情/贴纸 API** |
| UI 自动化 | wxauto4 自带 bundled `uia`（Python-UIAutomation-for-Windows）；本调研只用其控制树读取 + 低频点击 |

> 调研全程只在 **「文件传输助手」** 会话操作，未向任何真实好友发消息；发了一条系统表情做端到端验证（文件传输助手可见）。

## 2. 控件树关键摘录

### 2.1 聊天输入区（ChatBox，`mmui::XSplitterView`）

```text
ButtonControl '发送表情(Alt+E)'   mmui::XButton       35×35   ← 打开表情面板
ButtonControl '发送收藏'           mmui::XButton       35×35
ButtonControl '发送文件'           mmui::XButton       35×35
ButtonControl '截图(Alt+A)'        mmui::XButton
ButtonControl '发送'               mmui::XOutlineButton
EditControl  '文件传输助手'        mmui::ChatInputField
ListControl  '消息'                mmui::RecyclerListView（ChatTextItemView 等）
```

### 2.2 表情面板：**独立顶层窗口** `mmui::XPopover`（Name=Weixin，642×650）

```text
WindowControl 'Weixin' mmui::XPopover
└─ GroupControl mmui::EmoticonContentView
   ├─ GroupControl mmui::EmoticonPageVirtualGridView   （默认/收藏 tab）
   │  └─ GroupControl mmui::EmoticonGridView
   │     └─ ListControl '消息'? （默认表情 tab：所有系统表情格）
   │        ├─ TextControl '微笑'  mmui::ExpressionCollectItem  55×55
   │        ├─ TextControl '撇嘴'  mmui::ExpressionCollectItem  55×55
   │        ├─ …（按 55×55 网格排列，Name=表情中文名）
   │        └─ TextControl '自定义表情' mmui::FavEmoticonItemView 110×110（收藏 tab）
   └─ TabControl mmui::XView
      ├─ TabItemControl '搜索表情'  mmui::EmoticonToolbarItem
      ├─ TabItemControl '默认表情'  mmui::EmoticonToolbarItem
      └─ TabItemControl '自定义表情' mmui::EmoticonToolbarItem
```

### 2.3 能拿到什么

| 对象 | 可访问性 | 结论 |
| --- | --- | --- |
| 系统表情（默认表情 tab） | **每个格子是 TextControl，Name=中文名**（微笑/撇嘴/色/…），矩形 55×55 | ✅ 可按名字定位点击 |
| 收藏表情（自定义表情 tab） | 只有 1 个 `FavEmoticonItemView`，Name=「自定义表情」，无每个贴纸的 Name/索引/图片路径 | ❌ 无法枚举/按名选择 |
| 面板 tab | TabItemControl（搜索表情/默认表情/自定义表情） | ✅ 可切换 |
| 表情图片路径 | UIA 不暴露；只有 `mmui::XImage` 空名控件 | ❌ 无法拿路径 |
| `.wxgf` 文件 | 微信专有格式，只由微信渲染；文件/图片发送只会变成文件或图片气泡 | ❌ 不能当贴纸 |

### 2.4 实测（仅文件传输助手）

1. 打开面板：UIA `Click()` `发送表情(Alt+E)` → 出现 `mmui::XPopover`；
2. 切到「默认表情」→ 格子里出现 `ExpressionCollectItem`（Name 可读）；
3. 点「微笑」→ 输入框出现占位对象字符 `U+FFFC`（不是文本 `[微笑]`）；
4. 点「发送」→ 微信消息列表最后一条 `SelfTextMessage text='[微笑]'`（微信自己渲染成系统表情）；
5. 切到「自定义表情」→ 面板只有「自定义表情」管理格，**没有可点的贴纸格子**。

## 3. 可行方案与风险

### 3.1 系统表情（可做，但价值有限）

- 流程：ChatInfo 核对 → 打开表情面板 → 点「默认表情」→ 按 Name 点格子 → 点发送 → 关闭面板；
- 风险：Robustness 依赖微信版本/主题；面板是独立顶层窗口，需先 click 表情按钮；误点会发出表情（可接受），但若窗口不在目标会话会发错人 → **必须 ChatInfo 二次核对**。

### 3.2 收藏贴纸（不可靠，不做）

- UIA 没有贴纸名/索引；只能按坐标点格子 → 换版本/换收藏顺序立即失效，且无法知道点中的是哪张；
- 可能误点「自定义表情」管理格 → 打开设置窗口，不会发送但会弹窗；
- 结论：**不实现自定义贴纸原生发送**；继续用 P5-a「贴纸当图片」降级。

### 3.3 `.wxgf`

- 专有格式；微信不接受"发送 wxgf 文件"变成贴纸；只能存在于收藏面板/表情商店；
- 结论：`.wxgf` 不能用于原生贴纸接口；若需要贴纸效果，只能走收藏面板（当前不可枚举）或图片降级。

## 4. 预留接口（`agi/wechat_gateway/sticker_native.py`）

- `available() -> bool`：默认 **False**（需显式开关 + backend 可用）；
- `send_native(contact, name) -> dict`：默认返回
  `{"ok": False, "status": "unsupported", "error": "sticker_native_disabled"}`，**不点任何 UI**；
- `list_collect_stickers() -> list[dict]`：默认 `[]`；
- `sticker_native_enabled = False` 常量 + 环境变量 `WX_STICKER_NATIVE=1` 才启用；
- 实验 backend 只在显式注入（fake/真 wxauto4）时使用，且要求 `ChatInfo()["chat_name"] == contact`，否则拒绝；
- 失败返回 `{"ok": False, "status": "failed", "fallback": "image"}`，由上层回退 P5-a。

## 5. 后续若要真正支持收藏贴纸

1. 让用户在微信里把贴纸加入「自定义表情」，并确认版本；
2. 用图像模板匹配/OCR 识别贴纸格（坐标失效风险高）；
3. 或改用微信内部的收藏表情存储文件 + 协议级发送（高风险、非 UI 自动化范畴）；
4. 推荐先保持图片降级，等用户明确要"贴纸气泡"再投入。

## 6. 调研边界

- 未改 `sender/proactive/chat`，未长期占用微信；
- 未向真实好友发送；
- 只做了只读控制树读取 + 文件传输助手内一次系统表情发送验证。
