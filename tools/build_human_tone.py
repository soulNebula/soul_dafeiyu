# -*- coding: utf-8 -*-
"""P2-a 真人语气语料构建器：聊天截图 OCR + 项目真实消息 → corpus / profile / fewshot。

数据来源（全部只读）：
1. 5 张真实微信聊天截图：
   - ``C:\\Users\\Administrator\\Desktop\\img\\*.jpg``（4 张「羽毛球群」短聊天，朋友A）
   - ``8b9f2a84862977a59a2e1076cd567668.jpg`` 是与朋友B 的长聊天（已切到
     ``E:\\agent\\tmp\\girl_chat\\part00..06.png``）
   - 用 Windows.Media.Ocr 重新 OCR（**带每行 bounding box**，才能按气泡颜色分 speaker）；
     ``E:\\agent\\logs\\chat_ocr.txt`` 是 Lead 早期无坐标的 OCR，只作对照，不参与构建。
2. 项目内真实联系人库 ``agi/data/selflearn/wechat_contacts/*.sqlite3`` 的 messages 表：
   role=user 的消息 = 用户本人真实输入（只读连接，``mode=ro``）。
3. ``log/messages.jsonl``：**探测后不纳入**——其中 sender=self 的 758 条（85 unique）绝大多数
   是 AI 代发回复 / 测试回环 / 表情占位（如「在，什么事？」「[微笑]」「已润用工具…」），
   不是用户真人输入；放进 self 语料会污染语气画像。详见 profile.md「数据来源与取舍」。

清洗规则：
- 去 OCR 字间空格、时间戳行、顶部状态栏/联系人名、系统消息（撤回/拍了拍/红包等）、
  头像水印乱码（几何位置识别：右边缘 x>0.85W 且 h<=22 的小字块）、纯表情/贴图 OCR 噪声；
- 同一气泡被折行/被 OCR 拆断 → 按行间距 + 对齐方式合并；quote 引用条（「昵称：内容」）丢弃；
- speaker 由气泡颜色判定：微信绿气泡 (≈62,181,117) = self（用户本人，右侧），
  深灰气泡 (≈44,44,44) / 浅色气泡 = other；黑色 (17,17,17) 是顶栏，直接丢；
- 隐私：真实昵称→朋友A/B/C、昵称→[昵称]；6 位以上数字串→[号码]；wxid/邮箱/URL/本地路径→占位符；
- 去重：完全相同文本（按 speaker 分别）只留一条并累计 count。

产出：
- ``agi/eval_data/human_tone/corpus.jsonl``：{id, source, speaker, text, ts_hint, lang, count}
- ``agi/eval_data/human_tone/profile.md``：self 语料统计（句尾标点分布/长度/口语词/emoji/连发）
  + 与当前 AI 输出（联系人库 assistant 消息）的差距结论；末尾附机器可读 JSON（测试用）
- ``agi/eval_data/human_tone/fewshot.json``：20 条真实语气样例（去隐私）

用法::

    # 正常构建（有 E:\\agent\\tmp\\ocr_lines.json 缓存就直接用，否则先跑 Windows OCR）
    E:\\agent\\venv312\\Scripts\\python.exe tools/build_human_tone.py

    # 只看统计不落盘
    E:\\agent\\venv312\\Scripts\\python.exe tools/build_human_tone.py --dry-run

    # 强制重新 OCR
    E:\\agent\\venv312\\Scripts\\python.exe tools/build_human_tone.py --ocr

注意：本脚本不训练、不写 agi/data；contacts DB 用只读 URI 打开。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "agi" / "eval_data" / "human_tone"
CONTACTS_DIR = ROOT / "agi" / "data" / "selflearn" / "wechat_contacts"
SCREENSHOT_DIR = Path(r"C:\Users\Administrator\Desktop\img")
GIRL_PARTS_DIR = Path(r"E:\agent\tmp\girl_chat")
OCR_CACHE = Path(r"E:\agent\tmp\ocr_lines.json")
LONG_IMAGE = "8b9f2a84862977a59a2e1076cd567668.jpg"
LANG = "zh"

# --------------------------------------------------------------------------- #
# 隐私 / 清洗配置
# --------------------------------------------------------------------------- #
#: 截图里能识别的真实称呼 → 占位（小肥羊是用户本人昵称，quote 条会丢弃，仍兜底替换）
NAME_MAP = OrderedDict([
    # 长昵称/全名在前，避免子串漏替
    ("羽毛球群的小杰", "朋友A"),
    ("小杰", "朋友A"),
    ("罗煜莹", "朋友B"),
    ("犯错不睡觉", "朋友B"),
    ("刘武广", "朋友C"),
    ("邱宋凯", "朋友D"),
    ("马骏", "朋友E"),
    ("周酷诗", "朋友F"),
    ("老梅菜", "朋友G"),
    ("梅菜", "朋友G"),
    ("坤哥", "朋友H"),
    ("小黑子", "朋友I"),
    ("汉堡", "朋友J"),
    ("何工", "朋友K"),
    ("色逼", "朋友C"),
    ("小肥羊", "[昵称]"),
    ("白云一片去悠悠", "[昵称]"),
    # 公司/组织名给占位
    ("德赛", "[公司]"),
])
#: 测试/隐私扫描用：这些真实称呼不得出现在任何产出里
REAL_NAME_BLOCKLIST = tuple(NAME_MAP.keys()) + ("何工", "刘工", "周棋")
#: 头像水印 OCR 乱码（几何规则之外再兜底扫一遍）
OCR_GARBAGE_TOKENS = ("我張八又", "我嫵不次", "我八又", "張不", "弳八", "嫵八又", "艹八又")
GARBAGE_CHARS = set("張嫵弳乂亻忄刖")
SYSTEM_PATTERNS = (
    "撤回了一条消息", "拍了拍", "以上是打招呼", "你已添加", "红包", "领取了",
    "开启了好友验证", "消息已发出,但被对方拒收", "加入群聊",
)
TIME_RE = re.compile(
    r"(凌晨|早上|上午|中午|下午|傍晚|晚上|昨天|今天|前天|星期|周[一二三四五六日天])"
    r"|(\d{1,2}\s*月\s*\d{1,2}\s*[日号])"
)
URL_RE = re.compile(r"https?://\S+")
WXID_RE = re.compile(r"wxid_[0-9A-Za-z_:]+")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
CODEFIX_RE = re.compile(r"(?<!\d)\d{6,}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s，。；、\"'）)]*")
INSTITUTION_RE = re.compile(r"(?:人民|附属|中心|第[一二三四五六七八九十]+)医院")
QUOTE_RE = re.compile(r"^[^\s：:]{1,8}\s*[：:]\s*\S")
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\U0001F1E6-\U0001F1FF]"
)
TEXT_EMOJI_RE = re.compile(r"\[[^\[\]]{1,8}\]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ASCII_ALLOW = {"ok", "ok了", "okay", "sorry", "lol", "lsp", "886", "666"}
#: OCR 单字消息白名单（其它 1 字基本是贴图/头像残片）
OCR_SINGLE_ALLOW = {"诶", "欸", "嗯", "哦", "行", "好", "啊", "呀", "哈", "？", "?"}


# --------------------------------------------------------------------------- #
# OCR（Windows.Media.Ocr，带 bounding box）
# --------------------------------------------------------------------------- #
OCR_PS1 = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
function Await($WinRtTask, $ResultType) {
    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    $netTask = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
$lang = [Windows.Globalization.Language]::new('zh-Hans-CN')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if (-not $engine) { throw 'OCR engine unavailable' }
$all = @()
foreach ($f in $args) {
    if (-not (Test-Path $f)) { Write-Host "missing: $f"; continue }
    Write-Host "== $f"
    $sf = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path $f).Path)) ([Windows.Storage.StorageFile])
    $stream = Await ($sf.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    foreach ($line in $result.Lines) {
        $x1 = 1e9; $y1 = 1e9; $x2 = -1e9; $y2 = -1e9
        foreach ($word in $line.Words) {
            $r = $word.BoundingRect
            if ($r.X -lt $x1) { $x1 = $r.X }
            if ($r.Y -lt $y1) { $y1 = $r.Y }
            if (($r.X + $r.Width) -gt $x2) { $x2 = $r.X + $r.Width }
            if (($r.Y + $r.Height) -gt $y2) { $y2 = $r.Y + $r.Height }
        }
        $all += [pscustomobject]@{
            file = [System.IO.Path]::GetFileName($f)
            text = $line.Text
            x = [math]::Round($x1, 1)
            y = [math]::Round($y1, 1)
            w = [math]::Round($x2 - $x1, 1)
            h = [math]::Round($y2 - $y1, 1)
            img_w = $bitmap.PixelWidth
            img_h = $bitmap.PixelHeight
        }
    }
    $stream.Dispose()
}
$all | ConvertTo-Json -Depth 4 | Set-Content -Path $env:HT_OCR_OUT -Encoding UTF8
"""


def screenshot_files(extra_dirs: list[Path] | None = None) -> list[Path]:
    """多目录截图（原 img + img/new 等）+ 长聊切片；逐目录收集 jpg/jpeg/png。"""
    dirs = [SCREENSHOT_DIR, *(extra_dirs or [])]
    files: list[Path] = []
    for base in dirs:
        if not base.exists():
            continue
        for pattern in ("*.jpg", "*.jpeg", "*.png"):
            files.extend(sorted(base.glob(pattern)))
    files = [p for p in files if p.name != LONG_IMAGE]
    parts = sorted(GIRL_PARTS_DIR.glob("part*.png"))
    return files + parts


OCR_SCALE = 2.0
OCR_MAX_DIM = 9500   # Windows.Media.Ocr MaxImageDimension = 10000，留余量
OCR_CHUNK = int((OCR_MAX_DIM - 200) / OCR_SCALE)   # 原图像素高度：超长图按这个切段


def preprocess_for_ocr(files: list[Path], tmp_dir: Path,
                       scale: float = OCR_SCALE) -> dict[str, dict]:
    """放大 + 提对比后再 OCR；超长截图按高度切段（OCR 有 10000px 上限）。

    返回 {准备文件名: {"file": 原图名, "y0": 段在原图的 y 偏移, "scale": 缩放,
                        "img_w"/"img_h": 原图尺寸}}，用于把 bbox 映射回原图。
    """
    from PIL import Image, ImageEnhance

    mapping: dict[str, dict] = {}
    for path in files:
        image = Image.open(path).convert("RGB")
        width, height = image.size
        chunk = max(400, int((OCR_MAX_DIM - 200) / scale))
        if height <= chunk:
            crops = [(image, 0)]
        else:
            crops = []
            offset = 0
            while offset < height:
                crops.append((image.crop((0, offset, width, min(height, offset + chunk))), offset))
                offset += chunk
        for index, (crop, y0) in enumerate(crops):
            scaled = crop.resize((max(1, round(crop.width * scale)),
                                  max(1, round(crop.height * scale))), Image.LANCZOS)
            scaled = ImageEnhance.Contrast(scaled).enhance(1.15)
            name = path.name if len(crops) == 1 else f"{path.stem}__p{index}{path.suffix.lower()}"
            target = tmp_dir / name
            if path.suffix.lower() in (".jpg", ".jpeg"):
                scaled.save(target, quality=95)
            else:
                scaled.save(target)
            mapping[name] = {"file": path.name, "y0": y0, "scale": scale,
                             "img_w": width, "img_h": height}
    return mapping


def run_windows_ocr(files: list[Path], cache_path: Path = OCR_CACHE) -> list[dict]:
    """调用 Windows.Media.Ocr 重新 OCR，返回带 bbox 的行列表。

    注意：PowerShell 的 stdout 在 Windows 上是本地代码页（cp936），直接读会得到乱码；
    这里让 PS 自己用 UTF-8 写文件，再由 Python 读；bbox 坐标从放大图映射回原图。
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    import os
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        mapping = preprocess_for_ocr(list(files), tmp_dir)
        prepared = [tmp_dir / name for name in mapping]
        script = tmp_dir / "ocr_boxes.ps1"
        script.write_text(OCR_PS1, encoding="utf-8-sig")
        env = dict(os.environ, HT_OCR_OUT=str(cache_path))
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(script), *[str(p) for p in prepared]]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)
        if proc.returncode != 0:
            raise SystemExit(f"[ocr] powershell 失败: {proc.stderr[-800:]}")
    raw_rows = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    if not raw_rows or not any(CJK_RE.search(str(row.get("text") or "")) for row in raw_rows):
        raise SystemExit("[ocr] 输出里没有中文，可能是编码问题；请检查 PowerShell/OCR 语言包")
    rows: list[dict] = []
    for row in raw_rows:
        name = Path(str(row.get("file") or "")).name
        info = mapping.get(name)
        if not info:
            continue
        scale = float(info["scale"])
        row["file"] = info["file"]
        row["x"] = round(float(row["x"]) / scale + 0, 1)
        row["y"] = round(float(row["y"]) / scale + float(info["y0"]), 1)
        row["w"] = round(float(row["w"]) / scale, 1)
        row["h"] = round(float(row["h"]) / scale, 1)
        row["img_w"] = info["img_w"]
        row["img_h"] = info["img_h"]
        rows.append(row)
    # 缓存里也必须存“原图坐标”，否则下次直接读缓存会用错坐标系
    cache_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return rows


def load_ocr_lines(*, force_ocr: bool = False, cache_path: Path = OCR_CACHE,
                   files: list[Path] | None = None) -> list[dict]:
    if not force_ocr and cache_path.exists():
        try:
            rows = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            if rows and any(CJK_RE.search(str(row.get("text") or "")) for row in rows):
                return rows
            print("[warn] OCR 缓存里没有中文（可能编码损坏），重新 OCR", file=sys.stderr)
        except json.JSONDecodeError:
            pass
    paths = files if files is not None else screenshot_files()
    return run_windows_ocr(paths, cache_path)


# --------------------------------------------------------------------------- #
# 图片 / 气泡颜色
# --------------------------------------------------------------------------- #
class ImageStore:
    """按需加载截图（numpy），缓存。"""

    def __init__(self, base_dirs: list[Path] | None = None) -> None:
        self.base_dirs = base_dirs or [SCREENSHOT_DIR, GIRL_PARTS_DIR]
        self._cache: dict[str, "object"] = {}

    def path(self, name: str) -> Path:
        for base in self.base_dirs:
            candidate = base / name
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"找不到截图: {name}")

    def array(self, name: str):
        import numpy as np
        from PIL import Image

        if name not in self._cache:
            self._cache[name] = np.asarray(Image.open(self.path(name)).convert("RGB"))
        return self._cache[name]


def classify_side(rgb: tuple[int, int, int]) -> str:
    """气泡背景色 → speaker；无法判定归 other。"""
    r, g, b = (int(v) for v in rgb)
    if g > 110 and g > r + 40 and g > b + 20:
        return "self"
    return "other"


def is_header_color(rgb: tuple[int, int, int]) -> bool:
    """顶部状态栏/标题栏的黑色背景 (≈17,17,17)。"""
    r, g, b = (int(v) for v in rgb)
    return max(rgb) - min(rgb) <= 8 and max(rgb) <= 28


def is_self_green(rgb: tuple[int, int, int]) -> bool:
    """微信绿气泡底色（截图里稳定在 ≈(62,181,117)）；阈值收紧避免误判壁纸绿色。"""
    r, g, b = (int(v) for v in rgb)
    return g >= 150 and (g - r) >= 70 and (g - b) >= 40


def sample_bubble_color(arr, x: int, y: int, w: int, h: int) -> tuple[int, int, int]:
    """在文字 bbox 四周多偏移找气泡底色。

    OCR bbox 有时比气泡宽（把表情/边框也框进去），只在 bbox 外侧取色会取到壁纸；
    这里按由近到远采样，优先返回“微信绿”（self）；找不到就返回候选色中位值（other）。
    """
    import numpy as np

    h_img, w_img = arr.shape[:2]
    yc = int(y + h / 2)
    xc = int(x + w / 2)

    def at(yy: int, xx: int):
        return arr[max(0, min(h_img - 1, yy)), max(0, min(w_img - 1, xx))]

    fallback: list[tuple[int, int, int]] = []
    for k in (3, 5, 7, 10, 14, 18):
        candidates = [
            at(yc, x - k), at(yc, x + w + k),
            at(y - k, xc), at(y + h + k, xc),
        ]
        for rgb in candidates:
            rgb = tuple(int(v) for v in rgb)
            if is_self_green(rgb):
                return rgb
            fallback.append(rgb)
    if not fallback:
        return (0, 0, 0)
    px = np.array(fallback)
    med = np.median(px, axis=0).astype(int)
    return tuple(int(v) for v in med)


def is_avatar_geometry(x: float, y: float, w: float, h: float,
                       img_w: float, img_h: float) -> bool:
    """左侧头像区域被 OCR 识别成「0」等小块的过滤（x 靠左、近方形、约头像大小）。"""
    if img_w <= 0 or img_h <= 0:
        return False
    return (x <= 0.12 * img_w and w <= 0.13 * img_w
            and 0.035 * img_h <= h <= 0.09 * img_h)


# --------------------------------------------------------------------------- #
# OCR 行清洗 / 合并
# --------------------------------------------------------------------------- #
def normalize_ocr_text(raw: str) -> str:
    """去 OCR 字间空格 + 清理首尾噪声符号；保留中文/标点/emoji。"""
    text = str(raw or "").replace("\u3000", " ").strip()
    # 中文字符之间的空格、中文与标点之间的空格全部去掉
    text = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+", "", text)
    text = re.sub(r"[ \t]+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"[ \t]+(?=[，。！？；：、,.!?;:…])", "", text)
    text = re.sub(r"(?<=[，。！？；：、,.!?;:…])[ \t]+", "", text)
    # 反复剥掉首部 OCR 噪声字符（气泡圆角/边框/表情残片），但保住「一天/一起」这种真词
    noise_prefix = set("一0０〇日}彳《丿'\"`、，,.·-—~!！?？:：;； @＠")
    while text:
        head = text[0]
        if head in noise_prefix:
            if (head == "一" and len(text) > 1
                    and CJK_RE.match(text[1]) and text[1] not in "我你他她它们这那"):
                break
            text = text[1:].lstrip(" \u3000")
            continue
        if not (head.isalnum() or CJK_RE.match(head)
                or head in "，。！？；：、,.!?;:…" or EMOJI_RE.match(head)):
            text = text[1:].lstrip(" \u3000")
            continue
        break
    # 首部 OCR 噪声：英文字母/「X，数字」残片粘在真消息前面（如「O那明天再去吧」「下，02咋了呀」）
    text = re.sub(r"^[A-Za-z]{1,3}(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"^[\u4e00-\u9fff]{1,3}[，,]\d{1,2}(?=[\u4e00-\u9fff])", "", text)
    # 尾部噪声：反复剥（表情 OCR 出的「铲」、引号、括号、孤零零的数字等）
    trailing = set("《》〈〉@＠冫彳铲、，,.;:：； 0０〇")
    while text and text[-1] in trailing:
        text = text[:-1]
    if len(text) >= 6:
        text = re.sub(r"(?<=[\u4e00-\u9fff])一$", "", text)
    # OCR 常把「？」识别成 7：中文句尾的孤立 7 视作问号
    text = re.sub(r"(?<=[\u4e00-\u9fff])[7７]$", "？", text)
    return text.strip()


def is_timestamp_text(text: str, rgb: tuple[int, int, int] | None = None) -> bool:
    """时间戳判定：带「昨天/下午/凌晨」等日期词，或灰色小字里带冒号时间。

    注意：纯「3:30~4:30」可能是**聊天内容**（约时间），不能一律当时间戳丢。
    """
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return False
    if TIME_RE.search(compact):
        return True
    if rgb is not None:
        r, g, b = (int(v) for v in rgb)
        if (max(rgb) - min(rgb) <= 12 and 120 <= max(rgb) <= 200
                and re.search(r"\d{1,2}[:：]\d{2}", compact)):
            return True
    return False


def is_system_text(text: str) -> bool:
    return any(pattern in str(text or "") for pattern in SYSTEM_PATTERNS)


def is_watermark_geometry(x: float, y: float, w: float, h: float, img_w: float) -> bool:
    """右边缘的小字块 = 头像/水印 OCR 乱码（如「我張八又」「我八又」）。"""
    return img_w > 0 and x >= 0.85 * img_w and h <= 22 and w <= 90


def looks_like_garbage(text: str) -> bool:
    if any(token in text for token in OCR_GARBAGE_TOKENS):
        return True
    if set(text) & GARBAGE_CHARS:
        return True
    if set(text) & set("亠丶冫彳亻丿乂々刁弳丨"):
        return True
    if "「" in text or "」" in text:
        return True   # 引用条/气泡圆角 OCR 残片
    compact = re.sub(r"[\s\W_]+", "", text)
    return not compact


def keep_ocr_line(text: str, rgb: tuple[int, int, int]) -> bool:
    """行级过滤（不含几何水印判定）。"""
    if not text:
        return False
    if is_header_color(rgb) or is_timestamp_text(text, rgb):
        return False
    if is_system_text(text):
        return False
    if looks_like_garbage(text):
        return False
    if QUOTE_RE.match(text):
        return False
    if not CJK_RE.search(text) and text.strip().lower() not in ASCII_ALLOW:
        return False
    return True


def merge_same_line(rows: list[dict]) -> list[dict]:
    """同一视觉行被 OCR 拆成多段（如「小肥羊：」+「不是早说了吗？」）→ 合并。"""
    merged: list[dict] = []
    for row in rows:
        if merged:
            prev = merged[-1]
            overlap = min(prev["y"] + prev["h"], row["y"] + row["h"]) - max(prev["y"], row["y"])
            if overlap > 0.5 * min(prev["h"], row["h"]):
                left, right = (prev, row) if prev["x"] <= row["x"] else (row, prev)
                prev["text"] = f"{prev['text']} {row['text']}".strip()
                x2 = max(prev["x"] + prev["w"], row["x"] + row["w"])
                y2 = max(prev["y"] + prev["h"], row["y"] + row["h"])
                prev["x"] = min(prev["x"], row["x"])
                prev["y"] = min(prev["y"], row["y"])
                prev["w"] = x2 - prev["x"]
                prev["h"] = y2 - prev["y"]
                continue
        merged.append(dict(row))
    return merged


def ocr_image_messages(name: str, rows: list[dict], store: ImageStore) -> list[dict]:
    """一张图 → 按气泡合并后的消息列表 [{speaker,text,ts_hint,order}]。

    处理顺序很重要：先按“行”清掉顶栏/时间戳/头像水印/系统消息，再合并同一视觉行碎片
    （合并后还要再过滤 quote 引用条），最后按气泡底色和行间距合并多行气泡。
    """
    arr = store.array(name)
    img_h, img_w = arr.shape[:2]
    rows = [dict(row) for row in rows if row.get("file") == name]
    if not rows:
        return []
    rows.sort(key=lambda r: (r["y"], r["x"]))

    timestamp = ""
    kept: list[dict] = []
    for row in rows:
        text = normalize_ocr_text(row.get("text"))
        rgb = sample_bubble_color(arr, int(row["x"]), int(row["y"]),
                                  int(row["w"]), int(row["h"]))
        if is_header_color(rgb):
            continue
        if is_timestamp_text(text, rgb):
            timestamp = text
            continue
        if is_watermark_geometry(row["x"], row["y"], row["w"], row["h"], img_w):
            continue
        if is_avatar_geometry(row["x"], row["y"], row["w"], row["h"], img_w, img_h):
            continue
        if is_system_text(text):
            continue
        if not keep_ocr_line(text, rgb):
            continue
        if len(text) == 1 and text not in OCR_SINGLE_ALLOW:
            continue   # 单字基本是贴图/头像残片（如「至」「了」）
        kept.append({**row, "text": text, "rgb": rgb,
                     "speaker": classify_side(rgb), "ts_hint": timestamp})

    # 同一视觉行被 OCR 拆断的碎片先合并，再重新过滤（可能拼出 quote 引用条）
    kept = merge_same_line(kept)
    filtered: list[dict] = []
    for row in kept:
        text = normalize_ocr_text(row.get("text"))
        if not keep_ocr_line(text, row["rgb"]):
            continue
        if len(text) == 1 and text not in OCR_SINGLE_ALLOW:
            continue
        filtered.append({**row, "text": text,
                         "speaker": classify_side(row["rgb"])})

    messages: list[dict] = []
    open_bubble: dict | None = None
    for row in filtered:
        gap = row["y"] - (open_bubble["y2"] if open_bubble else -1e9)
        align_tol = 0.06 * img_w
        same_bubble = (
            open_bubble is not None
            and open_bubble["speaker"] == row["speaker"]
            and gap <= 0.7 * min(row["h"], open_bubble["h"])
            and (abs(row["x"] - open_bubble["x"]) <= align_tol
                 or abs((row["x"] + row["w"]) - open_bubble["x2"]) <= align_tol)
        )
        if same_bubble and open_bubble is not None:
            open_bubble["text"] = f"{open_bubble['text']}{row['text']}"
            open_bubble["y2"] = row["y"] + row["h"]
            open_bubble["h"] = row["h"]
            if abs((row["x"] + row["w"]) - open_bubble["x2"]) <= align_tol:
                open_bubble["x2"] = row["x"] + row["w"]
        else:
            open_bubble = {
                "speaker": row["speaker"],
                "text": row["text"],
                "ts_hint": row["ts_hint"],
                "y2": row["y"] + row["h"],
                "h": row["h"],
                "x": row["x"],
                "x2": row["x"] + row["w"],
            }
            messages.append(open_bubble)
    return messages


# --------------------------------------------------------------------------- #
# 数据库 / 隐私
# --------------------------------------------------------------------------- #
def read_contacts_messages(contacts_dir: Path = CONTACTS_DIR
                           ) -> tuple[list[dict], list[dict], list[dict]]:
    """只读读联系人库：返回 (user 消息行, assistant 列表, 按时间排序的全部行)。"""
    import sqlite3

    users: list[dict] = []
    assistants: list[dict] = []
    ordered: list[dict] = []
    if not contacts_dir.exists():
        return users, assistants, ordered
    for path in sorted(contacts_dir.glob("*.sqlite3")):
        uri = "file:" + path.as_posix() + "?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            continue
        try:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "messages" not in tables:
                continue
            rows = con.execute(
                "SELECT role, text, ts, proactive FROM messages "
                "WHERE text IS NOT NULL AND TRIM(text)<>'' ORDER BY ts"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for role, text, ts, proactive in rows:
            text = str(text).strip()
            if not text:
                continue
            ordered.append({"speaker": "self" if role == "user" else "other"})
            if role == "user":
                users.append({"source": "contacts_db", "text": text, "ts_hint": str(ts or "")})
            elif role == "assistant":
                assistants.append({"text": text, "proactive": bool(proactive)})
    return users, assistants, ordered


def clean_db_text(text: str) -> str:
    """DB 里混着测试/引用/表情占位等非真人有效输入，这里过滤掉。"""
    value = str(text or "").strip()
    if not value:
        return ""
    drop_patterns = ("测试", "对方引用", "引用了一条消息", "发了个表情包", "发了张图片",
                     "链路", "[对方引用")
    if any(pattern in value for pattern in drop_patterns):
        return ""
    if re.fullmatch(r"(?:\[[^\[\]]{1,8}\])+", value):
        return ""   # 纯 [呲牙][呲牙] 这类表情占位
    if not CJK_RE.search(value) and value.lower() not in ASCII_ALLOW:
        return ""   # 纯表情 / 纯符号占位
    return value


def anonymize(text: str) -> str:
    """隐私脱敏：昵称→朋友X、号码→[号码]、链接/路径/邮箱→占位。"""
    value = str(text or "")
    for name, replacement in NAME_MAP.items():
        value = value.replace(name, replacement)
    value = URL_RE.sub("[链接]", value)
    value = WXID_RE.sub("[微信号]", value)
    value = PHONE_RE.sub("[号码]", value)
    value = CODEFIX_RE.sub("[号码]", value)
    value = EMAIL_RE.sub("[邮箱]", value)
    value = PATH_RE.sub("[本地路径]", value)
    value = INSTITUTION_RE.sub("[医院]", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def privacy_violations(text: str) -> list[str]:
    """隐私扫描：返回命中的问题列表（空 = 干净）。"""
    problems: list[str] = []
    value = str(text or "")
    for name in REAL_NAME_BLOCKLIST:
        if name in value:
            problems.append(f"real_name:{name}")
    if WXID_RE.search(value):
        problems.append("wxid")
    if PHONE_RE.search(value):
        problems.append("phone")
    if re.search(r"(?<!\d)\d{8,}(?!\d)", value):
        problems.append("long_digits")
    if EMAIL_RE.search(value):
        problems.append("email")
    if PATH_RE.search(value):
        problems.append("win_path")
    if any(token in value for token in OCR_GARBAGE_TOKENS):
        problems.append("ocr_watermark")
    if set(value) & (GARBAGE_CHARS | set("亠丶冫彳丿乂々刁")):
        problems.append("ocr_garbage_char")
    return problems


# --------------------------------------------------------------------------- #
# 语料组装
# --------------------------------------------------------------------------- #
def build_corpus(ocr_lines: list[dict], contacts_dir: Path = CONTACTS_DIR,
                 image_store: ImageStore | None = None) -> tuple[list[dict], dict]:
    """OCR 截图 + 联系人库 → 去重后的 corpus 记录 + 构建信息。"""
    store = image_store or ImageStore()
    files = sorted({str(row.get("file")) for row in ocr_lines})
    # 长图已被切成 part，忽略原始整图
    files = [name for name in files if name != LONG_IMAGE]

    raw_sequences: list[list[dict]] = []   # 用于连发统计（含 other，未去重）
    rows: list[dict] = []
    per_image: dict[str, int] = {}
    for name in files:
        messages = ocr_image_messages(name, ocr_lines, store)
        if not messages:
            continue
        raw_sequences.append(messages)
        self_count = 0
        for index, message in enumerate(messages):
            text = anonymize(message["text"])
            if not text:
                continue
            if message["speaker"] == "self":
                self_count += 1
            rows.append({
                "source": f"ocr:{Path(name).stem[:12]}",
                "speaker": message["speaker"],
                "text": text,
                "ts_hint": anonymize(message.get("ts_hint") or ""),
                "order": index,
            })
        per_image[name] = self_count

    users, assistant_items, db_ordered = read_contacts_messages(contacts_dir)
    db_sequence: list[dict] = []
    for item in users:
        text = anonymize(clean_db_text(item["text"]))
        if not text:
            continue
        rows.append({"source": "contacts_db", "speaker": "self", "text": text,
                     "ts_hint": item.get("ts_hint") or "", "order": 0})
    for item in db_ordered:
        db_sequence.append(dict(item))
    if db_sequence:
        raw_sequences.append(db_sequence)

    # 去重：相同 speaker + text 只留一条，累计 count
    deduped: "OrderedDict[tuple, dict]" = OrderedDict()
    for row in rows:
        key = (row["speaker"], row["text"])
        if key in deduped:
            deduped[key]["count"] += 1
            continue
        deduped[key] = {**row, "count": 1}
    corpus: list[dict] = []
    for index, record in enumerate(deduped.values(), 1):
        corpus.append({
            "id": f"ht-{index:04d}",
            "source": record["source"],
            "speaker": record["speaker"],
            "text": record["text"],
            "ts_hint": record["ts_hint"],
            "lang": LANG,
            "count": record["count"],
        })

    info = {
        "ocr_files": files,
        "ocr_self_by_image": per_image,
        "ocr_rows": sum(1 for record in corpus if record["source"].startswith("ocr:")),
        "db_rows": sum(1 for record in corpus if record["source"].startswith("contacts_db")),
        "raw_sequences": raw_sequences,
        "assistant_items": assistant_items,
        "dropped_after_dedupe": len(rows) - len(corpus),
    }
    return corpus, info


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
LENGTH_BUCKETS = ("<=8", "9-15", "16-25", "26-40", ">40")
ENDING_CATEGORIES = ("无标点", "问号", "感叹号", "句号", "省略号")

LEXICON = (
    "哈哈哈", "哈哈", "笑死", "真的", "服了", "诶", "欸", "艹", "靠", "卧槽", "我靠",
    "咋", "啥", "嘛", "呗", "啊", "呀", "吧", "呢", "好家伙", "离谱", "无语", "麻了",
    "绝了", "救命", "绷不住", "笑不活了", "不会吧", "不是吧", "完蛋", "烦死", "气死",
    "累死", "饿死", "困死", "吐了", "想吐", "牛逼", "哎", "唉", "哦", "嗯",
    "行吧", "可以", "好的", "lsp", "芜湖", "冲", "搞", "整", "玩意", "东西", "事儿",
    "贼", "真香", "就这", "666", "emmm", "呜呜", "可恶", "什么玩意",
)


def normalize_text_length(text: str) -> int:
    return len(text)


def length_bucket(length: int) -> str:
    if length <= 8:
        return "<=8"
    if length <= 15:
        return "9-15"
    if length <= 25:
        return "16-25"
    if length <= 40:
        return "26-40"
    return ">40"


def ending_category(text: str) -> str:
    value = str(text or "").rstrip()
    if not value:
        return "无标点"
    if re.search(r"(…+|\.{3,}|。{2,})$", value):
        return "省略号"
    last = value[-1]
    if last in "？?":
        return "问号"
    if last in "！!":
        return "感叹号"
    if last in "。.":
        return "句号"
    return "无标点"


def has_emoji(text: str) -> bool:
    return bool(EMOJI_RE.search(text) or TEXT_EMOJI_RE.search(text))


def run_lengths(sequences: list[list[dict]]) -> Counter:
    """self 连发条数分布（连续 self 消息为一段，遇到 other 断开）。"""
    lengths: Counter = Counter()
    for sequence in sequences:
        run = 0
        for message in sequence:
            if message["speaker"] == "self":
                run += 1
            elif run:
                lengths[run] += 1
                run = 0
        if run:
            lengths[run] += 1
    return lengths


def basic_text_stats(texts: list[str]) -> dict:
    texts = [text for text in texts if text]
    lengths = [normalize_text_length(text) for text in texts]
    ends = Counter(ending_category(text) for text in texts)
    total = max(1, len(texts))
    return {
        "count": len(texts),
        "length_median": statistics.median(lengths) if lengths else 0,
        "length_mean": round(statistics.mean(lengths), 2) if lengths else 0,
        "length_dist": {bucket: round(sum(1 for n in lengths if length_bucket(n) == bucket)
                                      / total, 4) for bucket in LENGTH_BUCKETS},
        "ending_dist": {category: round(ends.get(category, 0) / total, 4)
                        for category in ENDING_CATEGORIES},
        "emoji_rate": round(sum(1 for text in texts if has_emoji(text)) / total, 4),
    }


def compute_profile(corpus: list[dict], info: dict) -> dict:
    """self 语料画像 + 当前 AI 输出对照（全部数字都从这里出，profile.md 只做渲染）。"""
    self_texts = [row["text"] for row in corpus if row["speaker"] == "self"]
    self_unique = len(self_texts)
    self_total = sum(row["count"] for row in corpus if row["speaker"] == "self")
    other_texts = [row["text"] for row in corpus if row["speaker"] == "other"]

    base = basic_text_stats(self_texts)
    assistant_items = info.get("assistant_items") or []
    ai_all_texts = [item["text"] for item in assistant_items]
    ai_proactive_texts = [item["text"] for item in assistant_items if item.get("proactive")]
    ai_reply_texts = [item["text"] for item in assistant_items if not item.get("proactive")]
    ai_stats = basic_text_stats(ai_all_texts)
    ai_proactive_stats = basic_text_stats(ai_proactive_texts)
    ai_reply_stats = basic_text_stats(ai_reply_texts)

    lexicon_counts: Counter = Counter()
    for text in self_texts:
        for word in LEXICON:
            hits = text.count(word)
            if hits:
                lexicon_counts[word] += hits
    openings: Counter = Counter()
    for text in self_texts:
        if not text:
            continue
        openings[text[:2]] += 1
    runs = run_lengths(info.get("raw_sequences") or [])
    total_runs = max(1, sum(runs.values()))

    profile = {
        "generated_by": "tools/build_human_tone.py",
        "self_rows": self_unique,
        "self_total_occurrences": self_total,
        "other_rows": len(other_texts),
        "by_source": dict(Counter(row["source"].split(":")[0] for row in corpus
                                  if row["speaker"] == "self")),
        "length": {
            "median": base["length_median"],
            "mean": base["length_mean"],
            "dist": base["length_dist"],
        },
        "ending": base["ending_dist"],
        "emoji_rate": base["emoji_rate"],
        "lexicon_top": lexicon_counts.most_common(20),
        "openings_top": openings.most_common(10),
        "run_lengths": {str(length): {"runs": runs[length],
                                      "pct": round(runs[length] / total_runs, 4)}
                        for length in sorted(runs)},
        "ai": {
            "count": ai_stats["count"],
            "length_median": ai_stats["length_median"],
            "length_mean": ai_stats["length_mean"],
            "ending": ai_stats["ending_dist"],
            "emoji_rate": ai_stats["emoji_rate"],
            "proactive": {
                "count": ai_proactive_stats["count"],
                "length_median": ai_proactive_stats["length_median"],
                "ending": ai_proactive_stats["ending_dist"],
            },
            "reply": {
                "count": ai_reply_stats["count"],
                "length_median": ai_reply_stats["length_median"],
                "ending": ai_reply_stats["ending_dist"],
            },
        },
        "ocr_self_by_image": info.get("ocr_self_by_image", {}),
        "ocr_usable_images": len(info.get("ocr_files") or []),
        "ocr_coverage_note": (
            f"{len(info.get('ocr_files') or [])} 个切片全部可用（多目录截图 img + img/new + "
            "长聊天 part00–06，2x 放大后 OCR）。OCR 仍会漏掉 1–2 字短气泡和纯图片/表情消息"
            "（例：part00 底部的「哈哈哈」「可惨你」被切到下一段/截断、贴图与图片不进入语料），"
            "故 self 语料以“能稳定识别的文本”为准；句末标点也可能被 OCR 吞掉"
            "（问号常被识别成 7，脚本已还原；截图本身几乎不用句号/感叹号）。"
        ),
        "log_messages_excluded": (
            "log/messages.jsonl 的 sender=self 758 条（85 unique）经探测主要是 AI 代发回复/"
            "测试回环/表情占位，不是用户真人输入，故不纳入 self 语料，避免污染画像。"
        ),
    }
    return profile


def render_profile_md(profile: dict) -> str:
    def pct(value: float) -> str:
        return f"{value * 100:.1f}%"

    lines: list[str] = []
    lines.append("# 真人语气画像（self 语料）")
    lines.append("")
    lines.append("> 由 `tools/build_human_tone.py` 自动生成，数字与 `corpus.jsonl` 可复算；"
                 "机器可读版本见文末 JSON。")
    lines.append("")
    lines.append("## 1. 语料概况")
    lines.append("")
    lines.append(f"- self 语料：**{profile['self_rows']} 条唯一**"
                 f"（含重复出现共 {profile['self_total_occurrences']} 次）；"
                 f"other 语境 {profile['other_rows']} 条（仅作上下文）")
    lines.append(f"- 来源分布（self）：`{json.dumps(profile['by_source'], ensure_ascii=False)}`")
    lines.append(f"- OCR 可用图片：{profile['ocr_usable_images']} 张；"
                 f"每图识别出的 self 条数：`{json.dumps(profile['ocr_self_by_image'], ensure_ascii=False)}`")
    lines.append(f"- OCR 覆盖说明：{profile['ocr_coverage_note']}")
    lines.append(f"- 数据取舍：{profile['log_messages_excluded']}")
    lines.append("")
    lines.append("## 2. 长度分布（self）")
    lines.append("")
    lines.append(f"- 中位数：**{profile['length']['median']}** 字；均值 {profile['length']['mean']} 字")
    lines.append("")
    lines.append("| 长度（字） | 占比 |")
    lines.append("| --- | --- |")
    for bucket, value in profile["length"]["dist"].items():
        lines.append(f"| {bucket} | {pct(value)} |")
    lines.append("")
    lines.append("## 3. 句尾标点分布（self）★ 关键数字")
    lines.append("")
    lines.append("| 句尾 | 占比 |")
    lines.append("| --- | --- |")
    labels = {"无标点": "无标点", "问号": "问号 ？", "感叹号": "感叹号 ！",
              "句号": "句号 。", "省略号": "省略号 …"}
    for category in ENDING_CATEGORIES:
        lines.append(f"| {labels[category]} | {pct(profile['ending'][category])} |")
    lines.append("")
    lines.append("> 注：OCR 可能吞掉句末标点（问号常被识别成 7，脚本已还原）；"
                 "截图里本身几乎不用句号/感叹号，这与人工核对一致。")
    lines.append("")
    lines.append("## 4. 口语词 Top20（self）")
    lines.append("")
    lines.append("| 词 | 次数 |")
    lines.append("| --- | --- |")
    for word, count in profile["lexicon_top"]:
        lines.append(f"| {word} | {count} |")
    lines.append("")
    lines.append("## 5. 其他特征")
    lines.append("")
    lines.append(f"- emoji / 表情使用率：**{pct(profile['emoji_rate'])}**（self 消息里含 emoji 或 [表情] 的比例）")
    lines.append(f"- 高频开场（前 2 字）：`{json.dumps(profile['openings_top'], ensure_ascii=False)}`")
    runs = profile["run_lengths"]
    lines.append("- 连发条数分布（连续 self 气泡）：" + "，".join(
        f"{length} 条 {pct(item['pct'])}" for length, item in runs.items()) or "- 连发：无")
    lines.append("")
    lines.append("## 6. 与当前 AI 输出的差距")
    lines.append("")
    ai = profile["ai"]
    proactive = ai.get("proactive", {})
    reply = ai.get("reply", {})
    lines.append(
        f"- AI 语料：{ai['count']} 条（联系人库 assistant 消息；其中 **主动消息 "
        f"{proactive.get('count', 0)} 条**、普通回复 {reply.get('count', 0)} 条）")
    lines.append("")
    lines.append("| 指标 | 真人 self | AI 主动消息 | AI 普通回复 |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(f"| 条数 | {profile['self_rows']} | {proactive.get('count', 0)} | {reply.get('count', 0)} |")
    lines.append(f"| 长度中位数 | {profile['length']['median']} 字 | "
                 f"{proactive.get('length_median', 0)} 字 | {reply.get('length_median', 0)} 字 |")
    p_ending = proactive.get("ending", {})
    r_ending = reply.get("ending", {})
    lines.append(f"| 无标点句尾 | {pct(profile['ending']['无标点'])} | "
                 f"{pct(p_ending.get('无标点', 0))} | {pct(r_ending.get('无标点', 0))} |")
    lines.append(f"| 句号收尾 | {pct(profile['ending']['句号'])} | "
                 f"{pct(p_ending.get('句号', 0))} | {pct(r_ending.get('句号', 0))} |")
    lines.append(f"| 问号收尾 | {pct(profile['ending']['问号'])} | "
                 f"{pct(p_ending.get('问号', 0))} | {pct(r_ending.get('问号', 0))} |")
    lines.append(f"| emoji 使用率 | {pct(profile['emoji_rate'])} | "
                 f"{pct(ai['emoji_rate'])} | — |")
    lines.append("")
    lines.append("**结论**：")
    lines.append("")
    lines.append(
        f"1. 真人聊天极短（中位数 {profile['length']['median']} 字，"
        f"{pct(profile['length']['dist'].get('<=8', 0))} 的消息 ≤8 字），"
        f"一条只表达一个意思；AI 主动消息中位数 {proactive.get('length_median', 0)} 字，"
        f"信息密度和完整度都高太多。")
    lines.append(
        f"2. 真人 **{pct(profile['ending']['无标点'])} 的 self 消息句尾不加任何标点**，"
        f"问号只用于真提问（{pct(profile['ending']['问号'])}）、感叹号 {pct(profile['ending']['感叹号'])}、"
        f"句号 {pct(profile['ending']['句号'])}；AI 主动消息问号收尾占 "
        f"{pct(p_ending.get('问号', 0))}、无标点仅 {pct(p_ending.get('无标点', 0))}"
        f"——最该校准的“客服腔/采访腔”来源。")
    lines.append(
        f"3. 真人口语高频是语气词/情绪直说（{', '.join(word for word, _ in profile['lexicon_top'][:8])}），"
        f"emoji 使用率 {pct(profile['emoji_rate'])}；AI 更倾向完整句 + 书面连接词、几乎不用 emoji。")
    lines.append(
        "4. 真人会连发多条短气泡（"
        + "，".join(f"{length} 条占 {pct(item['pct'])}" for length, item in runs.items() if item['runs'])
        + "），AI 习惯一条说完；训练应鼓励一句一条、避免长句。")
    lines.append("")
    lines.append("## 7. 数据来源与取舍")
    lines.append("")
    lines.append("- 截图 OCR：微信截图（img + img/new 多目录）+ 长聊天 part00–06"
                 "（Windows.Media.Ocr，带 bbox）")
    lines.append("- 项目联系人库：`agi/data/selflearn/wechat_contacts/*.sqlite3` 的 role=user（只读）")
    lines.append("- 排除：`log/messages.jsonl` 的 sender=self（AI 代发/测试回环，非真人输入）")
    lines.append("")
    lines.append("## 8. 机器可读统计（测试用，勿删）")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(profile, ensure_ascii=False, indent=1, sort_keys=True))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# few-shot
# --------------------------------------------------------------------------- #
def select_fewshot(corpus: list[dict], count: int = 30) -> list[dict]:
    """挑 20 条短、干净、能体现长度/标点习惯的 self 真实短句。"""
    def clean(row: dict) -> bool:
        text = row["text"]
        if not (1 <= len(text) <= 14):
            return False
        if len(text) == 1 and text not in OCR_SINGLE_ALLOW:
            return False
        if "[" in text or privacy_violations(text):
            return False
        if set(text) & (GARBAGE_CHARS | set("亠丶冫彳丿乂々刁")):
            return False
        return True

    candidates = [row for row in corpus if row["speaker"] == "self" and clean(row)]
    priority = {"ocr": 0, "contacts_db": 1}
    candidates.sort(key=lambda row: (priority.get(row["source"].split(":")[0], 9),
                                     row["id"]))
    chosen: list[dict] = []
    used_ending: Counter = Counter()
    # 先保证句尾标点类别覆盖，再补足数量
    for row in candidates:
        ending = ending_category(row["text"])
        if used_ending[ending] >= 5:
            continue
        used_ending[ending] += 1
        chosen.append(row)
        if len(chosen) >= count:
            break
    if len(chosen) < count:
        chosen_ids = {row["id"] for row in chosen}
        for row in candidates:
            if row["id"] in chosen_ids:
                continue
            chosen.append(row)
            if len(chosen) >= count:
                break
    return [{
        "id": row["id"],
        "text": row["text"],
        "source": row["source"],
        "length": len(row["text"]),
        "ending": ending_category(row["text"]),
    } for row in chosen[:count]]


# --------------------------------------------------------------------------- #
# 写盘 / CLI
# --------------------------------------------------------------------------- #
def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n"
                            for record in records), encoding="utf-8")


def build(out_dir: Path = OUT_DIR, *, force_ocr: bool = False, dry_run: bool = False,
          ocr_cache: Path = OCR_CACHE, contacts_dir: Path = CONTACTS_DIR,
          ocr_lines: list[dict] | None = None,
          img_dirs: list[Path] | None = None, fewshot_count: int = 30) -> dict:
    """完整构建；dry_run 只返回统计不写盘。

    img_dirs: 额外的截图目录（除默认 SCREENSHOT_DIR 外，如 img/new）；
    fewshot_count: few-shot 条数（默认 30）。
    """
    dirs = [SCREENSHOT_DIR, *(img_dirs or [])]
    files = None if ocr_lines is not None else screenshot_files(img_dirs)
    lines = ocr_lines if ocr_lines is not None else load_ocr_lines(
        force_ocr=force_ocr, cache_path=ocr_cache, files=files)
    store = ImageStore(base_dirs=[*dirs, GIRL_PARTS_DIR])
    corpus, info = build_corpus(lines, contacts_dir=contacts_dir, image_store=store)
    profile = compute_profile(corpus, info)
    fewshot = select_fewshot(corpus, fewshot_count)
    summary = {
        "corpus_rows": len(corpus),
        "self_rows": profile["self_rows"],
        "other_rows": profile["other_rows"],
        "fewshot": len(fewshot),
        "ocr_files": len(info["ocr_files"]),
        "ending": profile["ending"],
        "length_median": profile["length"]["median"],
    }
    if dry_run:
        return {"summary": summary, "corpus": corpus, "profile": profile,
                "fewshot": fewshot}
    _write_jsonl(out_dir / "corpus.jsonl", corpus)
    (out_dir / "profile.md").write_text(render_profile_md(profile), encoding="utf-8")
    fewshot_payload = {
        "description": "真人语气 few-shot 示例（全部来自 self 语料，已去隐私）",
        "count": len(fewshot),
        "examples": fewshot,
    }
    (out_dir / "fewshot.json").write_text(
        json.dumps(fewshot_payload, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    return {"summary": summary, "corpus": corpus, "profile": profile,
            "fewshot": fewshot}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建真人语气语料 corpus/profile/fewshot")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--ocr", action="store_true", help="强制重新 OCR 截图")
    parser.add_argument("--ocr-cache", default=str(OCR_CACHE))
    parser.add_argument("--contacts-dir", default=str(CONTACTS_DIR))
    parser.add_argument("--img-dir", action="append", default=None,
                        help="额外截图目录（可多次指定；默认只读 Desktop/img）")
    parser.add_argument("--fewshot-count", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = parser.parse_args(argv)

    img_dirs = [Path(item) for item in (args.img_dir or [])]
    result = build(Path(args.out_dir), force_ocr=args.ocr, dry_run=args.dry_run,
                   ocr_cache=Path(args.ocr_cache),
                   contacts_dir=Path(args.contacts_dir),
                   img_dirs=img_dirs, fewshot_count=args.fewshot_count)
    summary = result["summary"]
    print(json.dumps({"summary": summary}, ensure_ascii=False, indent=1))
    if args.dry_run:
        print("[dry-run] 未写盘")
    else:
        print(f"[ok] 写出 {args.out_dir}/corpus.jsonl / profile.md / fewshot.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
