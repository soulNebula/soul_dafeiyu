# WechatMoments 适配微信 4.x —— 接口契约（Lead 维护，勿改）

> 目标：把 `WechatMoments`（原版只支持微信 3.9.x + pywxdump）适配到本机 **微信 4.1.15.56（进程 `Weixin.exe`，数据目录 `Documents\xwechat_files`）**，最终能导出朋友圈 HTML（含图片/视频/头像/评论点赞）。

## 0. 环境与铁律

- 统一用项目 venv 解释器：`C:\Users\Administrator\Desktop\wx_box\WechatMoments\.venv\Scripts\python.exe`（3.12，已装 `cryptography/psutil/zstandard/xmltodict/dataclasses_json/pillow/opencv` 等）。
- 只读微信数据：只允许 `OpenProcess/ReadProcessMemory` 只读取密钥 + 读 `xwechat_files` 下文件；**绝不写/删/改名微信目录**，不注入、不 Hook。
- `C:\Users\Administrator\Desktop\wx_box\recon\ref_db.py`、`wx_core\*` 是**只读参考实现**，不要去改它们（可复制代码到本项目）。
- 微信当前已登录：账号目录 `C:\Users\Administrator\Documents\xwechat_files\wxid_hsqe9w1lhl1j22_5663`，本人 `wxid_hsqe9w1lhl1j22`，昵称"瀑布"。
- 代码统一 UTF-8，路径用 `pathlib`，禁止硬编 `C:\Users\Administrator`（要能通过 `Path.home()` 推导）。
- 每人只改自己 write scope 里的文件；改公共文件先在群里说。

## 1. 已由 Lead 实测确认的事实（不要再重复踩坑）

| 事实 | 证据 |
|---|---|
| 4.x 数据库为 SQLCipher4；从 `Weixin.exe` 内存可提取 20/20 个库密钥 | `_dev/probe_db.py`：`keys OK: 20/20`，含 `sns\sns.db`、`contact\contact.db` |
| `recon/ref_db.py` 可直接用（需打桩 `wechatauto.logger`） | 同上；`WeChatDB()._open("sns/sns.db")` 返回已解密 sqlite 连接 |
| `sns.db` 4.x 表：`SnsTimeLine(tid,user_name,content,pack_info_buf)` 等 | `_dev/probe_sns.py` dump |
| `SnsTimeLine.content` 是 3.x 同款 XML `<SnsDataItem><TimelineObject>…`，**评论/点赞内嵌在 `<LocalExtraInfo>`**：`comment_user_list/user_comment`、`like_user_list`、`like_flag` | `_dev/probe_feed2.py` |
| 朋友圈图片缓存路径：`<账号目录>\cache\<YYYY-MM>\Sns\Img\<2位>\<30位>`（无扩展名，文件名=URL 的 md5）；图片密钥已在 `wx_box/config.json` 缓存（`image_aes_key=0669b78f42c9a7d7`, `image_xor_key=82`） | `find msg/attach cache` 实测 |
| 4.x 图片 .dat 解密算法见 `wx_box/wx_core/dat_decrypt.py`（V1/V2 格式） | 该模块已在 wx_box 生产使用 |
| 图片密钥提取见 `wx_box/wx_core/wechat_keys.py`（`global_config` 地标 + cfgDword/wxId 派生，带 .dat 自验证） | wx_box 已跑通并缓存 |

## 2. 模块契约（`WechatMoments/wx4/` 包）

所有对外函数失败时抛 `wx4.Wx4Error`（带中文可读信息），**不要把 ctypes/异常细节抛给 GUI**。

### 2.1 `wx4/db.py`（owner: db-core）

```python
class Wx4DB:
    def __init__(self, db_dir: str | None = None, account: str | None = None) -> None: ...
    account: str            # 账号目录名，如 "wxid_hsqe9w1lhl1j22_5663"
    account_dir: Path       # 该账号数据目录
    wxid: str               # 如 "wxid_hsqe9w1lhl1j22"
    def open(self, rel: str) -> sqlite3.Connection:
        """rel 形如 "sns/sns.db" / "contact/contact.db"（也接受反斜杠、大小写不敏感）。
        内部自动解密+合并 WAL+缓存，返回可读 sqlite3 连接。"""
    def self_info(self) -> dict:      # {"username","nick_name","remark"}
    def contacts(self) -> list[dict]: # 全量联系人
    def contact(self, username: str) -> dict | None
    def close(self) -> None

def get_db() -> Wx4DB:  # 进程级单例；WeChat 未登录/取不到密钥时抛 Wx4Error("请启动并登录微信 4.x")
def reset() -> None:    # 测试用：释放单例
```

联系人 dict 字段统一为：
```python
{"username": str, "nick_name": str, "remark": str, "alias": str,
 "head_img_md5": str,  # 头像缓存 md5（没有则 ""）
 "local_type": int}    # 微信 4.x contact.local_type，群聊等可据此过滤
```

### 2.2 `wx4/sns.py`（owner: sns-parser）

```python
@dataclass
class Moment:
    feed_id: int            # 4.x 用 TimelineObject/id 或 SnsTimeLine/tid（两处都要能拿到）
    username: str
    create_time: int        # 秒
    moment: MomentMsg       # 复用 entity/moment_msg.py 的 dataclass（含 ContentObject/Media 等）
    raw_xml: str
    comments: list[Comment] # 复用 entity/comment.py；comment_type: 2=评论(回复为 1?) 依实测填
    likes: list[str]        # 点赞人 username 列表
    def media_md5s(self) -> list[str]: ...  # 从 ContentObject.mediaList 提取图片/视频 md5

def list_moments(start_ts: int, end_ts: int) -> list[Moment]:  # 按时间倒序，闭区间
def all_moments() -> list[Moment]
def cover_url() -> str | None   # 对应 3.x SnsConfigV20 Key="6" 的封面，4.x 没有就返回 None
```

要求：
- XML → `entity.moment_msg.MomentMsg` 用 `xmltodict` + `dataclasses_json`（3.x 同一套，`force_list={'media'}`）。
- 解析 `LocalExtraInfo` 的评论点赞；`user_comment` 字段：`username/nickname/content/type/create_time`。
- 同一条 feed 可能出现在多次拉取里，按 `feed_id` 去重。
- 兼容 XML 里没有 `LocalExtraInfo` / `media` 缺失的情况，不得抛异常。

### 2.3 `wx4/media.py`（owner: media）—— ✅ 已交付，接口以下方**实测版**为准

```python
def image_path(md5: str, account_dir: Path | None = None, *, size=None, allow_thumb=False) -> Path | None
def image_path_for_media(media, moment=None, *, allow_thumb=False) -> Path | None   # 集成层用这个
def thumb_path(md5: str, ...) -> Path | None
def avatar_path(username: str) -> Path | None      # 读 head_image.db 的明文 BLOB，132x132
def video_path(media, account_dir: Path | None = None) -> Path | None
def cover_path(md5: str) -> Path | None
def recon_report() -> dict / def clear_cache() -> None
```

实测修正了原契约里的 3 个错误假设：
1. **Sns/Img 缓存文件名（30 位 hex）与 XML `url@md5`（32 位）没有可推导关系**（200+ 种方案全 0 命中，含 md5(URL)/md5(thumb)/属性组合/二进制扫描）。因此 `image_path` 改为**内容匹配**：XML 里有 `<size>`（宽高）→ 解密缓存文件后按**尺寸精确匹配**；找不到再按宽高比兜底缩略图（`allow_thumb=True`）。
2. **图片 XOR 密钥是十进制 82 = 0x52**，不是 0x82；AES 段要**去 PKCS#7 填充**。
3. **头像不走文件缓存**：`db_storage/head_image/head_image.db` 的 `head_image(username, md5, image_buffer,...)` 里 `image_buffer` 就是明文 JPEG/PNG。
4. **朋友圈视频**：4.x 边下边播、看完即删，本机 `msg/video/` 为空、`cache/*/Sns` 无 Video 目录 → `video_path()` 对历史视频基本返回 None（聊天/已保存视频仍可命中 `msg/video/<月>/<md5>.mp4`）。

**未完成（后续方向）**：朋友圈 CDN 图片/视频（`x-Enc: 1`）是在线 ISAAC-64 流加密，可靠方案是「XML `key` 属性 → ISAAC-64 密钥流 → XOR」+ `mmsns.qpic.cn` 下载（参见看雪文章与 `tools/reference_weflow/isaac64.ts`）。本机 4.1.15.56 实测用该算法尚未对上（可能版本算法/参数有变），故当前只做**本地缓存解密**，与原版行为一致（导出前需在微信里看过图片）。


要求：
- 图片解密：复制 `wx_box/wx_core/dat_decrypt.py` 的 V1/V2 算法到 `wx4/dat.py`（不 import wx_box）。
- 图片密钥：移植 `wx_box/wx_core/wechat_keys.py` 到 `wx4/image_key.py`，结果缓存到 `WechatMoments/.wx4_cache/image_key.json`；取不到密钥时允许用已知 `config.json` 值兜底。
- 头像：先 recon 4.x 头像缓存真实位置（`head_image.db` 里有 md5 字段；文件可能在 `cache/<月>/HeadImg/...`），实测解出 1 张图为准。
- 所有解密产物写到 `WechatMoments/.wx4_cache/`（自动建目录），不要污染微信目录。

### 2.4 集成层（owner: integrator）

保持 3.x 各模块**公开 API 不变**，只换实现，让 `gui/`、`exporter/` 尽量少改：

| 文件 | 原 3.x 职责 | 4.x 新实现 |
|---|---|---|
| `main.py` | `pywxdump.read_info` 轮询登录 | 用 `wx4.get_db()` + `self_info()`；未就绪时 GUI 显示"请启动并登录微信 4.x"，**不得闪退** |
| `decrypter/db_decrypt.py` | pywxdump 解密到 `app/Database/Msg/*.db` | 改为"准备 4.x 数据层"：定位账号/取密钥/预热 sns+contact 连接，回调进度 0-100 |
| `app/DataBase/sns.py` | `FeedsV20/CommentV20` | 用 `wx4.sns.list_moments` 返回与原接口同形数据 `(username, xml_content, feed_id)` 与评论 tuple |
| `app/DataBase/micro_msg.py` | `MicroMsg.db` 联系人 | 用 `wx4.db.contacts/contact`，保持原方法名与返回字段 |
| `decrypter/image_decrypt.py` | 扫 3.x FileStorage 解密 .dat | 用 `wx4.media.image_path(md5)` |
| `decrypter/video_decrypt.py` | 3.x 视频缓存 | 用 `wx4.media.video_path(...)` |
| `exporter/image_exporter.py` / `video_exporter.py` / `avatar_exporter.py` | 3.x 路径与后缀 | 改为调 `wx4.media`，输出 HTML 引用相对路径不变 |
| `exporter/html_exporter.py` | 生成 html | 尽量不改；确认 4.x 数据能喂进模板 |

**验收（端到端）**：`_dev/e2e_export.py` 无 GUI 地跑一遍 `start_ts..end_ts` 导出，产出 `output/朋友圈_*/index.html`，HTML 里引用的本地图片/头像文件真实存在且能解码（PIL 打开成功），评论点赞数量与 `wx4.sns` 统计一致。

## 3. 协作方式

- 共享任务板：先 `team_task_get` 自己的任务，`claim` 后动手，完成时 `complete`。遇到接口疑问先看本文件，仍不清就问 Lead。
- 提交物：代码 + 自测脚本（放 `_dev/`，命名 `test_*.py`）+ 一段"怎么跑、实测结果"的消息发给 Lead。
- 不要用 git（Lead 统一提交），不要改别人文件，不要动 `wx_box`。
