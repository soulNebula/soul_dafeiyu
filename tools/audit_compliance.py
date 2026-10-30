"""wx_box 合规静态审计脚本。

用途
----
对项目内所有 ``*.py`` 源码做纯静态扫描，确认本项目停在约定的合规边界内：

1. 禁用 API / 库黑名单：向进程**写**内存、远程线程、全局 Hook、DLL 注入、
   调试转储、**回写/修改** SQLite 数据库等（命中即 FAIL）。
   用户显式授权的**只读**项（见下方白名单）不报 FAIL，但会在报告里如实列出：
   * ``wx_core/wechat_keys.py``、``wx_core/wcdb.py`` 的"一次性只读内存扫描"
     允许 ``OpenProcess`` / ``VirtualQueryEx`` / ``ReadProcessMemory``；
   * ``wx_core/wcdb.py``、``wx_core/db_source.py`` 允许读取 ``db_storage`` 下的
     微信数据库并解密到**本机临时目录**，用 ``sqlite3`` 的 ``mode=ro`` 只读连接；
     如果连接不是 ``mode=ro``、或文件里出现 SQL 写语句（INSERT/UPDATE/DELETE/
     DROP/CREATE/ALTER/ATTACH/VACUUM），照常 FAIL——**禁止回写微信数据库**。
   上述文件里出现写内存/注入/Hook/minidump 等 API 仍照常 FAIL。
2. 微信目录路径检查：源码里出现 ``WeChat Files`` / ``Tencent\\Weixin`` /
   ``xwechat_files`` 等微信数据或安装目录字符串时：
      - 出现在写入上下文（open(..., "w")、write_text、shutil.copy、
        unlink/rmtree、Image.save 等）   -> FAIL
      - 其余 -> WARN（普通文件）；对"用户授权的只读缓存/数据库模块"
        （见 ``ALLOWED_CACHE_READERS`` / ``ALLOWED_DB_READERS``）则视为合规。
   授权的只读模块仍会被写入上下文检查：只要它向微信目录写文件，同样判 FAIL。

合规边界（与 docs/风控与合规说明.md 一致）
------------------------------------------
允许（用户显式授权）：
  * 只读当前登录用户自己的微信本地缓存与数据库（``msg/file``、``msg/attach``、
    ``cache/.../Emoticon``、``db_storage/**/*.db``），只读、不写、不删、不改名；
  * SQLCipher/WCDB 数据库**只读解密**：密钥一次性只读内存扫描（缓存到 config）、
    解密结果只写本机临时目录、用 sqlite3 ``mode=ro`` 读取，读取 WAL 增量；
  * 对图片缓存 ``.dat``/表情包缓存做离线解密，``wxgf`` 用 imageio-ffmpeg 转 GIF；
  * 用微信自带"右键→复制"读剪贴板、窗口消息点击"另存为"（可选，默认关闭）；
  * 产出（原图/贴纸/文档/聊天记录）只写项目自己的 ``media/`` 与 ``log/``；
  * ``wx_core/emoji_assets/`` 是随包分发的表情素材 PNG 目录（非代码，不扫描）。
禁止：向进程写内存、注入、Hook、调试转储；**写回/修改微信数据库**（SQL 写操作、
      非 mode=ro 连接）；向微信目录写/删/改名；发送消息是受控能力（仅 Gateway._send）。

用法
----
    python tools/audit_compliance.py            # 中文报告
    python tools/audit_compliance.py --verbose  # 额外列出全部被扫描文件

退出码
------
    0 = PASS（无 FAIL，允许有 WARN）
    1 = FAIL（存在越界 API、DB 写操作或向微信目录写文件）

扫描范围
--------
项目根目录下所有 ``*.py``，但排除：
    recon/、media/、log/、.git/、venv/、.venv/、__pycache__/、
    build/、dist/、tests/、docs/（测试与文档）、
    历史第三方参考/归档残留（``_refs/``、``tools/reference_weflow/``、``Demo/``）：
    这些只在本地作参考、不参与运行，也不属于交付物；
    ``WechatMoments/`` 已随 8081 旧链路一并删除（本仓库不再存在），
    若本地仍有其副本，同样不参与扫描/合规判定；
    tools/audit_compliance.py 自身（其 pattern 表里必然包含禁用字样，属误报源）。

本脚本只用标准库，不 import 项目代码、不启动微信、不做任何进程/内存操作。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 扫描范围
EXCLUDE_DIRS = {
    ".git", "recon", "media", "log", "logs", "__pycache__",
    "venv", ".venv", "env", "build", "dist", ".idea", ".vscode",
    ".dsh-ops", ".mypy_cache", ".pytest_cache", "node_modules",
    # 历史第三方参考/归档：不是交付物、不参与运行，也不做合规判定。
    # WechatMoments/ 已随 8081 旧链路删除（不再列排除项；本地若残留副本同样不扫描）；
    # reference_weflow/_refs/Demo 仍是本地参考实现，继续排除。
    "reference_weflow", "_refs", "Demo", "demo",
    # tests 里会故意写 sqlite 临时库做夹具，不属于运行代码
    "tests", "docs",
    # 803 工作台（idea/）是独立子项目：自有 venv/git 历史、自有 SQLite 训练数据，
    # 不接触微信本地库；它的 INSERT/连接不是"回写微信数据库"，不进主仓合规判定。
    "idea",
}
# 扫描脚本自身：pattern 表中必然出现禁用字样，必须排除
SELF_PATH = Path(__file__).resolve()

# ------------------------- 用户授权的"只读微信本地缓存"模块白名单
# 这些文件允许：列目录/读文件/inode 时间/复制明文文档/读取 .dat 作为解密输入；
# 仍禁止写入微信目录，也禁止访问 SQLite 数据库。
ALLOWED_CACHE_READERS = (
    "wx_core/media_cache.py",
    "wx_core/dat_decrypt.py",
    "wx_core/emoticon.py",
)
# 授权的辅助模块（只展示，供报告说明；不涉及禁用 API，无需额外豁免）
ALLOWED_HELPER_MODULES = (
    "wx_core/clipboard.py",
    "wx_core/textlog.py",
    "wx_core/media_export.py",
    "wx_core/builtin_emoji.py",
)
# ---- 用户显式授权、一次性的"只读密钥提取"白名单 ----
# 这些文件允许 OpenProcess / VirtualQueryEx / ReadProcessMemory（只读扫描内存取密钥）；
# 写/注入/Hook/minidump 等 pattern 不豁免，照常 FAIL。
# 默认开启（config: auto_extract_image_key=true / db_keys 自动扫描），
# 成功后密钥缓存到 config.json，之后不再读内存。
ALLOWED_MEMORY_READERS = (
    "wx_core/wechat_keys.py",
    "wx_core/wcdb.py",
    # 仅用 OpenProcess(QUERY_LIMITED_INFORMATION) 判断 pid 存活，不读进程内存
    "tools/desktop/launcher.py",
)
MEMORY_READ_EXEMPT_PATTERNS = frozenset({
    r"\bOpenProcess\b",
    r"\bReadProcessMemory\b",
    r"\bctypes\.windll\.kernel32\.OpenProcess\b",
})
# ---- 用户显式授权的"数据库只读直读"白名单 ----
# 这些文件允许：引用 db_storage 路径、读微信 .db（只读）、解密到本机临时目录、
# sqlite3 以 mode=ro 只读连接。
# 仍会 FAIL：sqlite3 连接不是 mode=ro、SQL 写语句（回写数据库）、向微信目录写文件。
ALLOWED_DB_READERS = (
    "wx_core/wcdb.py",
    "wx_core/db_source.py",
    "webui/contacts.py",            # 网页端联系人筛选（只读 contact/session 库，mode=ro）
    "webui/groups.py",              # 网页端群列表（只读 contact.db，mode=ro）
    "webui/history.py",             # 网页端历史记录（只读 message 库，mode=ro）
    "webui/account.py",             # 只 stat key_info.db(-wal/-shm) 的 mtime 判断账号，不打开库
)
# 数据库路径字符串：若只出现在 `#` 注释里（例如"不读 db_storage"的说明），
# 不视为违规；授权 DB 模块允许只读引用；真正在代码里引用/打开数据库仍会 FAIL。

DB_PATH_PATTERNS = frozenset({
    r"\bdb_storage\b",
    r"(?i)\.db-wal\b",
    r"\bMicroMsg\.db\b",
    r"\bMsgMyMsg\b",
    r"\bkey\.dat\b",
})
# ---- `agi/`：独立 companion 系统（自己的 SQLite，不接触微信库）----
# agi 用 `agi/data/*.sqlite3` 存记忆/草稿/用量/工具日志，属于本项目**有意写入**的数据，
# 与"回写微信数据库"是两回事。这里只豁免 DB 相关 pattern；内存/Hook/注入仍照常 FAIL。
COMPANION_DB_DIRS = ("agi/", "tools/")
#: 网页端读 companion 库的页面（只读 mode=ro，与 agi/ 同属自建 SQLite）
COMPANION_DB_FILES = ("webui/agi.py",)
COMPANION_DB_EXEMPT_PATTERNS = frozenset({
    r"\bsqlite3\.connect\b",
    r"(?i)\b(?:insert\s+into|update\s+\w+\s+set|delete\s+from|drop\s+table|create\s+table|alter\s+table|attach\s+database|vacuum)\b",
    r"(?i)\.db-wal\b",
})


def is_allowed_cache_reader(rel_path: str) -> bool:
    """该文件是否属于"用户授权的只读微信缓存"白名单。"""
    return rel_path in ALLOWED_CACHE_READERS


def is_allowed_memory_reader(rel_path: str) -> bool:
    """该文件是否属于"用户授权、一次性只读密钥提取"白名单。"""
    return rel_path in ALLOWED_MEMORY_READERS


def is_allowed_db_reader(rel_path: str) -> bool:
    """该文件是否属于"用户授权的数据库只读直读"白名单。"""
    return rel_path in ALLOWED_DB_READERS


def is_companion_db(rel_path: str) -> bool:
    """``agi/`` 及其网页端只读页面：用自己的 companion SQLite，不是微信库。"""
    normalized = str(rel_path).replace("\\", "/")
    return normalized.startswith(COMPANION_DB_DIRS) or normalized in COMPANION_DB_FILES



def _is_comment_only(line: str) -> bool:
    """该行是否只是 `#` 注释（用于豁免注释里的数据库路径说明）。"""
    stripped = line.strip()
    return stripped.startswith("#")


# ------------------------------------------------- 一、禁用 API / 库黑名单
# (正则, 人类可读说明)。全部为 FAIL 级。
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    # ---- Windows 内存读写 / 远程线程 / 注入 ----
    (r"\bReadProcessMemory\b", "读取其他进程内存（ReadProcessMemory）"),
    (r"\bWriteProcessMemory\b", "写入其他进程内存（WriteProcessMemory）"),
    (r"\bNtReadVirtualMemory\b", "读取其他进程内存（NtReadVirtualMemory）"),
    (r"\bNtWriteVirtualMemory\b", "写入其他进程内存（NtWriteVirtualMemory）"),
    (r"\bVirtualAllocEx\b", "在目标进程内分配内存（VirtualAllocEx）"),
    (r"\bVirtualProtectEx\b", "修改目标进程内存保护（VirtualProtectEx）"),
    (r"\bOpenProcess\b", "打开微信进程句柄（OpenProcess）"),
    (r"\bCreateRemoteThread\b", "远程线程注入（CreateRemoteThread）"),
    (r"\bSetWindowsHookEx\w*\b", "全局 Hook / 注入 DLL（SetWindowsHookEx）"),
    (r"\bQueueUserAPC\b", "APC 注入（QueueUserAPC）"),
    (r"\bWriteProcessMemory\w*\b", "写入其他进程内存"),
    # ---- 调试 / 转储 / 反调试 ----
    (r"\bDebugActiveProcess\b", "附加调试微信进程（DebugActiveProcess）"),
    (r"\bMiniDumpWriteDump\b", "转储微信进程内存（MiniDumpWriteDump）"),
    (r"\bminidump\b", "进程内存转储（minidump）"),
    # ---- 第三方逆向 / 注入 / 内存库 ----
    (r"\bpymem\b", "第三方内存读写库 pymem"),
    (r"\bfrida\b", "动态插桩框架 frida"),
    (r"(?i)(?:dll|code|shellcode|process)[\s_-]*inject(?:ion|ed|s)?|inject(?:ion|ed|s)?\s+(?:dll|code|shellcode)", "DLL / 代码注入（inject）"),
    (r"\bmanualmap\b", "手动映射 DLL（manualmap）"),
    (r"\bctypes\.windll\.kernel32\.OpenProcess\b", "打开进程句柄"),
    # ---- 微信数据库 / 解密 ----
    (r"\bsqlcipher\b", "SQLCipher 解密微信数据库"),
    (r"\bsqlite3\.connect\b", "打开 SQLite 数据库连接（sqlite3.connect）"),
    (r"(?i)\b(?:insert\s+into|update\s+\w+\s+set|delete\s+from|drop\s+table|create\s+table|alter\s+table|attach\s+database|vacuum)\b",
     "SQL 写操作（禁止回写/修改数据库）"),
    (r"\bdecrypt\w*\s*\(\s*[^)]*(?:wechat|weixin|db)", "微信数据库解密函数"),
    (r"(?:wechat|weixin)\w*\s*\.\s*decrypt\b", "微信数据解密"),
    (r"\bdb_storage\b", "微信数据库目录 db_storage（禁止读取/解密）"),
    (r"(?i)\.db-wal\b", "微信数据库 WAL 文件（禁止读取）"),
    (r"\bMicroMsg\.db\b", "微信消息数据库文件 MicroMsg.db（禁止读取/解密）"),
    (r"\bMsgMyMsg\b", "微信消息表 MsgMyMsg（禁止访问）"),
    (r"\bkey\.dat\b", "微信数据库密钥 key.dat（禁止读取）"),
    # ---- 通用越界手段（中文注释/文档字符串） ----
    (r"解密微信数据库", "提及微信数据库解密"),
    (r"破解微信数据库", "提及破解微信数据库"),
]

# ------------------------------------- 二、微信目录字符串（数据目录 / 安装目录）
# 命中后按上下文判断：写入 -> FAIL；否则 -> WARN（本工具不做任何读写）
WECHAT_PATH_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)WeChat[\s_\\/]*Files", "微信数据目录 WeChat Files"),
    (r"(?i)xwechat[_\-]?files", "微信 4.x 数据目录 xwechat_files"),
    (r"(?i)Tencent[\s_\\/]*(?:Weixin|WeChat)", "腾讯微信安装目录 Tencent\\Weixin"),
    (r"(?i)Program[\s_]*Files(?:\s*\(x86\))?[\s_\\/]*Tencent", "Program Files\\Tencent 安装目录"),
    (r"(?i)Weixin(?:\.exe)?[\s_\\/]*(?:FileStorage|Msg)", "微信安装目录内的数据"),
    (r"(?i)FileStorage[\s_\\/]*(?:Image|Video|File|MsgAttach)", "微信聊天文件存储子目录"),
]

# 写入上下文关键字（用于区分"读"与"写"）。命中 WECHAT_PATH 的行附近出现
# 这些写法，即判定为向微信目录写文件 -> FAIL。
WRITE_HINTS_RE = re.compile(
    r"""(?ix)
    open\s*\([^)]*['"][^'"]*[wax+][^'"]*['"]      # open(..., "w"/"a"/"x"/"r+")
    |\.write_text\s*\(
    |\.write_bytes\s*\(
    |\.mkdir\s*\(
    |\.touch\s*\(
    |\.unlink\s*\(
    |\.rename\s*\(\s*[^,)]
    |\.replace\s*\(
    |\.save\s*\(                                # Image.save / np.save
    |shutil\.(?:copy\w*|move|rmtree|copytree)
    |os\.(?:remove|unlink|rename|replace|rmdir|removedirs|makedirs|mkdir)
    |Path\s*\([^)]*\)\s*\.\s*(?:write|mkdir|unlink)
    |rmtree\s*\(
    |send2trash
    """,
)

# 同一"语句"最多向后看几行，用于定位写入上下文
WRITE_CONTEXT_LINES = 3


# ---------------------------------------------------------------- 工具函数
def setup_console() -> None:
    """保证中文报告在 Windows 控制台/管道中都不乱码。

    做法：控制台场景先把输出代码页切到 UTF-8，再把 stdout/stderr 重配为
    UTF-8；管道/重定向场景只重配 Python 侧编码。全部 try/except 兜底，
    任何一步失败都不影响审计结果。
    """
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        if sys.stdout is not None and sys.stdout.isatty():
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def iter_py_files(root: Path) -> list[Path]:
    """遍历项目内所有 .py，按路径排序，排除无关目录与自身。"""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDE_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            p = (Path(dirpath) / name).resolve()
            if p == SELF_PATH or p.name == "_p02_patch.py":
                continue
            try:
                if p.relative_to(root).as_posix().startswith("docs/sources/"):
                    continue
            except ValueError:
                pass
            out.append(p)
    return out


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def read_lines(path: Path) -> list[str]:
    """按 UTF-8 读源码；解码失败用 GBK 兜底，仍失败则忽略。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").splitlines()


def strip_code_only(line: str) -> str:
    """极简噪声处理：保留原行用于报告，匹配时同时看原行即可。

    （不做完整语法解析，避免误删字符串中的危险 API 名称。
    注释里的禁用词也会命中——这是有意的保守策略。）
    """
    return line


# ---------------------------------------------------------------- 扫描逻辑
class Finding:
    def __init__(self, level: str, path: str, lineno: int, message: str, text: str) -> None:
        self.level = level          # "FAIL" | "WARN"
        self.path = path
        self.lineno = lineno
        self.message = message
        self.text = text.strip()

    def render(self) -> str:
        return f"  [{self.level}] {self.path}:{self.lineno}  {self.message}\n           > {self.text}"


#: 被"授权只读密钥提取 / 数据库只读直读"白名单豁免的调用记录（报告中如实列出）
EXEMPT_MEMORY_HITS: list[tuple[str, int, str]] = []
EXEMPT_DB_HITS: list[tuple[str, int, str]] = []
EXEMPT_COMPANION_HITS: list[tuple[str, int, str]] = []


def scan_forbidden_api(files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        rel_path = rel(path)
        allowed_memory = is_allowed_memory_reader(rel_path)
        allowed_db = is_allowed_db_reader(rel_path)
        allowed_companion = is_companion_db(rel_path)
        lines = read_lines(path)
        for i, line in enumerate(lines, 1):
            code = strip_code_only(line)
            for pattern, label in FORBIDDEN_PATTERNS:
                if not re.search(pattern, code):
                    continue
                # 数据库路径只出现在 `#` 注释里（"不读 db_storage"之类的说明）不算违规
                if pattern in DB_PATH_PATTERNS and _is_comment_only(line):
                    continue
                # agi 的 companion SQLite（自己的库，不是微信库）：只豁免 DB 相关 pattern
                if allowed_companion and pattern in COMPANION_DB_EXEMPT_PATTERNS:
                    EXEMPT_COMPANION_HITS.append(
                        (rel_path, i, "agi 自己的 SQLite（记忆/草稿/用量），不接触微信库"))
                    continue
                # 用户授权、一次性的只读密钥提取：仅豁免"读"，写/注入照常 FAIL
                if allowed_memory and pattern in MEMORY_READ_EXEMPT_PATTERNS:
                    EXEMPT_MEMORY_HITS.append((rel_path, i, label))
                    continue
                # 用户授权的数据库只读直读：允许只读连接 mode=ro 与 db_storage 引用；
                # 连接不是 mode=ro 仍然 FAIL（防止回写数据库）。
                if allowed_db and pattern == r"\bsqlite3\.connect\b":
                    if "mode=ro" in line:
                        EXEMPT_DB_HITS.append((rel_path, i, "只读连接 sqlite3 mode=ro"))
                        break
                    findings.append(Finding(
                        "FAIL", rel_path, i,
                        "数据库连接必须使用只读模式 mode=ro（禁止回写微信数据库）", line))
                    break
                if allowed_db and pattern in DB_PATH_PATTERNS:
                    EXEMPT_DB_HITS.append((rel_path, i, "只读引用微信数据库路径（db_storage 等，不写回）"))
                    continue
                findings.append(Finding("FAIL", rel_path, i, label, line))
                break  # 一行只报一个禁用项，避免刷屏
    return findings


def _write_context(lines: list[str], idx: int, stmt_window: int = WRITE_CONTEXT_LINES) -> bool:
    """判断第 idx 行（0 基）是否是/临近"写文件"语句。

    策略：取该行起 stmt_window+1 行（遇到空行或明显新语句可提前止），
    只要其中出现 WRITE_HINTS_RE 即视为写入上下文。
    """
    end = min(len(lines), idx + stmt_window + 1)
    chunk = "\n".join(lines[idx:end])
    return bool(WRITE_HINTS_RE.search(chunk))


def scan_wechat_paths(files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        rel_path = rel(path)
        allowed_reader = is_allowed_cache_reader(rel_path)
        lines = read_lines(path)
        for i, line in enumerate(lines):
            for pattern, label in WECHAT_PATH_PATTERNS:
                if not re.search(pattern, line):
                    continue
                if _write_context(lines, i):
                    findings.append(Finding(
                        "FAIL", rel_path, i + 1,
                        f"向微信目录写文件：{label}", line))
                elif allowed_reader:
                    # 用户显式授权的只读缓存模块：读取/列目录属于许可范围，不报 WARN
                    pass
                else:
                    findings.append(Finding(
                        "WARN", rel_path, i + 1,
                        f"出现微信目录字符串（读取/引用，请人工确认只读）：{label}", line))
                break  # 同一行只报一次
    return findings


# ---------------------------------------------------------------- 发送路径（P0-1）
#: 真实发送路径/入口（静态扫描；不执行、不联网）
SEND_PATTERNS = (
    (r"\bWxAutoSender\b", "WxAutoSender 发送适配器"),
    (r"\bsend_text\s*\(", "send_text() 发送文本"),
    (r"\bsend_msg\s*\(", "send_msg() 发送消息"),
    (r"\bSendMsg\s*\(", "SendMsg() 发送消息"),
    (r"\bsend_cli\b", "send_cli 发送子进程入口"),
    (r"\b_send\s*\(", "_send() 发送路径"),
)
#: 已知受控入口：Gateway._send 带 STOP/白名单/静默/策略；sender/send_cli 是
#: 只允许由网关调用的底层原语（本身不带完整闸门，静态审计无法证明调用链）。
SEND_ALLOWED_FILES = {
    "agi/wechat_gateway/server.py": "Gateway._send：STOP/engine/白名单/静默/内容/频率闸门",
    "agi/wechat_gateway/sender.py": "WxAutoSender：仅由 Gateway._send 调用的受控原语",
    "agi/wechat_gateway/send_cli.py": "send_cli：仅由网关子进程调用的受控原语",
}
#: 文档里已失效的绝对断言（一旦代码存在发送路径即 P0 冲突）
DOC_FALSE_SEND_CLAIMS = (
    "不发送任何消息", "不发送消息", "没有发送消息功能",
    "工具没有发送消息功能", "绝不发送消息", "没有任何发送",
)


def scan_send_paths(files: list[Path], *, allowed_files: dict | None = None) -> list[Finding]:
    """扫描真实发送路径，allowed（受控入口）与 unguarded（直调）分开。"""
    allowed = dict(SEND_ALLOWED_FILES if allowed_files is None else allowed_files)
    findings: list[Finding] = []
    for path in files:
        rel_path = rel(path)
        reason = allowed.get(rel_path)
        for i, line in enumerate(read_lines(path), 1):
            for pattern, label in SEND_PATTERNS:
                if not re.search(pattern, line):
                    continue
                gateway_call = bool(re.search(r"\bgateway\._send\s*\(", line))
                note = reason or ("调用 Gateway._send（受控闸门）" if gateway_call else "")
                if reason or gateway_call:
                    findings.append(Finding(
                        "INFO", rel_path, i, f"受控发送入口：{label}（{note}）", line))
                else:
                    findings.append(Finding(
                        "FAIL", rel_path, i,
                        f"未受控发送路径：{label}（无 Gateway._send 闸门证据）", line))
                break
    return findings


def scan_doc_conflicts(send_findings: list[Finding], docs: list[Path] | None = None) -> list[Finding]:
    """文档仍写"不发送任何消息"但代码存在发送路径 → P0 冲突。"""
    if not any(f.level in ("INFO", "FAIL") for f in send_findings):
        return []
    targets = docs or [ROOT / "docs" / "风控与合规说明.md", ROOT / "SPEC.md"]
    findings: list[Finding] = []
    for path in targets:
        if not path.is_file():
            continue
        try:
            lines = read_lines(path)
        except Exception:  # noqa: BLE001
            continue
        for i, line in enumerate(lines, 1):
            for claim in DOC_FALSE_SEND_CLAIMS:
                if claim in line:
                    findings.append(Finding(
                        "FAIL", rel(path), i,
                        f"文档断言「{claim}」与代码发送路径冲突（P0，必须改文档）", line))
                    break
    return findings


# ---------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wx_box 合规静态审计")
    parser.add_argument("--verbose", action="store_true", help="列出所有被扫描的 .py 文件")
    args = parser.parse_args(argv)
    setup_console()

    files = iter_py_files(ROOT)

    print("=" * 66)
    print("wx_box 合规静态审计（只读 / 不注入 / 不写数据库 / 授权只读直读）")
    print("=" * 66)
    print(f"项目根目录 : {ROOT}")
    print(f"扫描文件数 : {len(files)} 个 .py")
    print("已排除目录 : " + "、".join(sorted(EXCLUDE_DIRS)))
    print("已排除文件 : tools/audit_compliance.py（自身 pattern 表含禁用字样）")
    print("素材目录   : wx_core/emoji_assets/（109 个 PNG 随包素材，非 .py，不在扫描范围）")
    print("授权只读 : " + "、".join(ALLOWED_CACHE_READERS)
          + "（只读本地缓存/解密输入；不写微信目录）")
    print("授权辅助 : " + "、".join(ALLOWED_HELPER_MODULES)
          + "（窗口消息/剪贴板/纯文本导出/内置 emoji 素材匹配；无禁用 API）")
    print("授权密钥 : " + "、".join(ALLOWED_MEMORY_READERS)
          + "（用户显式授权、一次性只读：OpenProcess / VirtualQueryEx / ReadProcessMemory；"
            "成功即缓存、之后不再读内存）")
    print("授权数据库: " + "、".join(ALLOWED_DB_READERS)
          + "（用户授权：只读 db_storage 微信库、解密到本机临时目录、sqlite3 mode=ro；"
            "写 SQL/非只读连接仍 FAIL）")
    if args.verbose:
        for p in files:
            print(f"  - {rel(p)}")
    print()

    EXEMPT_MEMORY_HITS.clear()
    EXEMPT_DB_HITS.clear()
    EXEMPT_COMPANION_HITS.clear()
    api_findings = scan_forbidden_api(files)
    path_findings = scan_wechat_paths(files)
    send_findings = scan_send_paths(files)
    doc_findings = scan_doc_conflicts(send_findings)

    fails = [f for f in api_findings + path_findings + send_findings + doc_findings
             if f.level == "FAIL"]
    warns = [f for f in path_findings if f.level == "WARN"]

    print("[1/4] 禁用 API / 库扫描（写内存、注入、Hook、DB 写操作；授权只读项豁免）")
    if api_findings:
        for f in api_findings:
            print(f.render())
    else:
        print("  [PASS] 未发现越界的 API / 库引用")
    if EXEMPT_MEMORY_HITS:
        print("  [授权] 一次性只读密钥提取（wechat_keys/wcdb，可 auto_extract_image_key=false 关闭）：")
        for path, lineno, label in EXEMPT_MEMORY_HITS:
            print(f"         {path}:{lineno}  {label}")
    if EXEMPT_DB_HITS:
        print("  [授权] 数据库只读直读（db_storage 读取 / sqlite3 mode=ro；写回微信库仍 FAIL）：")
        for path, lineno, label in EXEMPT_DB_HITS:
            print(f"         {path}:{lineno}  {label}")
    if EXEMPT_COMPANION_HITS:
        print("  [授权] agi companion 数据库（自建 SQLite：记忆/草稿/用量；不接触微信库）：")
        for path, lineno, label in EXEMPT_COMPANION_HITS[:10]:
            print(f"         {path}:{lineno}  {label}")
        if len(EXEMPT_COMPANION_HITS) > 10:
            print(f"         …… 其余 {len(EXEMPT_COMPANION_HITS) - 10} 处同类（同一授权）")
    print()

    print("[2/4] 微信数据/安装目录字符串检查（写入=FAIL；授权只读模块豁免 WARN）")
    if path_findings:
        for f in path_findings:
            print(f.render())
    else:
        print("  [PASS] 未发现越界的微信目录引用或写入")
    print()

    print("[3/4] 发送路径扫描（allowed=受控入口；FAIL=无闸门直调）")
    allowed_sends = [f for f in send_findings if f.level == "INFO"]
    unguarded_sends = [f for f in send_findings if f.level == "FAIL"]
    if allowed_sends:
        for f in allowed_sends:
            print(f.render())
    if unguarded_sends:
        for f in unguarded_sends:
            print(f.render())
    if not send_findings:
        print("  [PASS] 未发现发送路径字符串")
    elif not unguarded_sends:
        print(f"  [受控] 发送能力 {len(allowed_sends)} 处，均为已知受控入口；"
              "详见 docs/风控与合规说明.md")
    print()

    print("[4/4] 文档一致性（合规文档/SPEC 不得再写“不发送任何消息”）")
    if doc_findings:
        for f in doc_findings:
            print(f.render())
    else:
        print("  [PASS] 未发现文档与发送能力的绝对断言冲突")
    print()

    print("-" * 66)
    if fails:
        print(f"结果：FAIL —— 严重问题 {len(fails)} 项，警告 {len(warns)} 项")
        print("说明：允许『窗口截图/OCR + 本地缓存与数据库只读直读 + 标准窗口消息』；")
        print("      禁止写进程内存、注入、Hook、DB 写操作（回写微信库）、向微信目录写文件；")
        print("      发送消息是受控能力：只能走 Gateway._send（STOP/白名单/静默/策略闸门），")
        print("      直接调用 WxAutoSender/send_cli 视为 unguarded 并 FAIL。")
        print("      请删除上述越界代码后再运行审计。")
        print("=" * 66)
        return 1

    print(f"结果：PASS —— 严重问题 0 项，警告 {len(warns)} 项")
    if warns:
        print("提示：警告项不代表违规，但请确认相关路径只出现在说明文字或只读识别逻辑中。")
    print("结论：未发现写内存、Hook、注入、数据库写操作或向微信目录写文件的代码。")
    print("      授权项：wx_core/media_cache.py、wx_core/dat_decrypt.py、wx_core/emoticon.py")
    print("              （缓存只读 / 图片与贴纸离线解密）；")
    print("              wx_core/wechat_keys.py、wx_core/wcdb.py（一次性只读密钥提取）；")
    print("              wx_core/wcdb.py、wx_core/db_source.py（只读直读微信库、mode=ro、临时目录解密）；")
    print("              wx_core/clipboard.py、wx_core/textlog.py、wx_core/media_export.py、")
    print("              wx_core/builtin_emoji.py（窗口消息/剪贴板/纯文本/内置 emoji 素材辅助）；")
    print("              agi/、webui/agi.py（自建 companion SQLite：记忆/草稿/用量，不接触微信库）。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
