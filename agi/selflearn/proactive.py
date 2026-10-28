# -*- coding: utf-8 -*-
"""11/12 分享欲与主动表达候选：只生成候选，不自动发送。"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from . import db, initiative, memory, prompt_store, share_store, sharing, state, style_profile, trust
from .config import DB_PATH

COOLDOWN = 2 * 3600
MIN_SCORE = 0.45
DAILY_BUDGET = 6

# --------------------------------------------------------------------------- #
# P0-3：主动消息意图（4 类 + 可选低频 silence_ack），防止「查岗」模板塌缩
# --------------------------------------------------------------------------- #
KINDS = ("care", "share", "followup", "light", "silence_ack")
#: 轮换窗口：看最近 N 条主动消息的 kind 分布（proactive 表）
KIND_WINDOW = 20
#: 单一 kind 在窗口内占比上限（超过即降权/禁用，硬上限保证 ≤50%）
KIND_MAX_RATIO = 0.5
#: light 只在这些小时可用：8:00 ≤ hour < 22:00
LIGHT_HOURS = (8, 22)
#: silence_ack 至少间隔 7 天，且要求久未联系
SILENCE_ACK_GAP_S = 7 * 86400
SILENCE_MIN_IDLE_H = 24.0
#: care 只在低活跃时（用户 ≥1h 没说话）或上次情绪偏负
CARE_MIN_IDLE_H = 1.0
#: followup 要接「上次没聊完」：用户安静 ≥30 分钟，且上次对话在 7 天内
FOLLOWUP_MIN_IDLE_H = 0.5
FOLLOWUP_MAX_AGE_H = 7 * 24.0
#: 出站文本超过该字符数时按句末标点切分，避免长句粘连
LONG_TEXT_CHARS = 60
#: 中文/英文句末标点（已合规不再追加）
END_PUNCT = "。！？!?…～~"
#: 句尾是这些语气词 → 补问号，否则补句号
_QUESTION_TAILS = ("吗", "呢", "么", "嘛", "什么")
#: 通用短句/招呼，不当 followup 的「具体内容」
_STOP_CLAUSES = {
    "在吗", "在么", "在不在", "你好", "你好呀", "嗨", "hi", "hello",
    "嗯", "嗯嗯", "哦", "好", "好的", "行", "没事", "算了", "就那样",
    "没什么", "还行", "随便", "再说", "不用",
}
_STAGE_RANK = {"陌生": 0, "疏远": 0, "初识": 1, "熟悉": 2, "亲近": 3, "信任": 4}
_WS_RE = re.compile(r"\s+")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?])")
_CLAUSE_SPLIT_RE = re.compile(r"[，。！？!?；;,.、\s]+")
# 主题相似度用：去掉空白/标点，只留中英文数字（避免标点把 2-gram 稀释）
_SIM_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fff]+")

# --------------------------------------------------------------------------- #
# P0-8：反「安静」内容塌缩（禁元话题 / 主题去重 / 问句配额 / 后处理兜底）
# --------------------------------------------------------------------------- #
#: 正文禁词（真机日志里高频的「元话题」；命中即重写，保证线上绝不出现）
BANNED_TOPIC_WORDS = (
    "安静", "没动静", "没消息", "好久没聊", "忙吗", "在忙吗", "在干嘛",
    "冒个泡", "打扰了", "没人理我",
    # 同义改写 / 真机样例变体：一起拦，避免换个说法又冒出来
    "好久没", "没声音", "声音都没有", "一点声音", "冷清", "没人回",
)
#: 主题去重：最近 N 条已发主动消息、字符 2-gram Jaccard 上限
TOPIC_WINDOW = 10
TOPIC_SIMILARITY_MAX = 0.35
#: 问句配额：最近 N 条里 ≥M 条以问号结尾 → 本次强制陈述/分享句
QUESTION_WINDOW = 3
QUESTION_FORCE_AT = 2

# --------------------------------------------------------------------------- #
# P0-9：反编造共同经历（真机首条消息「便当盒」杜撰）
# --------------------------------------------------------------------------- #
#: 「共同经历断言」句式：正文命中后必须有真实锚点支撑（≥2 字重叠），否则视为编造。
#: 真实锚点 = 该次 tick 提供给模型的最近用户消息 / memory 话题 / followup 锚点。
SHARED_CLAIM_PATTERNS = (
    "你上回", "你上次", "你之前", "你以前", "你昨天", "你前天", "你那次",
    "你说过", "你提过", "你放在我这", "你放在我这儿", "你留在我这",
    "上次你", "还记得你", "我们上次", "我们之前", "我们以前",
    "你送我的", "你给我的",
)
#: 全局不编造约束（拼进每个 kind 的 prompt；followup 有真实锚点时例外）
GROUNDING_CONSTRAINTS = (
    "不编造约束（比具体更重要）：\n"
    "- 只能引用「记忆/最近聊天里真实出现过」的内容；没有依据时只讲你自己的事，"
    "不得声称对方做过什么；\n"
    "- 禁止编造和对方共同的经历，禁止这些句式：你上回…、上次你…、还记得你…、"
    "你说过要…、你放在我这的…；\n"
    "- 禁止杜撰具体的私人物品、共同住所、共同行程。"
)

# --------------------------------------------------------------------------- #
# P0-10：修 run-on 粘连句（真机「…剪了剪你这两天…」缺标点）
# --------------------------------------------------------------------------- #
#: run-on 判定（P0-10/P0-13）：
#: - 常规阈值：连续 ≥6 汉字无标点 + 紧跟「你/您/我」→ 切；
#: - 5 字档：只有末字属于「句末倾向」字符集（凉/累/了/好…）才切；
#: - 命中文末的防误伤规则（动宾/领属/量词）时一律不切，拿不准不切。
RUNON_MIN_CJK = 6
RUNON_TENDENCY_MIN_CJK = 5
RUNON_PRONOUNS = "你您我"
RUNON_BOUNDARIES = ("剪了剪", "了", "着", "过", "完", "好")
#: 「句末倾向」字集：P0-13 的 5 字档与 P0-19 的第三档**共用同一份**，避免漂移。
#: P0-13 原来只收状态/语气收尾；P0-19 扩充了可收句的形容词/动词（大/多/笑/写/想/看…）。
SENTENCE_END_CHARS = set(
    "了着过完好凉暖累困饿饱忙闲冷热疼酸痛渴烦闷爽脏湿干香臭甜苦咸"
    "多大久笑哭慌沉亮对错行成写想说看听快慢早晚满空")
#: 兼容旧名（同一份集合，不要新增第二份）
_RUNON_END_TENDENCY = SENTENCE_END_CHARS
#: 全局标点/分句约束（拼进每个 kind 的 prompt）
RUNON_CONSTRAINTS = (
    "标点/分句约束：\n"
    "- 一条消息只表达一件事，最多两句；句与句之间必须有标点（句号/问号）；\n"
    "- 不要一条消息连着说两件事，不要把两层意思粘在一起。"
)
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
#: 代词是宾语/介词宾语时（…跟你、…对你、…告诉你、…喜欢你）不能切
_RUNON_OBJECT_PREPOSITIONS = "跟对给和让把被替帮同与朝向等找陪问说想爱谢请叫诉欢道得白出现觉知望"
#: 代词前 2 字是这些心理/认知动词时（知道你在想什么）是动宾 → 不能切
_RUNON_OBJECT_VERBS = (
    "知道", "觉得", "明白", "看出", "发现", "希望", "担心", "心疼",
    "记得", "忘了", "认为", "感觉", "相信", "理解", "想起", "看到",
    "听见", "遇见", "喜欢", "告诉", "以为", "清楚", "确定", "懂",
)
#: 代词后以这些字眼开头 → 是「看你一眼/等你一会/问了你一个问题」这类动宾，不能切
_RUNON_TAIL_PHRASES = (
    "一眼", "一下", "一会", "一会儿", "两次", "一声", "几句",
    "一句", "一遍", "一次", "半天", "好久", "一个", "两个", "几个",
    "一", "两", "三", "几", "半", "个", "些", "点",
)

#: P0-18 第三档：感受/副词短语前缺标点（「…那么写心里有点感慨」）
RUNON_FEELING_WORDS = (
    "心里", "感觉", "觉得", "其实", "有点", "突然", "想着", "回想",
    "现在看", "现在", "越想越", "越想", "这时候", "后来",
    "估计", "大概", "似乎",
)
#: 该词前连续汉字至少这么长才切（真机「有点想笑心里」前段=4 字，故取 4）
RUNON_FEELING_MIN_FRONT = 4
#: 前一个字是代词/介词/常见动词时是同一句话，不能切
_RUNON_FEELING_BLOCK_PREV = set(
    "你您我他她它" + _RUNON_OBJECT_PREPOSITIONS + "得着看想说道感有")

# --------------------------------------------------------------------------- #
# P0-14：反「营销号」句式 + 轻度人格化边界
# --------------------------------------------------------------------------- #
#: 人格边界：允许数字生活/感受/观点/真实记忆；禁止物理起居/身体/天气/位置/出行
PERSONA_CONSTRAINTS = (
    "人格边界（轻度人格化，必须遵守）：\n"
    "- 只写「数字生活」：在读/在听/在整理资料/在学新东西/在想事情；"
    "可以写情绪倾向、对话题的看法、记忆里真实存在的事、观察到的现象；\n"
    "- 禁止物理身体与起居细节：做饭/吃饭/刷鞋/洗澡/睡觉体感/测血压/天气冷热/"
    "身体疼/地理位置/出行；\n"
    "- 不许用「刚+物理动作」当开头模板；\n"
    "- 每条必须带你自己的感受或想法（我在想/我觉得/有点…），不要写成流水账；\n"
    "- 允许短句；句式要有变化，不要连续几条都用同一种结构。"
)
#: 物理起居/身体/天气/位置/出行禁词（命中 → 换数字生活兜底句）
PHYSICAL_FORBIDDEN_WORDS = (
    "做饭", "煮", "炒", "蒸", "炖", "烤", "烧水", "洗碗", "刷碗", "刷鞋",
    "洗衣", "洗衣服", "洗澡", "洗漱", "洗脸", "刷牙", "洗手", "洗了",
    "吃饭", "吃了", "吃点", "喝完", "喝水", "喝点", "喝酒",
    "冰箱", "便当", "外卖", "买菜", "切菜", "切丝", "粥", "碗", "锅",
    "筷子", "厨房", "菜单", "超市", "菜市场", "饭",
    "睡觉", "睡了", "睡着", "起床", "躺下", "躺了", "被子", "枕头", "床",
    "卫生间", "厕所", "血压", "体检", "吃药", "医院", "药店",
    "出汗", "生病", "感冒", "发烧", "咳嗽", "伤口", "疼", "痛",
    "天气", "降温", "升温", "好冷", "好热", "冷得", "热得", "下雨", "下雪",
    "刮风", "晒太阳", "淋雨",
    "楼下", "窗外", "窗户", "窗台", "阳台", "小区", "绿萝", "花盆", "鞋",
    "衣服", "袜子", "书架", "抽屉", "柜子", "快递", "地铁", "公交",
    "打车", "开车", "出门", "路上", "逛街", "散步", "跑步", "爬山",
)
#: 自我感受/想法标记（每条消息至少要有一个）
_FEELING_MARKERS = (
    "我觉得", "我在想", "在想", "有点", "感觉", "心里", "挺", "好像",
    "希望", "担心", "开心", "难过", "感慨", "舒服", "累", "烦", "乱", "笑", "琢磨",
    "走神", "出神", "想了想", "想着", "惦记", "想聊", "想起", "记起", "突然想到",
    "想不起来", "可惜", "离谱", "无语", "上头", "服了", "笑死", "哈哈", "吵", "闷", "慌",
    "在整理", "在看", "在听",
)
#: 缺感受时的确定性前缀
_FEELING_PREFIXES = ("我有点想聊聊，", "说不上为什么，我觉得", "心里有点感慨，")

#: 开头模式池（P0-14）：时间/感受/想法/数字生活/直接陈述/对话式/回忆
OPENER_MODES = ("time", "feeling", "thought", "digital", "direct", "chat", "memory")
_OPENER_INSTRUCTIONS = {
    "time": "开头用时间感（这会儿/下午/最近），直接陈述，不要用「刚」开头。",
    "feeling": "开头先说你此刻的感受（有点累/挺开心/心里有点闷），不要用「刚」开头。",
    "thought": "开头用「突然想到/我最近在想」这类想法，不要用「刚」开头。",
    "digital": "开头写你的数字生活（在听/在看/在整理资料/在学东西），不要用「刚」开头。",
    "direct": "开头直接陈述一件事/一个观察，不要用「刚」开头。",
    "chat": "开头用对话式短句（说个事/跟你说），不要用「刚」开头。",
    "memory": "开头用「想起/记得」领起一件你自己的事，不要用「刚」开头。",
}
_OPENER_PREFIXES = {
    "time": ("这会儿，", "最近，", "今天，"),
    "feeling": ("我有点想聊聊，", "说不上为什么，我觉得", "说实话，"),
    "thought": ("突然想到，", "我最近在想，"),
    "digital": ("在整理东西的时候，", "在听歌的时候，"),
    "direct": ("跟你聊个观察，",),
    "chat": ("说个事，", "跟你说个事，", "跟你说，"),
    "memory": ("想起一件事，", "记起一个片段，"),
}
_OPENER_TIME_RE = re.compile(
    r"^(这会儿|现在|今天|昨天|最近|这几天|下午|晚上|早上|刚才)")
_OPENER_FEELING_RE = re.compile(
    r"^(有点|挺|感觉|心里|好像|说不上|我有点|我觉得|说实话)")
_OPENER_THOUGHT_RE = re.compile(r"^(突然想到|在想|我最近在想|琢磨|说起来)")
_OPENER_DIGITAL_RE = re.compile(r"^(在听|在看|在读|在整理|在学|在写|在翻|在查|最近在)")
_OPENER_CHAT_RE = re.compile(r"^(说个|跟你说|问个|讲个|聊个)")
_OPENER_MEMORY_RE = re.compile(r"^(想起|记起|记得|翻到)")
_QUOTED_RE = re.compile(r"「[^」]*」|“[^”]*”|\"[^\"]*\"")

#: 问句概率（P0-14）：base 0.25；energy/mood 高 → 最多 0.45，低 → 0.15
QUESTION_PROB_BASE = 0.08
QUESTION_PROB_MIN = 0.03
QUESTION_PROB_MAX = 0.13
#: 全局负向约束（拼进每个 kind 的 prompt；光靠轮换挡不住「安静」主题）
TOPIC_CONSTRAINTS = (
    "硬约束（违反会被重写，必须遵守）：\n"
    "- 正文禁止出现这些词及其同义改写：安静、没动静、没消息、好久没聊、"
    "忙吗/在忙吗、在干嘛、冒个泡、打扰了、没人理我；\n"
    "- 不要评论对方不回、不说话、冷清，不要把「沉默/没动静」当话题，沉默是正常的；\n"
    "- 不要写「窗外/声音/犯困/发呆/冷清」这类和对方沉默、无聊相关的环境感受；\n"
    "- 必须说具体内容：一件刚发生的小事 / 一个具体问题 / 一段记忆里的具体细节；"
    "禁止「最近怎么样」式泛问；\n"
    "- 不复述、不概括最近已发过的内容，换一个具体话题；\n"
    "- 最多一个问句，以陈述为主，像微信里随手发的一句话。"
)

#: 每类意图的生成约束（同一 LLM 调用里按 kind 选指令，禁止多类共用）

# P0-15：用户待办/进度追问
PROGRESS_CHECK_PROB = 0.25
PROGRESS_ASK_WINDOW_DAYS = 7
_PLAN_CUES = re.compile(
    r"要|打算|准备|想|计划|明天|后天|下周|下个月|这周|周末|过几天|月底|"
    r"在准备|在弄|马上|快要|即将")
_TODO_ANCHORS = (
    "面试", "搬家", "考试", "考证", "答辩", "入职", "体检", "手术", "看病",
    "出差", "旅行", "装修", "买房", "买车", "换工作", "减肥", "健身", "项目",
    "论文", "签证", "落户", "结婚", "生日", "转正", "答辩", "开学", "毕业",
)


def _todo_anchor(text: Any) -> str:
    value = str(text or "")
    positions = [(value.find(word), word) for word in _TODO_ANCHORS if word in value]
    if positions:
        return min(positions)[1]
    match = _PLAN_CUES.search(value)
    if not match:
        return ""
    tail = value[match.end():]
    tail = re.split(r"[，,。！!？?；;\n]", tail, maxsplit=1)[0]
    tail = re.sub(r"^(?:去|把|在|要|打算|准备|想|计划|要去做|得)", "", tail.strip())
    return tail.strip()[:12]


def find_user_todos(path: Path | str = DB_PATH, now: float | None = None, *,
                    max_age_days: int = 30, limit: int = 5) -> list[dict]:
    """从记忆/最近用户消息捞“用户说过要做的事”，按 anchor 7 天去重。"""
    now = time.time() if now is None else float(now)
    since = now - max(1, int(max_age_days)) * 86400
    todos: list[dict] = []
    seen_anchor: set[str] = set()
    try:
        con = db.connect(path)
        try:
            asked = {str(row[0] or "") for row in con.execute(
                "SELECT text FROM proactive WHERE created_at>=? AND kind IN "
                "('progress_check','followup')", (now - PROGRESS_ASK_WINDOW_DAYS * 86400,)).fetchall()}
            rows = con.execute(
                "SELECT id,content,created_at FROM user_memories WHERE status='active' "
                "AND lower(kind) IN ('plan','value') AND created_at>=? "
                "ORDER BY created_at DESC LIMIT 20", (since,)).fetchall()
            for row in rows:
                text = str(row[1] or "").strip()
                if not _PLAN_CUES.search(text):
                    continue
                anchor = _todo_anchor(text)
                if not anchor or anchor in seen_anchor:
                    continue
                if any(anchor in item for item in asked):
                    continue
                seen_anchor.add(anchor)
                todos.append({"text": text[:120], "source": "memory",
                              "msg_id": str(row[0] or ""), "ts": float(row[2] or 0),
                              "anchor": anchor})
            msgs = con.execute(
                "SELECT id,text,ts FROM messages WHERE role='user' AND ts>=? "
                "ORDER BY ts DESC LIMIT 40", (since,)).fetchall()
            for row in msgs:
                text = str(row[1] or "").strip()
                if not _PLAN_CUES.search(text):
                    continue
                anchor = _todo_anchor(text)
                if not anchor or anchor in seen_anchor:
                    continue
                if any(anchor in item for item in asked):
                    continue
                seen_anchor.add(anchor)
                todos.append({"text": text[:120], "source": "message",
                              "msg_id": str(row[0] or ""), "ts": float(row[2] or 0),
                              "anchor": anchor})
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return []
    todos.sort(key=lambda item: item["ts"], reverse=True)
    return todos[:max(0, int(limit))]

KIND_INSTRUCTION = {
    "care": "意图=关心近况：问一句具体的近况（工作/身体/心情里挑一个），"
            "不要泛泛说「最近怎么样」，不要查岗式连问。",
    "share": "意图=分享自己：说一件你自己的想法/感受，可以自然带出你记得的对方话题，"
             "不要客服腔、不要只提问。",
    "followup": "意图=接上次没聊完的事：必须提到最近对话里的具体内容（会给锚点），"
                "围绕它问一句或接一句，不要换成泛泛的近况。",
    "light": "意图=轻松一下：接个梗/调侃一句/讲件小趣事，短句，不查岗、不讲道理。",
    "silence_ack": "意图=久未联系后的轻打招呼：只说一句轻的，不追问、不解释、不查岗。",
    "progress_check": "意图=问用户自己说过要做的事的进度：必须引用用户原话里的具体锚点"
                      "（例如面试/搬家），问一句进度或结果；禁止泛泛「最近怎么样」，禁止编造。",
}

#: 每类意图的兜底句（LLM 不可用/跳过/跑题时使用；彼此不同，不允许多类共用）
#: P0-14：只写数字生活/感受，禁止物理起居细节；followup/share 的真实锚点用「」引用
_KIND_FALLBACK = {
    "care": "资料还没理完，有点烦",
    "share": "看到一个说法，挺有意思",
    "followup": "上次你说「{anchor}」，我一直惦记着",
    "light": "看到一个段子，笑死",
    "silence_ack": "翻到以前存的一篇文章，有点感慨",
    "progress_check": "你之前说的{anchor}，后来弄到哪一步了？",
}

#: 后处理兜底用的**数字生活/自我感受**安全句（P0-14：无物理细节、
#: 不含禁词、不编造共同经历、彼此不同）
_SAFE_FALLBACK = {
    "care": "资料还没理完，有点烦",
    "share": "看到一个说法，挺有意思",
    "followup": "想起以前的一个笔记，有点感慨",
    "light": "看到一个段子，笑死",
    "silence_ack": "翻到一篇文章，有点感慨",
    "progress_check": "你之前说的{anchor}，后来弄到哪一步了？",
}

#: P6-c：兜底池（≥8 句，避免每次落同一句）；按最近 5 条/7 天去重选择
_SAFE_FALLBACK_POOL = (
    "资料还没理完，有点烦",
    "看到一个说法，挺有意思",
    "刚整理完一批笔记，有点累",
    "翻到以前存的一篇文章，有点感慨",
    "今天效率一般，有点着急",
    "收藏夹里还有一堆没看的，慢慢来",
    "刚把一段代码跑通，挺有成就感",
    "看到一个段子，笑死",
    "那批资料终于归档了，轻松一点",
    "刚才试着换了个方法，居然成了",
)
_FALLBACK_CURSOR = {"value": 0}
_ACTIVE_DB_PATH: Path | str | None = None

#: followup 的主题去重变体：都保留锚点，避免「换话题」把具体内容换没了
_ANCHOR_VARIANTS = (
    "上次你说「{anchor}」，我这两天还一直想着这事。",
    "你之前提过「{anchor}」，我今天突然又想起这茬。",
    "关于「{anchor}」，我后来还琢磨了一下。",
)


def kind_fallback(kind: str, *, anchor: str = "", topic: str = "") -> str:
    """某类意图的兜底文本（各类互不相同）。"""
    template = _KIND_FALLBACK.get(str(kind or ""), _KIND_FALLBACK["care"])
    return template.format(anchor=str(anchor or "").strip(),
                           topic=str(topic or "").strip())


def _proactive_recent_texts(path: Path | str | None = None,
                            limit: int = 5, since: float | None = None) -> list[str]:
    """读最近 proactive 文本（用于兜底去重；失败返回空，不阻塞主链）。"""
    target = _ACTIVE_DB_PATH if path is None else path
    if target is None:
        target = DB_PATH
    con = None
    try:
        con = db.connect(target)
        if since is None:
            rows = con.execute("SELECT text FROM proactive ORDER BY created_at DESC LIMIT ?",
                               (int(limit),)).fetchall()
        else:
            rows = con.execute(
                "SELECT text FROM proactive WHERE created_at >= ? ORDER BY created_at DESC",
                (float(since),)).fetchall()
        return [str(row[0] or "") for row in rows]
    except Exception:  # noqa: BLE001
        return []
    finally:
        if con is not None:
            con.close()


def _final_safe(kind: str, anchor: str = "") -> str:
    if kind == "progress_check" and anchor:
        return _safe_fallback("progress_check", anchor=anchor)
    return _SAFE_FALLBACK.get(kind) or _SAFE_FALLBACK["care"]


def _pick_safe_fallback() -> str:
    """从 ≥8 句兜底池选：最近 5 条不重复、7 天内不重复，池空则放宽。"""
    now = time.time()
    recent = set(_proactive_recent_texts(limit=5))
    used7 = set(_proactive_recent_texts(since=now - 7 * 86400))
    candidates = [text for text in _SAFE_FALLBACK_POOL
                  if text not in recent and text not in used7]
    if not candidates:
        candidates = [text for text in _SAFE_FALLBACK_POOL if text not in recent]
    if not candidates:
        candidates = list(_SAFE_FALLBACK_POOL)
    cursor = int(_FALLBACK_CURSOR.get("value") or 0)
    _FALLBACK_CURSOR["value"] = cursor + 1
    return candidates[cursor % len(candidates)]


def _safe_fallback(kind: str, *, anchor: str = "", topic: str = "") -> str:
    """带锚点/话题的兜底句；静态兜底按最近 5 条/7 天去重，避免反复同一句。"""
    name = str(kind or "")
    text = kind_fallback(name, anchor=anchor, topic=topic)
    # care/light/silence_ack 的静态兜底走多样性池；有真实锚点/话题的 followup/share 保留模板
    if name in ("care", "light", "silence_ack"):
        return _pick_safe_fallback()
    if (name == "followup" and not str(anchor or "").strip()) or \
            (name == "share" and not str(topic or "").strip()):
        return _pick_safe_fallback()
    if _has_banned_topic(text):
        return _pick_safe_fallback()
    return text


def _has_banned_topic(text: Any) -> bool:
    """正文是否命中「安静/没动静/忙吗…」禁词。"""
    value = str(text or "")
    return any(word in value for word in BANNED_TOPIC_WORDS)


def _ends_question(text: Any) -> bool:
    return str(text or "").strip().endswith(("？", "?"))


def _bigrams(text: Any) -> set[str]:
    value = _SIM_CLEAN_RE.sub("", str(text or "")).replace("_", "")
    if len(value) < 2:
        return set()
    return {value[index:index + 2] for index in range(len(value) - 1)}


def topic_overlap(first: Any, second: Any) -> float:
    """两段文本的字符 2-gram Jaccard（0~1）。"""
    left, right = _bigrams(first), _bigrams(second)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def max_topic_overlap(text: Any, history: list[str]) -> float:
    return max((topic_overlap(text, old) for old in list(history or [])), default=0.0)


def _has_shared_experience_claim(text: Any) -> bool:
    """正文是否出现「你上回/上次你/还记得你…」这类共同经历断言。"""
    value = str(text or "")
    return any(pattern in value for pattern in SHARED_CLAIM_PATTERNS)


#: P0-11：断言后截取的 claim 长度上限（到标点为止）
CLAIM_MAX_CHARS = 12
#: P0-11：claim 里必须存在连续 ≥N 字的子串出现在 anchors 中才算有依据
CLAIM_MIN_MATCH = 3
#: claim 截取到这些字符就停（标点/空白）
_CLAIM_STOP_CHARS = "。！？!?；;，,、：:…\n "


def _claim_after(text: str, start: int) -> str:
    """取 pattern 结束位置之后最多 12 个字（到标点/空白为止）作为 claim。"""
    segment = text[start:start + CLAIM_MAX_CHARS]
    for index, char in enumerate(segment):
        if char in _CLAIM_STOP_CHARS:
            return segment[:index]
    return segment


def _claim_grounded(claim: str, anchors: list[str]) -> bool:
    """claim 里是否存在连续 ≥3 字子串出现在 anchors 任一条中。"""
    length = len(claim)
    if length < CLAIM_MIN_MATCH:
        return False
    for index in range(length - CLAIM_MIN_MATCH + 1):
        piece = claim[index:index + CLAIM_MIN_MATCH]
        if any(piece in anchor for anchor in anchors):
            return True
    return False


def grounding_ok(text: Any, anchors) -> bool:
    """反编造校验（P0-11 收紧）：共同经历断言必须能被真实锚点支撑。

    判定：
    - 没有共同经历断言 → 直接通过（只讲自己的事/中性事实）；
    - 对**每个**命中的断言 pattern，取它后面最多 12 字（到标点为止）= claim；
      claim 里必须存在**连续 ≥3 字**子串出现在 anchors 任一条中才放行；
      claim 长度 <3 → 无法验证 → 判编造（保守）；
    - 多个断言全部通过才算通过（任一编造即拦）；
    - ``anchors`` 为空时，任何共同经历断言都视为编造。
    """
    value = str(text or "").strip()
    if not value:
        return True
    hits: list[int] = []
    for pattern in SHARED_CLAIM_PATTERNS:
        start = 0
        while True:
            index = value.find(pattern, start)
            if index < 0:
                break
            hits.append(index + len(pattern))
            start = index + 1
    if not hits:
        return True
    anchor_list = [str(item).strip() for item in list(anchors or [])
                   if str(item or "").strip()]
    if not anchor_list:
        return False
    for claim_start in sorted(hits):
        if not _claim_grounded(_claim_after(value, claim_start), anchor_list):
            return False
    return True


def _grounding_anchors(path: Path | str, candidate: dict | None = None) -> list[str]:
    """该次 tick 提供给模型的真实上下文：最近用户消息 + memory 话题/证据 + followup 锚点。"""
    anchors: list[Any] = []
    if isinstance(candidate, dict):
        anchors.append(candidate.get("anchor"))
        anchors.append(candidate.get("topic"))
    try:
        memories = memory.list_user_memories(path, 20)
    except Exception:  # noqa: BLE001
        memories = []
    for item in memories:
        anchors.append(item.get("content"))
        anchors.append(item.get("evidence"))
    try:
        recent = memory.recent_messages(path, limit=40)
    except Exception:  # noqa: BLE001
        recent = []
    for msg in recent:
        if msg.get("role") != "user":
            continue
        anchors.append(msg.get("text"))
    cleaned: list[str] = []
    for item in anchors:
        text = str(item or "").strip()
        if not text or text.startswith("["):  # 媒体占位不算真实内容
            continue
        cleaned.append(text)
    return cleaned


def _recent_proactive_texts(path: Path | str, limit: int = TOPIC_WINDOW) -> list[str]:
    """最近 N 条**已发**主动消息正文（新→旧）。

    优先读 worker 写的 ``proactive_events``（status='sent'，真正出站过）；
    表还不存在/没记录时退回 ``proactive`` 表（tick 自己生成的历史），
    这样纯 tick 场景（单测、网页预览）也有去重依据。
    """
    con = db.connect(path)
    try:
        try:
            rows = con.execute(
                "SELECT text FROM proactive_events WHERE status='sent' "
                "ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        except Exception:  # noqa: BLE001 - 旧库没有 proactive_events 表
            rows = []
        if not rows:
            rows = con.execute(
                "SELECT text FROM proactive WHERE status!='rejected' "
                "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        con.close()
    return [str(row["text"] or "") for row in rows if str(row["text"] or "").strip()]


def sanitize_topic(text: Any, kind: str = "care") -> str:
    """后处理兜底：命中禁词就换成该 kind 的安全兜底句（发送前最后一道闸）。

    保证线上绝不出现「安静/没动静/忙吗/冒个泡…」这类元话题；空文本同样兜底。
    """
    value = str(text or "").strip()
    if value and not _has_banned_topic(value):
        return value
    name = str(kind or "").strip()
    return _SAFE_FALLBACK.get(name) or _SAFE_FALLBACK["care"]


def _bigram_containment(reference: Any, text: Any) -> float:
    """reference 的 2-gram 有多大比例出现在 text 里（判断近重复/复读）。"""
    ref = str(reference or "")
    value = str(text or "")
    ref_grams = {ref[i:i + 2] for i in range(len(ref) - 1)}
    value_grams = {value[i:i + 2] for i in range(len(value) - 1)}
    if not ref_grams:
        return 0.0
    return len(ref_grams & value_grams) / len(ref_grams)


def _low_overlap_fallback(kind: str, candidate: dict,
                          recent: list[str]) -> str:
    """主题仍超标时：从兜底句里挑与历史重叠最低的（followup 保留锚点）。"""
    anchor = str(candidate.get("anchor") or "").strip()
    topic = str(candidate.get("topic") or "").strip()
    if kind == "followup" and anchor:
        pool = [variant.format(anchor=anchor) for variant in _ANCHOR_VARIANTS]
    else:
        pool = [_safe_fallback(other, anchor=anchor, topic=topic) for other in KINDS]
        pool.append(_safe_fallback(kind, anchor=anchor, topic=topic))
    unique = list(dict.fromkeys(pool))
    safe = [text for text in unique if not _has_banned_topic(text)] or unique
    return min(safe, key=lambda text: max_topic_overlap(text, recent))



def _daily_budget(path: Path | str) -> int:
    """日预算：优先读 autonomy 配置；<=0 表示不限。"""
    try:
        from . import autonomy
        value = autonomy._config(path).get("daily_budget", DAILY_BUDGET)
        return int(value)
    except Exception:  # noqa: BLE001
        return DAILY_BUDGET


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def _last_interaction(path: Path | str) -> float:
    con = db.connect(path)
    try:
        # 只看"人"最后说话的时间：自己刚发过的主动消息不算"刚聊过"，
        # 这样 LLM 想连发时不会被 8 秒静默挡住。
        row = con.execute("SELECT max(ts) FROM messages WHERE role='user'").fetchone()
        return float(row[0] or 0) if row else 0.0
    finally:
        con.close()


def _cooldown_ok(path: Path | str, now: float) -> bool:
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive").fetchone()
        return not row or not row[0] or now - float(row[0]) >= COOLDOWN
    finally:
        con.close()


def _idle_hours(path: Path | str, now: float) -> float:
    last = _last_interaction(path)
    return (now - last) / 3600 if last else 99.0


def _followup_anchor(path: Path | str, now: float, limit: int = 40) -> str:
    """从最近对话里取一段「具体内容」当 followup 的引用锚点（不要空泛招呼）。

    limit 放大到 40：主动消息自己也会不断写进 messages 表，窗口太小会把
    「上次用户说的话」挤出视野，followup 就退化没了。
    """
    try:
        recent = memory.recent_messages(path, limit=limit)
    except Exception:  # noqa: BLE001
        return ""
    for msg in reversed(recent):
        if msg.get("role") != "user":
            continue
        ts = float(msg.get("ts") or 0)
        if ts and (now - ts) > FOLLOWUP_MAX_AGE_H * 3600:
            continue
        text = str(msg.get("text") or "").strip()
        if not text or text.startswith("["):  # [图片]/[表情包] 不算具体内容
            continue
        for clause in _CLAUSE_SPLIT_RE.split(text):
            clause = clause.strip()
            if len(clause) >= 2 and clause not in _STOP_CLAUSES and not clause.isdigit():
                if _has_banned_topic(clause):  # 锚点本身不能带「安静」类禁词
                    continue
                return clause[:20]
    return ""


def _kind_counts(path: Path | str, limit: int = KIND_WINDOW) -> dict[str, int]:
    """最近 N 条主动消息的 kind 分布（proactive 表）。"""
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT kind FROM proactive ORDER BY created_at DESC LIMIT ?",
            (int(limit),)).fetchall()
    finally:
        con.close()
    counts: dict[str, int] = {}
    for row in rows:
        kind = str(row["kind"] or "").strip()
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _last_proactive_kind(path: Path | str) -> str:
    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT kind FROM proactive ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        con.close()
    return str(row["kind"] or "") if row else ""


def _last_kind_ts(path: Path | str, kind: str) -> float:
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive WHERE kind=?",
                          (str(kind),)).fetchone()
    finally:
        con.close()
    return float(row[0] or 0) if row and row[0] else 0.0


def build_candidates(path: Path | str = DB_PATH, now: float | None = None) -> list[dict]:
    """按 4+1 类意图生成候选；每类有自己的触发条件与兜底句。"""
    now = time.time() if now is None else float(now)
    st = state.load(path)
    own = st["self"]
    mood = float(own.get("mood", 0.55))
    share_drive = float(own.get("sharing", 0.45))
    emotion = str((st.get("emotion") or {}).get("label") or "")
    trust_value = trust.get(path)
    stage = trust.stage(trust_value)
    idle_h = _idle_hours(path, now)
    hour = time.localtime(now).tm_hour
    try:
        mems = memory.list_user_memories(path, 10)
    except Exception:  # noqa: BLE001
        mems = []
    topic = str(mems[0].get("content") or "").strip() if mems else ""
    candidates: list[dict] = []

    # care：低活跃 / 上次情绪偏负
    if idle_h >= CARE_MIN_IDLE_H or emotion == "negative":
        candidates.append({
            "kind": "care", "text": _safe_fallback("care"),
            "reason": f"已 {idle_h:.1f} 小时没互动" if idle_h >= CARE_MIN_IDLE_H
                      else "上次情绪偏负",
            "base": 0.55,
        })
    # share：有记忆话题或关系够近，分享自己的想法
    if topic or trust_value >= 0.55:
        candidates.append({
            "kind": "share",
            "text": _safe_fallback("share", topic=topic or "最近的事"),
            "reason": "记忆里有可接回的话题" if topic else "信任足够，适合分享自己的念头",
            "base": 0.50, "topic": topic,
        })
    # followup：接上次没聊完的具体内容（必须能引用到锚点）
    anchor = _followup_anchor(path, now)
    if anchor and idle_h >= FOLLOWUP_MIN_IDLE_H:
        candidates.append({
            "kind": "followup", "text": _safe_fallback("followup", anchor=anchor),
            "reason": f"上次聊到「{anchor}」", "base": 0.52, "anchor": anchor,
        })
    # progress_check：只在捞到用户待办时出现，且低概率（≤25%）
    try:
        todos = find_user_todos(path, now, limit=3)
    except Exception:  # noqa: BLE001
        todos = []
    if todos:
        import random as _random
        roll = _random.Random(f"{path}|{int(now // 1800)}").random()
        if roll < PROGRESS_CHECK_PROB:
            todo = todos[0]
            candidates.append({
                "kind": "progress_check",
                "text": _safe_fallback("progress_check", anchor=todo["anchor"]),
                "reason": f"用户说过「{todo['anchor']}」，可问进度",
                "base": 0.42, "anchor": todo["anchor"], "todo": todo,
            })
    # light：仅白天且关系 ≥ 熟悉
    if LIGHT_HOURS[0] <= hour < LIGHT_HOURS[1] and _STAGE_RANK.get(stage, 0) >= 2:
        candidates.append({
            "kind": "light", "text": _safe_fallback("light"),
            "reason": "白天且关系够近，适合轻松接梗", "base": 0.45,
        })
    # silence_ack：久未联系 + 至少 7 天没发过
    last_ack = _last_kind_ts(path, "silence_ack")
    if idle_h >= SILENCE_MIN_IDLE_H and (not last_ack or now - last_ack >= SILENCE_ACK_GAP_S):
        candidates.append({
            "kind": "silence_ack", "text": _safe_fallback("silence_ack"),
            "reason": f"已 {idle_h:.1f} 小时没联系", "base": 0.40,
        })
    if not candidates:
        # 关系还远/刚从沉默里出来：先轻轻问一句，别硬分享
        candidates.append({
            "kind": "care", "text": _safe_fallback("care"),
            "reason": "没有特别理由，只是想轻轻问一句", "base": 0.35,
        })
    for candidate in candidates:
        candidate["score"] = round(min(0.95, candidate["base"] + 0.1 * trust_value), 3)
        candidate.setdefault("anchor", "")
        candidate.setdefault("topic", topic)
    return sorted(candidates, key=lambda item: (-item["score"], item["kind"]))


def choose_candidate(candidates: list[dict], path: Path | str = DB_PATH,
                     now: float | None = None, *,
                     drive: float | None = None) -> dict | None:
    """加权反重复选 kind：窗口内计数少的优先、同计数比分数，超 50% 硬排除。

    - 反重复：最近 ``KIND_WINDOW`` 条里该 kind 出现次数最少的优先（并列才比分数），
      某 kind 占比 >50% 时直接排除（除非没有别的可选）——强制降权落到「选不上」；
    - 同类不连发：上一轮刚发过的 kind 在并列时排到最后（silence_ack 因此不会连续出现）；
    - 选择是确定性的（同状态同历史落在同一 kind），避免测试随机抖动；文本仍由 LLM 生成。
    """
    if not candidates:
        return None
    # P33：drive 透传（worker 传入）→ 低 drive 只留 light/progress_check，
    # 高 drive 分享类权重 ×1.5、light ×0.7；不传 drive 时行为与旧版完全一致。
    weights: dict[str, float] = {}
    if drive is not None:
        try:
            from . import initiative as _initiative
            allowed = _initiative.allowed_kinds(drive)
            if allowed is not None:
                filtered = [c for c in candidates
                            if str(c.get("kind") or "") in allowed]
                if filtered:
                    candidates = filtered
            weights = {str(c.get("kind") or ""):
                       _initiative.kind_weight(c.get("kind"), drive)
                       for c in candidates}
        except Exception:  # noqa: BLE001 - 权重失败退回原行为
            weights = {}
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    now = time.time() if now is None else float(now)
    counts = _kind_counts(path)
    total = sum(counts.values())
    last_kind = _last_proactive_kind(path)
    # 硬上限：若选它会让窗口内占比超过 KIND_MAX_RATIO，先排除
    eligible: list[dict] = []
    for candidate in candidates:
        kind = str(candidate.get("kind") or "")
        count = int(counts.get(kind, 0))
        if total and (count + 1) / (total + 1) > KIND_MAX_RATIO + 1e-9:
            continue
        eligible.append(candidate)
    if not eligible:
        eligible = list(candidates)
    # 反重复：计数最少的 kind 优先；并列按分数，再按轮转起点避免永远同一顺序
    least = min(int(counts.get(str(c.get("kind") or ""), 0)) for c in eligible)
    pool = [c for c in eligible
            if int(counts.get(str(c.get("kind") or ""), 0)) == least]
    offset = (total + int(now)) % len(pool)
    ordered = list(pool[offset:]) + list(pool[:offset])
    # 上一轮同 kind 在并列时排后面（不连发）
    ordered.sort(key=lambda c: 0 if str(c.get("kind") or "") != last_kind else 1)
    return max(ordered, key=lambda c: float(c.get("score") or 0.0)
               * (weights.get(str(c.get("kind") or ""), 1.0) if weights else 1.0))


def _find_runon_feeling(value: str) -> int | None:
    """第三档：感受/副词短语（心里/觉得/有点…）前缺标点时的切分点。"""
    best: int | None = None
    for word in RUNON_FEELING_WORDS:
        start = 0
        while True:
            index = value.find(word, start)
            if index < 0:
                break
            start = index + 1
            if index == 0:
                continue
            count = 0
            cursor = index - 1
            while cursor >= 0 and _CJK_CHAR_RE.match(value[cursor]):
                count += 1
                cursor -= 1
            if count < RUNON_FEELING_MIN_FRONT:
                continue
            if value[index - 1] in _RUNON_FEELING_BLOCK_PREV:
                continue
            # P0-19：前段末字必须「像句末」（动词/形容词/语气收尾）；
            # 名词/量词/代词结尾（案/情/法/子/事/话…）说明还是半句话 → 不切。
            if value[index - 1] not in SENTENCE_END_CHARS:
                continue
            if word == "有点":
                run = value[index - count:index]
                if run[-1:] in set("情事话子案法种点"):
                    continue
                if re.search(r"(这|那)[一二两三四五六七八九十几]?$", run):
                    continue
            rest = value[index + len(word):]
            if not rest.strip(" 。！？!?...\n\t"):
                continue  # 词后面没内容（句尾），不切
            if best is None or index < best:
                best = index
    return best


def find_runon(text: Any) -> int | None:
    """定位 run-on 粘连句的切分点（在代词前插入句号的位置）。

    规则（P0-10/P0-13）：
    - 找一段**连续 ≥RUNON_TENDENCY_MIN_CJK 个汉字且中间无标点**、且紧跟着
      「你/您/我」的片段；
    - 常规阈值（≥6 字）直接切；5 字档只在**片段末字属于「句末倾向」**
      （凉/累/了/好/暖…）时才切——避免把「我刚吃完饭你吃了吗」这类切断；
    - 防误伤：代词是宾语/介词宾语（跟你/对你/告诉你/喜欢你…）、代词后接
      「一眼/一下/一会儿/一个/半天…」、代词后 3 字内有「的」领属、或代词后
      不是一小段像样分句（<3 汉字）→ 都不切；
    - 切分点 = 该片段内最后一个动词边界（了/着/过/完/好，或「剪了剪」这类
      动词重叠）之后；若边界不在片段末尾（后面还有补语/量词），切在片段末尾、
      代词之前；没有动词边界时同样切在代词之前。

    返回插入「。」的绝对位置（int），没检测到返回 None。纯函数。
    """
    value = str(text or "")
    if not value:
        return None
    pronoun_split: int | None = None
    for index, char in enumerate(value):
        if char not in RUNON_PRONOUNS:
            continue
        count = 0
        cursor = index - 1
        while cursor >= 0 and _CJK_CHAR_RE.match(value[cursor]):
            count += 1
            cursor -= 1
        if count < RUNON_TENDENCY_MIN_CJK:
            continue
        run_start = index - count
        run = value[run_start:index]
        # 5 字档必须收在一个「句末倾向」字上（拿不准不切）；≥6 字正常切
        if count < RUNON_MIN_CJK and run[-1] not in _RUNON_END_TENDENCY:
            continue
        # 防误伤：代词是介词/动词宾语（…跟你、…告诉你、…喜欢你）→ 同一句话
        if run and run[-1] in _RUNON_OBJECT_PREPOSITIONS:
            continue
        # 防误伤：代词前 2 字是心理/认知动词（我知道你在想什么）→ 动宾，不切
        if value[max(0, index - 2):index] in _RUNON_OBJECT_VERBS:
            continue
        # 防误伤：代词后接「一眼/一下/一会儿/半天…」→ 动宾短语
        if str(value[index + 1:]).startswith(_RUNON_TAIL_PHRASES):
            continue
        # 防误伤：代词后 3 字内出现「的」→ 领属/关系从句（你推荐的店、你说的那事）
        if "的" in value[index + 1:index + 4]:
            continue
        # 防误伤：代词后面必须还有一小段像样的分句（≥3 个汉字），
        # 否则「…都告诉了你」这种单句会被错误切开。
        tail = 0
        for char in value[index + 1:]:
            if not _CJK_CHAR_RE.match(char):
                break
            tail += 1
        if tail < 3:
            continue
        boundary_end = None
        for token in RUNON_BOUNDARIES:
            pos = run.rfind(token)
            if pos < 0:
                continue
            token_end = pos + len(token)
            if boundary_end is None or token_end > boundary_end:
                boundary_end = token_end
        # 边界必须在片段末尾（后面没有补语/量词）才在边界后切；否则切在代词前
        if boundary_end is not None and boundary_end == len(run):
            pronoun_split = run_start + boundary_end
        else:
            pronoun_split = run_start + count
        break
    feeling_split = _find_runon_feeling(value)
    if pronoun_split is not None and (feeling_split is None or pronoun_split <= feeling_split):
        return pronoun_split
    return feeling_split


def _insert_runon_periods(text: Any) -> str:
    """确定性修复 run-on：在 find_runon 定位处插入「。」，只加标点、不丢内容。"""
    value = str(text or "")
    offset = 0
    guard = 0
    while guard < 8:  # 防御：超长/异常文本也不会死循环
        pos = find_runon(value[offset:])
        if pos is None:
            break
        split_at = offset + pos
        if split_at <= offset:  # pragma: no cover - 防御
            break
        value = value[:split_at] + "。" + value[split_at:]
        offset = split_at + 1
        guard += 1
    return value


def polish_outbound(text: Any) -> str:
    """出站文本质检（发送前）：去首尾空白、合并连续空白、补句末标点、修 run-on、超长按标点切分。

    规则：
    - 句尾缺标点：以「吗/呢/么/嘛」结尾补问号，其余陈述补句号；
    - run-on 粘连（连续 ≥6 汉字，或 5 字且末字为「句末倾向」，后接你/您/我）
      → 在动词边界后确定性插入「。」，只加标点、不丢内容；
    - 超过 ``LONG_TEXT_CHARS`` 且有多句时，按句末标点切分并用换行分隔（发送层逐段发），
      切分只加分隔符、不丢字；
    - 已合规文本（无多余空白、已有句末标点、无 run-on、未超长）**逐字节不变**；
      修复幂等（对已修好的文本再调用不变）。

    纯函数：不读文件、不依赖时间/随机。
    """
    value = _WS_RE.sub(" ", str(text or "")).strip()
    if not value:
        return ""
    # 2026-10-28 用户反馈：不要用「」方括号引号
    try:
        from ..textguard import strip_corner_brackets
        value = strip_corner_brackets(value)
    except Exception:  # noqa: BLE001
        pass
    # 2026-10-28 用户反馈：不要「账/算账」比喻（2026-10-29 接到主动消息路径）
    try:
        from .guard import ledger_check, strip_ledger_clauses
        if ledger_check(value).get("hit"):
            value = strip_ledger_clauses(value)
    except Exception:  # noqa: BLE001 - guard 不可用不影响发送
        pass
    # 2026-10-28 用户反馈：不要再说「想起你说…」这种句式
    for _bad, _good in (("想起你之前说过", "你之前说过"),
                        ("想起你说", "你说"),
                        ("我突然想起", "我想起")):
        value = value.replace(_bad, _good)
    # P0-20：发送前兜底删除元话语（「接着刚才说的/刚才说的/前面提到…」）
    value = strip_metadiscourse(value).strip()
    if not value:
        return ""
    # P0-21：去动作汇报开头 + 收掉叠加前缀 + 单主题 + 句尾不加句号
    value = strip_action_opener(value)
    value = collapse_leading_prefixes(value)
    value = strip_embedded_action_prefixes(value)
    value = fix_single_topic(value).strip()
    value = clean_chat_punctuation(value)
    value = normalize_particles(value)
    if not value:
        return ""
    if (value[-1] not in END_PUNCT and value.endswith(_QUESTION_TAILS)
            and not value.endswith(("咋说呢", "怎么说呢", "嗯呢"))):
        value += "？"          # 只有真提问才补问号；陈述默认不加任何标点
    value = _insert_runon_periods(value)
    if len(value) > LONG_TEXT_CHARS:
        parts = [part.strip() for part in _SENT_SPLIT_RE.split(value) if part.strip()]
        if len(parts) > 1:
            value = "\n".join(parts)
    return strip_trailing_period(value)


# --------------------------------------------------------------------------- #
# P0-14：人格边界 / 开头多样 / 问句概率 / 自我感受（纯函数，便于测试）
# --------------------------------------------------------------------------- #
def question_probability(mood: float, energy: float) -> float:
    """问句概率：energy/mood 高 → 最多 0.45，低 → 0.15，中段线性（base≈0.25）。"""
    try:
        score = (float(mood) + float(energy)) / 2
    except (TypeError, ValueError):
        score = 0.55
    score = max(0.0, min(1.0, score))
    if score <= 0.35:
        return QUESTION_PROB_MIN
    if score >= 0.65:
        return QUESTION_PROB_MAX
    span = QUESTION_PROB_MAX - QUESTION_PROB_MIN
    return round(QUESTION_PROB_MIN + span * (score - 0.35) / 0.30, 3)


def should_ask_question(mood: float, energy: float,
                        recent_texts: list[str] | None = None) -> bool:
    """本次是否允许提问：最近 3 条已有任一问句 → 禁问；否则按 q_prob 掷骰。

    首条（没有任何主动历史）允许提问，避免随机把开场白全变成陈述。
    """
    last = [str(item or "") for item in list(recent_texts or [])[:QUESTION_WINDOW]
            if str(item or "").strip()]
    if not last:
        return True
    if any(_ends_question(item) for item in last):
        return False
    return random.random() < question_probability(mood, energy)


def classify_opener(text: Any) -> str:
    """判断一条消息的开头模式（just 是历史坏习惯，不进模式池）。"""
    value = str(text or "").strip()
    if not value:
        return "direct"
    if value.startswith("刚"):
        return "just"
    if _OPENER_TIME_RE.match(value):
        return "time"
    if _OPENER_FEELING_RE.match(value):
        return "feeling"
    if _OPENER_THOUGHT_RE.match(value):
        return "thought"
    if _OPENER_DIGITAL_RE.match(value):
        return "digital"
    if _OPENER_CHAT_RE.match(value):
        return "chat"
    if _OPENER_MEMORY_RE.match(value):
        return "memory"
    return "direct"


def choose_opener_mode(recent_texts: list[str] | None = None) -> str:
    """从模式池里挑本次开头：不用上一条同模式、优先用得最少的（确定性轮转）。"""
    history = [classify_opener(item) for item in list(recent_texts or [])
               if str(item or "").strip()]
    last = history[0] if history else ""
    counts = {mode: 0 for mode in OPENER_MODES}
    for mode in history:
        if mode in counts:
            counts[mode] += 1
    pool = [mode for mode in OPENER_MODES if mode != last] or list(OPENER_MODES)
    least = min(counts[mode] for mode in pool)
    candidates = [mode for mode in pool if counts[mode] == least]
    return random.choice(candidates)


def opener_problem(text: Any, target_mode: str,
                   recent_texts: list[str] | None = None) -> bool:
    """开头是否违规：以「刚」开头（超过 20% 配额/不是目标模式）、或与上一条同模式。"""
    value = str(text or "").strip()
    if not value:
        return False
    actual = classify_opener(value)
    history = [classify_opener(item) for item in list(recent_texts or [])
               if str(item or "").strip()]
    last = history[0] if history else ""
    if actual == "just":
        if target_mode != "just":
            return True
        just_count = sum(1 for mode in history if mode == "just")
        if (just_count + 1) / (len(history) + 1) > 0.20:
            return True
    if actual == last and target_mode != last:
        return True
    return False


def vary_opener(text: Any, target_mode: str,
                recent_texts: list[str] | None = None) -> str:
    """确定性换开头：去掉「刚」前缀 + 换成目标模式前缀（不改内容）；已合规则原样。"""
    value = str(text or "").strip()
    if not value:
        return value
    mode = target_mode if target_mode in OPENER_MODES else "direct"
    if not opener_problem(value, mode, list(recent_texts or [])):
        return value
    prefixes = _OPENER_PREFIXES.get(mode) or _OPENER_PREFIXES["direct"]
    prefix = prefixes[len(value) % len(prefixes)]
    # P0-18 A：正文已带任何开头模式前缀时先摘掉，避免重复前缀
    matched = _match_existing_prefix(value)
    if matched:
        value = value[len(matched):].lstrip("，,；;、 ")
    elif value.startswith("刚刚"):
        value = value[2:]
    elif value.startswith("刚"):
        value = value[1:]
    value = value.lstrip("，,；; ")
    if not value:
        return prefix.rstrip("，") + "。"
    return prefix + value


def ensure_opener_distinct(text: Any, recent_texts: list[str] | None,
                           fallback_mode: str = "direct") -> str:
    """保证开头模式与上一条不同（确定性尝试所有非动作模式）。"""
    value = str(text or "").strip()
    history = [str(item or "") for item in list(recent_texts or [])[:1]
               if str(item or "").strip()]
    if not history:
        return value
    previous = classify_opener(history[0])
    if classify_opener(value) != previous:
        return value
    modes = [mode for mode in OPENER_MODES if mode not in (previous, "digital")]
    if fallback_mode in modes:
        modes.remove(fallback_mode)
        modes.insert(0, fallback_mode)
    for mode in modes:
        candidate = vary_opener(value, mode, recent_texts)
        if classify_opener(candidate) != previous:
            return candidate
    for prefixes in _OPENER_PREFIXES.values():
        prefix = prefixes[0]
        candidate = prefix + value
        if classify_opener(candidate) != previous:
            return candidate
    return value


def has_self_feeling(text: Any) -> bool:
    """消息里有没有自我感受/想法（P0-14：禁止纯流水账）。"""
    value = str(text or "")
    return any(marker in value for marker in _FEELING_MARKERS)


def find_physical_detail(text: Any) -> str | None:
    """检测物理起居/身体/天气/位置/出行细节；引用对方/记忆（「」内或断言之后）的除外。"""
    value = str(text or "")
    if not value:
        return None
    stripped = _QUOTED_RE.sub("", value)
    # 2026-10-28：模板不再用「」包 anchor（用户要求），改成自然引述前缀；
    # 这些前缀后面的物理词属于"对方/记忆里的内容"，不算小鱼自己的起居细节。
    claim_hints = SHARED_CLAIM_PATTERNS + (
        "你说的", "你之前说", "你上次说", "你说要", "上次你说", "你说",
        "你之前提过", "你提过", "关于", "上次聊到", "刚看到", "跟")
    for sentence in re.split(r"[。！？!?；;\n]+", stripped):
        if not sentence:
            continue
        claim_pos = None
        for pattern in claim_hints:
            pos = sentence.find(pattern)
            if pos >= 0 and (claim_pos is None or pos < claim_pos):
                claim_pos = pos
        for word in PHYSICAL_FORBIDDEN_WORDS:
            index = sentence.find(word)
            if index < 0:
                continue
            if claim_pos is not None and claim_pos < index:
                continue  # 引用对方/记忆里的内容，不是自己的物理细节
            return word
    return None


def _clause_count(text: Any) -> int:
    """粗略分句数（按句末标点切），用于判断连续几条是否同一句式。"""
    parts = [part.strip() for part in re.split(r"[。！？!?]+", str(text or ""))]
    return len([part for part in parts if part]) or 1


def _shape_problem(text: Any, recent_texts: list[str] | None = None) -> bool:
    """连续 3 条分句数相同 → 本次要换结构（P0-14 句式波动）。"""
    counts = [_clause_count(item) for item in list(recent_texts or [])[:3]]
    return len(counts) == 3 and counts[0] == counts[1] == counts[2] == _clause_count(text)


_SHAPE_TAILS = ("，越想越有点感慨", "，说起来挺有意思", "，有点说不上来", "，反正挺烦的")


def _vary_structure(text: Any) -> str:
    """换结构：多句 → 收成短句；单句 → 补一小句情绪尾（变成两句）。"""
    value = str(text or "").strip()
    parts = [part.strip() for part in re.split(r"[。！？!?]+", value) if part.strip()]
    if len(parts) > 1:
        return parts[0] + "。"
    if not value:
        return value
    return value.rstrip("。！？!?") + _SHAPE_TAILS[len(value) % len(_SHAPE_TAILS)]


def _style_problem(text: Any, opener_mode: str, recent_texts: list[str] | None,
                   allow_question: bool) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if not allow_question and _ends_question(value):
        return True
    if find_physical_detail(value):
        return True
    if not has_self_feeling(value):
        return True
    if _shape_problem(value, recent_texts):
        return True
    return opener_problem(value, opener_mode, recent_texts)


def _style_postprocess(text: Any, kind: str, opener_mode: str,
                       recent_texts: list[str] | None,
                       allow_question: bool) -> str:
    """风格违规的确定性收口：禁问→陈述兜底、物理→数字生活兜底、缺感受→补前缀、开头→换前缀、同句式→换结构。"""
    value = str(text or "").strip()
    if not value:
        value = _safe_fallback(kind)
    if not allow_question and _ends_question(value):
        value = _safe_fallback(kind)
    if find_physical_detail(value):
        value = _safe_fallback(kind)
    if not has_self_feeling(value):
        prefixes = _FEELING_PREFIXES
        value = prefixes[len(value) % len(prefixes)] + value
    if opener_problem(value, opener_mode, recent_texts):
        value = vary_opener(value, opener_mode, recent_texts)
    if _shape_problem(value, recent_texts):
        value = _vary_structure(value)
    if find_physical_detail(value):  # 兜底句里的 topic 插值也可能带物理词
        value = _SAFE_FALLBACK.get(str(kind or ""), _SAFE_FALLBACK["care"])
    return value


# --------------------------------------------------------------------------- #
# P0-17：相邻主动消息的逻辑/情绪连续性
# --------------------------------------------------------------------------- #
#: new_topic 只允许这些自然口语转折（去掉「回到刚才/接着聊」这类元话语）
CONTINUITY_TRANSITIONS = ("对了", "说起来", "换个话题", "顺带说个事", "另外")
#: 指代承接（不许出现“刚才/前面/接着”这类元话语）
CONTINUITY_REFS = ("那个", "那件", "这事", "这件", "那事", "那条", "那批",
                   "那次", "这个", "这些", "那些")
#: P0-20：全量禁用的“元话语”（客服/播报腔），发送前必须剥离
METADISCOURSE_PHRASES = (
    "接着刚才说的", "回到刚才", "刚才提到", "前面提到的", "前面提到",
    "前面说的", "我刚说", "刚说到", "接着聊", "刚才说的", "刚才说",
)
#: 功能字：算“实词重叠”时排除含这些字的 2-gram
_CONTINUITY_STOP_CHARS = set(
    "的了着过是我你他她它们在就都很太又这那一不没也还和与及把被让给对从到为以于而之其或但然后呢吗吧啊嘛哦")
_NEGATIVE_EMOTION = (
    "累", "沉", "烦", "闷", "难受", "低落", "焦虑", "疲惫", "困", "没劲",
    "丧", "难过", "压力", "撑不住", "不想动",
)
_STRONG_POSITIVE_EMOTION = (
    "巨开心", "特别开心", "超级开心", "太开心", "好开心", "开心极了",
    "巨好", "特别爽", "太爽", "兴奋", "爽翻",
)


def _content_bigrams(text: Any) -> set[str]:
    value = re.sub(r"[^\u4e00-\u9fff]+", "", str(text or ""))
    grams = {value[index:index + 2] for index in range(len(value) - 1)}
    return {gram for gram in grams
            if not any(char in _CONTINUITY_STOP_CHARS for char in gram)}


def _shares_content(first: Any, second: Any) -> bool:
    """两段文本是否有 ≥2 字实词重叠（排除功能字 2-gram）。"""
    return bool(_content_bigrams(first) & _content_bigrams(second))


def has_transition(text: Any) -> bool:
    value = str(text or "")
    return any(marker in value for marker in CONTINUITY_TRANSITIONS)


def has_continuation_reference(text: Any) -> bool:
    value = str(text or "")
    return any(ref in value for ref in CONTINUITY_REFS)


def contains_metadiscourse(text: Any) -> bool:
    """P0-20：文本是否含「接着刚才说的/刚才说的/前面提到」这类元话语。"""
    value = str(text or "")
    return any(phrase in value for phrase in METADISCOURSE_PHRASES)


def _collapse_duplicate_prefix(value: str) -> str:
    """剥离元话语后可能留下「这会儿，这会儿…」→ 合并成一个开头前缀。"""
    for prefix in _all_opener_prefixes():
        key = _strip_prefix_edges(prefix)
        if not key:
            continue
        value = re.sub(r"(" + re.escape(key) + r")[，,]?\s*\1", r"\1", value)
    return value


def strip_metadiscourse(text: Any) -> str:
    """删除元话语短语（连同多余逗号），不做同义替换；纯函数、幂等。"""
    value = str(text or "")
    if not value:
        return value
    for phrase in sorted(METADISCOURSE_PHRASES, key=len, reverse=True):
        value = value.replace(phrase, "")
    value = re.sub(r"[，,、；;]{2,}", "，", value)
    value = re.sub(r"[，,、；;]\s*([。！？!?])", r"\1", value)
    value = re.sub(r"([。！？!?])\s*[，,、；;]+", r"\1", value)
    value = re.sub(r"^[，,、；;。\s]+", "", value).strip()
    return _collapse_duplicate_prefix(value)


def parse_voice_choice(text: Any) -> tuple[str, str]:
    """解析 LLM 的 choice: share_with_anchor|reaction（没有就返回空）。"""
    value = str(text or "").strip()
    match = re.search(r"(?:^|[\s，。；;])(?:choice|表达方式)\s*[:：]\s*"
                      r"(share_with_anchor|reaction)\s*", value, re.I)
    if not match:
        return "", value
    choice = match.group(1).lower()
    body = (value[:match.start()] + value[match.end():]).strip()
    return choice, body.lstrip("，。；; \n").strip()


def consecutive_bare_emotion(recent_texts: list[str] | None) -> int:
    count = 0
    for item in list(recent_texts or []):
        text = str(item or "").strip()
        if not text:
            continue
        if has_concrete_subject(text):
            break
        count += 1
    return count


def needs_concrete_subject(text: Any, recent_texts: list[str] | None,
                           user_recent: list[str] | None = None) -> bool:
    """P0-23 软约束：只兜底连续≥3裸情绪/无上下文裸情绪；单条裸情绪放行。"""
    if has_concrete_subject(text):
        return False
    recent = [str(item or "") for item in list(recent_texts or []) if str(item or "")]
    if consecutive_bare_emotion(recent) >= 2:
        return True
    has_context = (any(has_concrete_subject(item) for item in recent[:3])
                   or bool([item for item in list(user_recent or []) if str(item or "")]))
    return not has_context


def parse_action(text: Any) -> tuple[str, str]:
    """解析 LLM 输出的 ``action: continuation|new_topic``，返回 (action, 正文)。"""
    value = str(text or "").strip()
    match = re.search(r"(?:^|[\s，。；;])(?:action|动作)\s*[:：]\s*(continuation|new_topic)\s*",
                      value, re.I)
    if not match:
        return "", value
    action = match.group(1).lower()
    body = (value[:match.start()] + value[match.end():]).strip()
    body = body.lstrip("，。；; \n").strip()
    return action, body


def infer_action(text: Any, previous: Any = "") -> str:
    """LLM 没给结构化 action 时按文本推断。"""
    value = str(text or "")
    if has_transition(value):
        return "new_topic"
    if previous and (has_continuation_reference(value) or _shares_content(value, previous)):
        return "continuation"
    if previous and topic_overlap(value, previous) < 0.2:
        return "new_topic"
    return "continuation"


def continuity_ok(text: Any, action: str, recent_sent: list[str] | None) -> bool:
    """连续性校验：不许元话语；continuation 靠内容/指代；new_topic 有转折且明显不同。"""
    history = [str(item or "").strip() for item in list(recent_sent or [])
               if str(item or "").strip()]
    if not history:
        return True
    previous = history[0]
    value = str(text or "").strip()
    if not value:
        return False
    if contains_metadiscourse(value):
        return False
    if action == "new_topic":
        return has_transition(value) and topic_overlap(value, previous) < 0.2
    # continuation（默认）
    if has_continuation_reference(value) or _shares_content(value, previous):
        return True
    return False


def emotion_polarity(text: Any) -> str:
    value = str(text or "")
    if any(word in value for word in _NEGATIVE_EMOTION):
        return "negative"
    if any(word in value for word in _STRONG_POSITIVE_EMOTION):
        return "positive"
    return "neutral"


def emotion_conflict(text: Any, previous_polarity: str, mood: float) -> bool:
    """情绪大跳检测：上条低落/累，当前 mood 不高时不允许“巨开心”式强正向。"""
    value = str(text or "")
    if previous_polarity != "negative":
        return False
    if float(mood) >= 0.65:
        return False
    return any(word in value for word in _STRONG_POSITIVE_EMOTION)


def _recent_proactive_messages(path: Path | str, limit: int = 3) -> list[dict]:
    """最近 N 条自己发过的主动消息（kind/text/ts，新→旧）。"""
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT kind,text,created_at FROM proactive WHERE status!='rejected' "
            "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        con.close()
    return [{"kind": str(row["kind"] or ""), "text": str(row["text"] or ""),
             "ts": float(row["created_at"] or 0)} for row in rows]


def _continuity_context(recent_own: list[dict], user_recent: list[str],
                        mood: float, energy: float) -> str:
    """给 prompt 的连续上下文（自己最近 3 条 + 用户最近 5 条 + 当前状态/情绪极性）。"""
    lines = ["你最近发过的主动消息（新→旧）："]
    if recent_own:
        for item in recent_own:
            lines.append(f"- [{item.get('kind') or '?'}] {str(item.get('text') or '')[:80]}")
    else:
        lines.append("- （暂无，前面没有你说过的话）")
    lines.append("用户最近说过的：")
    if user_recent:
        for item in user_recent[-5:]:
            lines.append(f"- {str(item or '')[:80]}")
    else:
        lines.append("- （暂无）")
    previous = recent_own[0]["text"] if recent_own else ""
    polarity = emotion_polarity(previous) if previous else "neutral"
    lines.append(f"当前状态：mood={mood:.2f} energy={energy:.2f}；"
                 f"上一条你的情绪倾向={polarity}。")
    return "\n".join(lines)


def _strip_prefix_edges(value: Any) -> str:
    return re.sub(r"^[，,。；;、\s:：]+|[，,。；;、\s:：]+$", "", str(value or ""))


def _all_opener_prefixes() -> tuple[str, ...]:
    prefixes: list[str] = []
    for items in _OPENER_PREFIXES.values():
        prefixes.extend(items)
    return tuple(prefixes)


def _match_existing_prefix(value: str) -> str:
    """正文开头是否已带某个开头模式前缀（忽略前缀末尾标点）。"""
    for prefix in _all_opener_prefixes():
        key = _strip_prefix_edges(prefix)
        if key and value.startswith(key):
            return key
    return ""


def _prepend_continuity(text: str, marker: str, opener_mode: str) -> str:
    """在正文前加承接/转折标记；最多一层开头前缀 + 一层标记，已带前缀/标记不重复。"""
    value = str(text or "").strip()
    if not value:
        return value
    # 已有同一标记 / 同类指代或转折：不再叠加（避免「prefix+标记+prefix」冗余）
    if marker in value:
        return value
    if marker.startswith("接着") and has_continuation_reference(value):
        return value
    if not marker.startswith("接着") and has_transition(value):
        return value
    matched = _match_existing_prefix(value)
    if matched:
        rest = value[len(matched):].lstrip("，,；;、 ")
        return matched + "，" + marker + rest
    prefixes = _OPENER_PREFIXES.get(opener_mode) or _OPENER_PREFIXES["direct"]
    prefix = prefixes[len(value) % len(prefixes)]
    return prefix + marker + value


def _pick_transition(text: Any) -> str:
    options = ("对了，", "说起来，", "换个话题，", "顺带说个事，", "另外，")
    value = str(text or "")
    return options[len(value) % len(options)]


def _continuity_postprocess(text: Any, action: str, recent_sent: list[str] | None,
                            previous_polarity: str, mood: float,
                            kind: str, opener_mode: str) -> tuple[str, str]:
    """确定性收口：剥元话语 / 内容承接不加标记 / 真断裂改「对了，」/ 兜底。"""
    value = strip_metadiscourse(str(text or "").strip())
    if not value:
        value = _safe_fallback(kind)
    history = [str(item or "").strip() for item in list(recent_sent or [])
               if str(item or "").strip()]
    if emotion_conflict(value, previous_polarity, mood):
        for strong in _STRONG_POSITIVE_EMOTION:
            value = value.replace(strong, "心情还行")
    if not history:
        return value, action
    previous = history[0]

    if action == "new_topic":
        # 内容其实相近 → 不需要转折词，直接当承接发（内容自身已承接）
        if topic_overlap(value, previous) >= 0.2:
            return value, "continuation"
        if not has_transition(value):
            value = _pick_transition(value) + value
        if continuity_ok(value, "new_topic", history):
            return value, "new_topic"
        return _safe_fallback(kind), "new_topic"

    # continuation：先验证内容是否真承接
    if continuity_ok(value, "continuation", history):
        return value, "continuation"
    if topic_overlap(value, previous) >= 0.2:
        # 内容相近但不命中实词/指代规则 → 直接发，不加任何标记（加标记反而假）
        return value, "continuation"
    # 真断裂：不加承接头，改 new_topic + 一个自然转折词
    if not has_transition(value):
        value = _pick_transition(value) + value
    if continuity_ok(value, "new_topic", history):
        return value, "new_topic"
    return _safe_fallback(kind), "new_topic"


def _continuity_problem(text: Any, action: str, recent_sent: list[str] | None,
                        previous_polarity: str, mood: float) -> bool:
    if contains_metadiscourse(text):
        return True
    if emotion_conflict(text, previous_polarity, mood):
        return True
    return not continuity_ok(text, action, recent_sent)


def _bubbles_coherent(bubbles: list[str]) -> bool:
    """多气泡：后面的气泡必须与第一个气泡有实词重叠/承接指代/转折词。"""
    if len(bubbles) <= 1:
        return True
    first = bubbles[0]
    for bubble in bubbles[1:]:
        if _shares_content(first, bubble) or has_continuation_reference(bubble) \
                or has_transition(bubble):
            continue
        return False
    return True


# --------------------------------------------------------------------------- #
# P0-21：聊天语气 v2（内心念头 / 口语元素 / 句尾不加句号 / 短句单主题）
# --------------------------------------------------------------------------- #
#: 口语元素池（用户点名）；每条最多 1 个、最近 5 条不重复
#: 口语元素只认「句首/句尾/标点旁」的用法（避免把内容词「离谱」当元素删掉）
_PARTICLE_BOUNDARY = set("，,。！？!?…；;、 ")
VOICE_PARTICLES = (
    "诶", "哎", "唉", "咋说呢", "说真的", "你别说", "可惜", "离谱",
    "无语", "有点上头", "挺逗", "笑死", "笑死我了", "真的服了", "服了",
    "哈哈哈", "嗯…",
)
#: 少量 emoji（真实画像约 5.1%）
VOICE_EMOJIS = ("🙂", "😂", "🥲", "😅", "😭", "🙃")
#: 真人语气 few-shot 兜底（正常从 agi/eval_data/human_tone/fewshot.json 加载 20 条）
_HUMAN_FEWSHOT_FALLBACK = (
    "那还是去体育中心吧", "可以", "我在吃饭", "吃完先", "订了",
    "8点半？", "有星星看吗？", "不是早说了吗？", "在吗？", "看得到吗？",
    "ok", "那明天再去吧", "打球，去不去", "好像是这个", "今晚请你喝水",
    "定了吗", "是不是这个号码", "咋了呀", "不过找到了就好", "播到一半，电影出问题了",
)


def _load_human_fewshot() -> tuple[str, ...]:
    """加载用户真实语气 few-shot（20 条）；文件不可用时退回内置同款。"""
    try:
        path = Path(__file__).resolve().parents[1] / "eval_data" / "human_tone" / "fewshot.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        texts = [str(item.get("text") or "").strip()
                 for item in data.get("examples", [])]
        texts = [text for text in texts if text]
        if texts:
            return tuple(texts[:20])
    except Exception:  # noqa: BLE001 - 语料缺失不阻塞生成
        pass
    return _HUMAN_FEWSHOT_FALLBACK


#: 只学语气/长度/标点习惯，不照抄内容
VOICE_FEWSHOT = _load_human_fewshot()
VOICE_RULES = (
    "聊天语气（最重要，读起来要像人在聊天，别像写作文）：\n"
    "- 只说你此刻的**内心念头/感受/评价**，不要汇报自己在做什么"
    "（禁止「在整理资料」「在听专辑」「刚做完…」这种动作流水账）；\n"
    "- 口语是语气词自然带出来（吧/啊/呢/呀/诶/嗯），不是每条都硬塞"
    "「笑死/真的服了」；口语元素每条最多 1 个、隔几条出现一次；\n"
    "- **短句为主**：多数 2-12 字，中位数像 7 字，允许「可以」「嗯，好像也是」"
    "这种极短句；一条只说一件事，不要「在X的时候…在Y」并置；\n"
    "- **句尾默认不加任何标点**；只有真提问才用？，绝不用。或！；\n"
    "- 偶尔可以带 1 个 emoji，不要多；\n"
)
VOICE_FEWSHOT_SECTION = (
    "- 参考你平时说话的样子（学语气/长度/标点习惯，不要照抄内容）：\n"
    + "\n".join(f"  · {example}" for example in VOICE_FEWSHOT)
)
VOICE_CONSTRAINTS = VOICE_RULES + VOICE_FEWSHOT_SECTION


#: P0-23：泛指/时间/空结构词；判定"具体事由"时先剔掉
CONCRETE_TIME_WORDS = (
    "这会儿", "最近", "今天", "昨天", "现在", "刚才", "上次", "之前", "以前",
    "突然", "说起来", "然后", "其实", "刚刚", "刚", "这会儿",
    "对了", "另外", "换个话题", "顺带说个事", "接着聊", "回到刚才",
)
CONCRETE_VAGUE_WORDS = (
    "事儿", "事", "东西", "那事", "这事", "这玩意", "那个", "这个", "啥",
    "什么", "一点", "一堆", "好多", "几处", "没想明白", "空", "这些", "那些",
    "聊", "聊聊", "聊个事", "说个事", "说话", "随便说说", "跟你", "跟你们",
    "说法", "观点", "道理", "一句话", "意思",
    "越想越", "越想", "越来越", "说不上来", "说不上", "反正", "挺烦的",
)
_CONCRETE_REMOVE_WORDS = tuple(sorted(set(
    VOICE_PARTICLES + _FEELING_MARKERS + _NEGATIVE_EMOTION
    + _STRONG_POSITIVE_EMOTION + CONCRETE_TIME_WORDS + CONCRETE_VAGUE_WORDS
    + ("烦", "乱", "累", "吵", "闷", "笑", "上头")
    + tuple(prefix.rstrip("，,；;、 ") for items in _OPENER_PREFIXES.values()
            for prefix in items)
), key=len, reverse=True))


def _concrete_residue(text: Any) -> str:
    """去掉情绪/口语/时间/功能词后剩下的实词串（用来判断有没有具体事由）。"""
    value = str(text or "")
    for word in _CONCRETE_REMOVE_WORDS:
        if word and word in value:
            value = value.replace(word, "")
    value = re.sub(r"[^一-鿿]+", "", value)
    return "".join(char for char in value if char not in _CONTINUITY_STOP_CHARS)


def has_concrete_subject(text: Any) -> bool:
    """P0-23：是否含具体名词/事件（≥2 字实词）；裸情绪返回 False。"""
    return len(_concrete_residue(text)) >= 2


def _pick_concrete_anchor(sources: list[str]) -> str:
    """从真实可用锚点里挑一个具体片段（state 话题 > 自己最近消息 > 用户最近提过）。"""
    for source in sources:
        residue = _concrete_residue(source)
        if len(residue) < 2:
            continue
        for start in range(0, max(1, len(residue) - 1)):
            fragment = residue[start:start + 6]
            if len(fragment) < 2:
                continue
            if fragment in CONCRETE_VAGUE_WORDS:
                continue
            if find_physical_detail(fragment):
                continue
            return fragment
    return ""


def ensure_concrete_subject(text: Any, kind: str, sources: list[str] | None = None) -> str:
    """裸情绪 → 用真实锚点补具体事由；无锚点则用具体事由兜底。"""
    value = strip_metadiscourse(str(text or "").strip())
    if has_concrete_subject(value):
        return value
    bare = value
    # 去掉空结构的前缀/转折词（“说起来，有点烦” → “有点烦”）
    for marker in CONTINUITY_TRANSITIONS:
        prefix = marker + "，"
        if bare.startswith(prefix):
            bare = bare[len(prefix):].strip()
            break
    matched = _match_existing_prefix(bare)
    if matched:
        bare = bare[len(matched):].lstrip("，,；;、 ").strip()
    bare = _strip_particles(bare).strip("，,；; ") or "有点烦"
    fragment = _pick_concrete_anchor(list(sources or []))
    if fragment:
        candidate = f"想起{fragment}，{bare}"
        if find_physical_detail(candidate) is None and not contains_metadiscourse(candidate):
            return candidate
    return _safe_fallback(kind)


def apply_voice_polish(text: Any, kind: str, anchors: list[str] | None = None,
                       max_chars: int = 40) -> tuple[str, dict]:
    """P0-24：接入 voice_polish 安全层；改后复检，任一失败回退原文。

    返回 (最终文本, {changed, reverted, reasons, failures})，供 tick 日志/测试使用。
    """
    original = str(text or "")
    info = {"changed": False, "reverted": False, "reasons": [], "failures": []}
    if not original.strip():
        return original, info
    try:
        from . import voice_polish  # 延迟导入，避免循环依赖/文件未落地时崩
    except Exception:  # noqa: BLE001
        return original, info
    try:
        result = voice_polish.polish_reply(original, max_chars=max_chars)
    except Exception:  # noqa: BLE001
        return original, info
    info["reasons"] = list(result.get("reasons") or [])
    if not result.get("changed"):
        return original, info
    candidate = str(result.get("text") or "")
    info["changed"] = candidate != original
    failures: list[str] = []
    # 实体/数字零丢失
    protected = voice_polish.protected_tokens(original)
    if any(token not in candidate for token in protected):
        failures.append("protected_missing")
    if find_physical_detail(candidate):
        failures.append("physical")
    if contains_metadiscourse(candidate):
        failures.append("metadiscourse")
    if not grounding_ok(candidate, list(anchors or [])):
        failures.append("grounding")
    if candidate.rstrip().endswith(("。", "！", "!")):
        failures.append("trailing_period")
    if len(candidate) > 25 and not ("「" in candidate or "」" in candidate):
        failures.append("too_long")
    if not candidate.strip():
        failures.append("empty")
    if failures:
        info["reverted"] = True
        info["failures"] = failures
        return original, info
    return candidate, info


_ADVICE_RE = re.compile(r"建议|最好|记得|应该|试试|不妨|可以试|不妨试")


def strip_advice(text: Any, kind: str) -> str:
    """P3-d：behavior.advice=false 时不出现建议类句式（删建议小句）。"""
    value = str(text or "").strip()
    if not value or not _ADVICE_RE.search(value):
        return value
    parts = [part.strip() for part in re.split(r"[，,。！？!?；;]", value) if part.strip()]
    kept = [part for part in parts if not _ADVICE_RE.search(part)]
    result = "，".join(kept).strip("，,；; ")
    return result or _safe_fallback(kind)


def _particle_spans(text: Any) -> list[tuple[int, int, str]]:
    """非重叠命中（「笑死我了」不会被同时算成「笑死」+「笑死我了」）。"""
    value = str(text or "")
    spans: list[tuple[int, int, str]] = []
    for particle in sorted(VOICE_PARTICLES, key=len, reverse=True):
        start = 0
        while True:
            index = value.find(particle, start)
            if index < 0:
                break
            start = index + 1
            if particle == "服了" and index > 0 and value[index - 1] == "舒":
                continue
            end = index + len(particle)
            before = value[index - 1] if index > 0 else ""
            after = value[end] if end < len(value) else ""
            # 只认「句首/句尾/标点旁」的口语用法；「挺离谱的段子」里的离谱是内容词
            at_start = index == 0 or before in _PARTICLE_BOUNDARY or before.isspace()
            at_end = end == len(value) or after in _PARTICLE_BOUNDARY or after.isspace()
            if not (at_start or at_end):
                continue
            if any(left < end and index < right for left, right, _ in spans):
                continue
            spans.append((index, end, particle))
    return sorted(spans)


def _contains_particle(text: Any, particle: str) -> bool:
    return any(span[2] == particle for span in _particle_spans(text))


def _particles_in(text: Any) -> list[str]:
    return [particle for _, _, particle in _particle_spans(text)]


def _strip_particles(text: Any, keep: str = "") -> str:
    value = str(text or "")
    spans = _particle_spans(value)
    keep_span = None
    for span in spans:
        if keep and span[2] == keep and keep_span is None:
            keep_span = span
            break
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        if keep_span is not None and span == keep_span:
            continue
        pieces.append(value[cursor:span[0]])
        cursor = span[1]
    pieces.append(value[cursor:])
    value = "".join(pieces)
    value = re.sub(r"[，,、]{2,}", "，", value)
    value = re.sub(r"^[，,、；;\s]+", "", value)
    value = re.sub(r"[，,、；;]+$", "", value).strip()
    return value


def _choose_particle(used: list[str], seed: int) -> str:
    available = [particle for particle in VOICE_PARTICLES if particle not in used]
    if not available:
        available = list(VOICE_PARTICLES)
    return available[seed % len(available)]


def apply_oral_particle(text: Any, recent_texts: list[str] | None = None) -> str:
    """口语元素收口：最多 1 个、最近 3 条不重复、覆盖率约 30–50%（真实画像）。"""
    value = str(text or "").strip()
    if not value:
        return value
    history = [str(item or "") for item in list(recent_texts or [])[:3]
               if str(item or "").strip()]
    used = [particle for item in history for particle in _particles_in(item)]
    hits = sum(1 for item in history if _particles_in(item))
    present = _particles_in(value)
    if present:
        keep = present[0]
        if keep in used or hits >= 2:
            return _strip_particles(value)
        return _strip_particles(value, keep=keep)
    # 问句/省略号结尾不加口语尾巴，保住原句尾
    if value.rstrip().endswith(("？", "?", "…", "~")):
        return value
    # 已经有情绪尾巴/堆叠尾巴时不再加口语元素
    if any(token in value for token in ("反正", "越想越", "说不上来", "挺烦的")):
        return value
    inject = hits == 0 or (hits == 1 and len(history) % 2 == 0)
    if not inject:
        return value
    particle = _choose_particle(used, len(value) + len(history))
    if len(value) + len(particle) + 1 > 25:
        # 先按短句收口（保留具体事由），再试更短的口语元素
        value = limit_voice_length(value, 20) or value
    if len(value) + len(particle) + 1 > 25:
        for shorter in ("诶", "哎", "唉", "挺逗", "可惜", "离谱", "无语"):
            if shorter not in used and len(value) + len(shorter) + 1 <= 25:
                particle = shorter
                break
        else:
            return value
    return value.rstrip("。！？!?…~ ") + "，" + particle


def apply_emoji(text: Any, recent_texts: list[str] | None = None,
                now: float = 0.0, enabled: bool = True) -> str:
    """真实画像约 5% 消息带 1 个 emoji；已有则不再加。"""
    value = str(text or "").strip()
    if not value or not enabled:
        return value
    if any(emoji in value for emoji in VOICE_EMOJIS):
        return value
    if int(float(now or 0) // 300) % 20 != 0:   # 约 5%（真实画像 5.1%）
        return value
    return value + VOICE_EMOJIS[int(float(now or 0) // 300) % len(VOICE_EMOJIS)]


def normalize_particles(value: Any) -> str:
    """最终兜底：一条消息最多保留 1 个口语元素。"""
    text = str(value or "")
    present = _particles_in(text)
    if len(present) > 1:
        return _strip_particles(text, keep=present[0])
    return text


def _collapse_repeated_phrase(value: str, phrase: str) -> str:
    value = str(value or "")
    first = value.find(phrase)
    if first < 0:
        return value
    second = value.find(phrase, first + len(phrase))
    if second < 0:
        return value
    start = second
    if start > 0 and value[start - 1] in "，,；; ":
        start -= 1
    return value[:start] + value[second + len(phrase):]


def clean_chat_punctuation(value: Any) -> str:
    """去掉「。，」「，。」这类拼接垃圾、重复前缀和多余逗号。"""
    text = str(value or "")
    for prefixes in _OPENER_PREFIXES.values():
        for prefix in prefixes:
            key = _strip_prefix_edges(prefix)
            if key:
                text = _collapse_repeated_phrase(text, key)
    text = re.sub(r"。\s*[，,、]+", "。", text)
    text = re.sub(r"[，,、]+\s*。", "。", text)
    text = re.sub(r"。{2,}", "。", text)
    text = re.sub(r"[，,、]{2,}", "，", text)
    text = re.sub(r"^[，,、；;]+", "", text)
    text = re.sub(r"[，,、；;]+$", "", text)
    return text.strip()


def clean_reaction_text(text: Any, keep_transition: bool = False) -> str:
    """纯情绪条保持短促：去掉 Shape 尾巴/多余前缀，只留一句情绪。

    keep_transition=True（new_topic）时保留转折词，避免丢掉“对了/说起来”。
    """
    value = str(text or "").strip()
    if has_concrete_subject(value):
        return value
    for tail in _SHAPE_TAILS:
        value = value.replace(tail, "")
    for _ in range(4):
        changed = False
        if not keep_transition:
            for marker in CONTINUITY_TRANSITIONS:
                prefix = marker + "，"
                if value.startswith(prefix):
                    value = value[len(prefix):].strip()
                    changed = True
        matched = _match_existing_prefix(value)
        if matched:
            rest = value[len(matched):].lstrip("，,；;、 ").strip()
            if rest:
                value = rest
                changed = True
        if not changed:
            break
    # 去掉结尾悬空的转折/时间尾巴（「想起一件事，对了」）
    for _ in range(4):
        match = re.search(r"[，,]([^，,]{1,6})$", value)
        if not match:
            break
        tail = match.group(1).strip()
        if (tail in CONCRETE_TIME_WORDS or tail in CONTINUITY_TRANSITIONS
                or tail in VOICE_PARTICLES or tail in ("对了", "另外", "这会儿")):
            value = value[:match.start()].strip()
        else:
            break
    value = clean_chat_punctuation(value)
    return value or "有点烦"


def strip_trailing_period(text: Any) -> str:
    """P0-21：结尾默认无标点（去掉。和！），只保留真问号/省略号/波浪；幂等。"""
    value = str(text or "")
    if not value:
        return value
    value = value.rstrip()
    while value.endswith(("。", "！", "!")):
        value = value[:-1].rstrip()
    return value


_ACTION_CLAUSE_RE = re.compile(r"(?:^|[，,。；;！？!?])(在[^，,。；;！？!?]{1,12})")


def fix_single_topic(text: Any, kind: str = "") -> str:
    """单主题：出现 ≥2 个「在…」动作结构 → 截断到第一个念头 + 情绪尾巴。"""
    value = str(text or "")
    starts = [match.start(1) for match in _ACTION_CLAUSE_RE.finditer(value)]
    if len(starts) < 2:
        return value
    head = value[:starts[1]].rstrip("，,；;、。！？!?... ")
    if not head:
        return value
    if not has_self_feeling(head):
        head += "，有点烦"
    return head


def limit_voice_length(text: Any, limit: int = 18) -> str:
    """P0-23：中位数 10-18、上限 25；截断时先删口语/情绪尾巴，保留具体事由。"""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    if "「" in value or "」" in value:   # followup 真实锚点不截断，避免 grounding 误杀
        return value
    # 先去掉口语元素（不要为了变短先删事由）
    stripped = _strip_particles(value).strip("，,；; ")
    if len(stripped) <= limit and has_concrete_subject(stripped):
        return stripped
    parts = [part.strip() for part in re.split(r"[，,。！？!?；;]", stripped) if part.strip()]
    subject_parts = [part for part in parts if has_concrete_subject(part)]
    other_parts = [part for part in parts if not has_concrete_subject(part)]
    ordered = subject_parts[:1] + other_parts + subject_parts[1:]
    head = ""
    for part in ordered:
        candidate = part if not head else head + "，" + part
        if len(candidate) > 18 and head:
            break
        if len(candidate) > limit:
            break
        head = candidate
    if not head:
        head = stripped[:limit]
    if len(head) > limit:
        head = head[:limit]
    if has_concrete_subject(head) and not has_self_feeling(head):
        tail = "，有点烦"
        if len(head) + len(tail) <= limit:
            head += tail
    return head


_ACTION_VERB_RE = re.compile(r"在(?:整理|听|看|读|学|写|翻|查|做)[^，,。；;！？!?]{0,12}[，,]?")


def strip_embedded_action_prefixes(value: Any) -> str:
    """P0-21：去掉「顺带说个事，在听…」里转折词后紧跟的动作分句。"""
    text = str(value or "").strip()
    markers = tuple(CONTINUITY_TRANSITIONS) + ("说个事", "跟你说", "说实话", "对了")
    for marker in markers:
        prefix = marker + "，"
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix):]
        match = _ACTION_VERB_RE.match(rest)
        if not match:
            continue
        rest = rest[match.end():].lstrip("，, ")
        text = prefix + (rest if rest else "有点烦")
        if not has_self_feeling(text):
            text += "，有点烦"
        break
    return text


def strip_action_opener(value: Any) -> str:
    """P0-21：去掉「在听歌的时候/在整理东西的时候」这类动作汇报开头。"""
    text = str(value or "").strip()
    for prefix in _OPENER_PREFIXES.get("digital", ()):
        key = _strip_prefix_edges(prefix)
        if not key or not text.startswith(key):
            continue
        rest = text[len(key):].lstrip("，,；;、 ")
        if rest and has_self_feeling(rest):
            return rest
    return text


def split_short_bubbles(text: Any, max_bubbles: int, now: float = 0.0) -> list[str]:
    """真实画像约 13.6% 轮次连发 2 条短气泡；两条都必须短且承接同一句。"""
    value = str(text or "").strip()
    if max_bubbles < 2 or len(value) < 8 or "「" in value or "」" in value:
        return [value] if value else []
    if int(float(now or 0)) % 100 >= 14:
        return [value]
    matches = list(re.finditer(r"[，,。！？!?…]", value))
    for match in reversed(matches):
        head = value[:match.start()].strip("，,。！？!?… ")
        tail = value[match.end():].strip("，,。！？!?… ")
        if 1 <= len(head) <= 18 and 1 <= len(tail) <= 18:
            return [head, tail]
    return [value]


def dedupe_opener_prefixes(value: Any) -> str:
    """同一开头前缀在句中出现多次时只留第一次（防止「突然想到…突然想到…」）。"""
    text = str(value or "")
    for prefixes in _OPENER_PREFIXES.values():
        for prefix in prefixes:
            key = _strip_prefix_edges(prefix)
            if not key:
                continue
            while text.count(key) > 1:
                before = text
                text = _collapse_repeated_phrase(text, key)
                if text == before:
                    break
    return text


def collapse_leading_prefixes(value: Any) -> str:
    """开头最多保留一层前缀：叠加的「嗯…/说个事/顺带说个事」收成一个。"""
    text = str(value or "").strip()
    if not text:
        return text
    opener_keys = [( _strip_prefix_edges(prefix), "opener")
                   for prefix in _all_opener_prefixes()]
    transition_keys = [(marker, "transition") for marker in CONTINUITY_TRANSITIONS]
    candidates = sorted(opener_keys + transition_keys, key=lambda item: len(item[0]),
                        reverse=True)
    openers: list[str] = []
    transitions: list[str] = []
    while True:
        for key, kind in candidates:
            if not key or not text.startswith(key):
                continue
            # 只有「前缀后面跟标点」才算叠加前缀；「最近在整理…」不是两个前缀
            after = text[len(key):len(key) + 1]
            if after and after not in "，,；;、":
                continue
            text = text[len(key):].lstrip("，,；;、 ")
            (openers if kind == "opener" else transitions).append(key)
            break
        else:
            break
    result = ""
    if openers and transitions:
        result = openers[-1] + "，对了，"
    elif openers:
        result = openers[-1] + "，"
    elif transitions:
        result = transitions[0] + "，"
    return (result + text).strip()


def _mentions_anchor(text: str, anchor: str) -> bool:
    """LLM 文本有没有真的引用到锚点（整句或至少一个 2 字片段）。"""
    value = str(text or "")
    target = str(anchor or "").strip()
    if not target:
        return True
    if target in value:
        return True
    compact = _WS_RE.sub("", target)
    return any(compact[i:i + 2] in value for i in range(len(compact) - 1))


_INNER_FALLBACK_LAST = 0.0


def _resolve_proactive_inner() -> str:
    """P3-d：proactive_inner 开关（off|auto|force，默认 auto）。"""
    raw = os.environ.get("PROACTIVE_INNER")
    if raw is None:
        try:
            from ..wechat_gateway import config as gateway_config
            data = json.loads(Path(gateway_config.CONFIG_PATH).read_text(encoding="utf-8"))
            raw = data.get("proactive_inner") if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            raw = None
    value = str(raw or "auto").strip().lower()
    return value if value in ("off", "auto", "force") else "auto"


def _log_proactive_inner_fallback(reason: str, now: float | None = None) -> bool:
    """proactive_inner_fallback：JSONL 10 分钟节流（不影响主链）。"""
    global _INNER_FALLBACK_LAST
    now = time.time() if now is None else float(now)
    if now - _INNER_FALLBACK_LAST < 600.0:
        return False
    _INNER_FALLBACK_LAST = now
    try:
        from ..inner import inject
        target = Path(inject.INNER_LOG_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(now, 3), "kind": "proactive_inner_fallback",
                                 "reason": str(reason or "unknown")[:200]},
                                ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def _build_proactive_inner(path, client, inner_client=None,
                           recent_messages: list | None = None) -> dict:
    """P3-d：tick 生成前取一次内状态（每 tick 只调一次），返回 ctx/元数据。"""
    info = {"mode": "off", "source": "off", "latency_ms": None,
            "injected": False, "behavior": {}, "fallback": "", "ctx": ""}
    if _resolve_proactive_inner() == "off":
        return info
    try:
        from ..inner import inject
        settings = inject.resolve_settings(production=True)
    except Exception as exc:  # noqa: BLE001
        return info
    mode = str(settings.get("mode") or "off")
    if mode == "off":
        return info
    settings = dict(settings)
    settings["timeout_s"] = min(1.5, float(settings.get("timeout_s") or 1.5))
    soc = inner_client
    if soc is None and getattr(client, "local_soul", False):
        soc = client
    auto = _resolve_proactive_inner() == "auto"
    if auto and settings.get("shadow_only"):
        # 灰度只观测不注入：主动链路保持原样，避免无意义模型延迟
        info["mode"] = "rule"
        info["source"] = "rule"
        info["fallback"] = "shadow_only"
        return info
    call_mode = mode
    if soc is None:
        try:
            soc = inject.make_local_client(settings)
        except Exception:  # noqa: BLE001
            soc = None
    if auto and mode == "model":
        try:
            available = bool(soc is not None and inject.probe_local(soc))
        except Exception:  # noqa: BLE001
            available = False
        if not available:
            # auto=server 可用才用；不可用时保持被动链路原样（不注入 rule 块）
            info["mode"] = "rule"
            info["source"] = "rule"
            info["fallback"] = "server_unavailable"
            _log_proactive_inner_fallback("server_unavailable")
            return info
    if soc is None and not auto:
        _log_proactive_inner_fallback("client_missing")
    try:
        outcome = inject.build_inner_ctx_ex(
            path, call_mode, soc, "", list(recent_messages or []), None,
            proactive=True, scope="all",
            shadow_only=bool(settings.get("shadow_only")))
        info["mode"] = str(outcome.get("mode") or call_mode)
        info["source"] = str(outcome.get("source") or "off")
        info["latency_ms"] = outcome.get("latency_ms")
        info["injected"] = bool(outcome.get("injected"))
        state = outcome.get("state") or {}
        info["behavior"] = dict(state.get("behavior") or {})
        info["ctx"] = str(outcome.get("ctx") or "") if info["injected"] else ""
        info["fallback"] = str(outcome.get("fallback") or "")
        if info["fallback"]:
            _log_proactive_inner_fallback(info["fallback"])
    except Exception as exc:  # noqa: BLE001 - 内状态失败绝不阻塞主动消息
        info["mode"] = "rule"
        info["source"] = "rule"
        _log_proactive_inner_fallback(f"internal:{type(exc).__name__}")
    if not info["ctx"]:
        info["injected"] = False
    return info


#: P0-17 连续性静态约束（prompt_store 默认值；动态上下文另拼）
CONTINUITY_CONSTRAINTS = (
    "\n叙事连续性（强制二选一，正文前单独一行输出 action）：\n"
    "- action: continuation → 承接上一条说的事：复用上一条里出现过的具体词"
    "（名词/事件词，如「那批笔记」「旧结论」）或「那个/这事」指代；"
    "**绝对不要出现「刚才/前面/接着」这类元话语**；\n"
    "- action: new_topic → 换话题，正文必须以「对了/说起来/换个话题/顺带说个事/另外」"
    "开头；\n"
    "- 情绪要连贯：不要从上一句的低落/疲累突然变成特别开心。\n"
)


def _kind_prompt(kind: str, candidate: dict, mood: float, share_drive: float,
                 dialog: str, *, force_statement: bool = False,
                 regenerate: bool = False, own_only: bool = False,
                 fix_runon: bool = False, opener_mode: str = "",
                 style_fix: bool = False, continuity_ctx: str = "",
                 continuity_fix: bool = False, continuity_hint: str = "",
                 concrete_fix: bool = False, inner_ctx: str = "",
                 media_ctx: str = "", text_fix: bool = False) -> str:
    """按 kind 选生成指令 + 全局约束（禁元话题/反编造/标点/人格边界/开头/问句/连续性）。"""
    instruction = prompt_store.get(
        f"proactive.kind.{kind}",
        KIND_INSTRUCTION.get(kind) or KIND_INSTRUCTION["care"])
    voice_section = prompt_store.get("proactive.voice_fewshot", VOICE_FEWSHOT_SECTION)
    persona = (f"你现在心情 {mood:.2f}，分享欲 {share_drive:.2f}。你想主动给对方发条微信。")
    if inner_ctx:
        persona += ("\n你的内在状态（只影响语气与选择，不是事实，不要说出口）：\n"
                    + str(inner_ctx))
    prompt = (
        persona
        + instruction
        + TOPIC_CONSTRAINTS
        + prompt_store.get("proactive.anti_fabrication", GROUNDING_CONSTRAINTS)
        + prompt_store.get("proactive.punctuation", RUNON_CONSTRAINTS)
        + prompt_store.get("proactive.persona_constraints", PERSONA_CONSTRAINTS)
        + VOICE_RULES
        + voice_section
        + ("表达方式（正文前单独一行输出 choice: share_with_anchor 或 reaction）：\n"
           "- share_with_anchor：你想分享一件事/一个观察，必须有具体事由+情绪；\n"
           "- reaction：纯情绪反应/承接上一条/回应对方，可以没有具体事由"
           "（哈哈/服了/有点烦/笑死）；\n"
           "分享用 share_with_anchor，短促回应用 reaction，由你判断。\n")
        + "只输出你要说的话，1-2 句，口语；不要客服腔、不报能力、不列清单。"
    )
    if opener_mode in _OPENER_INSTRUCTIONS:
        prompt += "\n开头模式：" + _OPENER_INSTRUCTIONS[opener_mode]
    if continuity_ctx:
        prompt += (prompt_store.get("proactive.continuity", CONTINUITY_CONSTRAINTS)
                   + continuity_ctx)
    if continuity_fix:
        prompt += ("\n【重生成】上一条和你之前说的话断开了：这次要么用上一条里的具体词"
                   "或「那个/这事」承接（不要写「刚才/前面/接着」），"
                   "要么改用 new_topic 并以「对了」等自然转折词开头。")
    if continuity_hint:
        prompt += "\n" + str(continuity_hint)
    anchor = str(candidate.get("anchor") or "").strip()
    topic = str(candidate.get("topic") or "").strip()
    if kind == "followup" and anchor:
        prompt += f"必须提到这个具体内容：「{anchor}」，不要换成泛泛的近况。"
        prompt += f"（例外：可以引用这条真实锚点「{anchor}」，其他共同经历一律不许编造。）"
    elif kind == "progress_check" and anchor:
        prompt += (f"用户自己说过要做「{anchor}」。必须引用这个真实锚点，"
                   f"这次必须是一句直接问进度的问句，结尾用问号"
                   f"（例如「{anchor}准备得怎么样了？」「{anchor}定下来了吗？」）；"
                   f"不要只表达紧张/感慨，不要写「最近怎么样」，不要编造其他共同经历。")
    elif kind == "share" and topic:
        prompt += f"可以从对方之前说过的「{topic}」自然引到你自己的想法。"
    if force_statement:
        prompt += "\n这次只表达自己，不要问对方任何问题，不要出现问号。"
    if regenerate:
        prompt += ("\n【重生成】上一次生成的内容和最近发过的消息主题太像了，"
                   "换一个完全不同的具体话题重说一句。")
    if own_only:
        prompt += ("\n【重生成】不要提对方做过/说过的事，也不要提你们共同的经历；"
                   "只说你自己的事（你的经历/想法/刚观察到的小事）。")
    if fix_runon:
        prompt += ("\n【重生成】上一句把两层意思粘在一起了（中间没有标点）："
                   "把两层意思用句号分开，重写这一条，不要丢内容。")
    if style_fix:
        prompt += ("\n【重生成】只写数字生活/自己的感受，不要写任何物理起居、身体、"
                   "天气、位置、出行细节；开头按指定模式、不要用「刚」开头；"
                   "必须带你自己的感受或想法；句子可短可长，换一种结构。")
    if text_fix:
        prompt += ("\n【重生成】正文不要出现表情包/素材/清单/表情库/系统/指令等元话题；"
                   "不要用文字描述“我要发表情/那个表情”，要发就单独输出 [[media:...]] 指令行；"
                   "如果本条没有发贴纸，就不要提“那个/表情/贴纸”；"
                   "第一条必须带具体事由，让人单看就懂；后续气泡只能承接第一条或用户上条消息，"
                   "不要用“那个/它/这事”指代前文没出现的东西；"
                   "不要把负面情绪和“哈哈哈/笑死”放同一句；"
                   "不要写“心里/心头/悬着的心/眼眶/百感交集”这种内心独白。"
                   "改写成具体表达：❌心里一下子轻了 → ✅终于弄完了，舒服；"
                   "❌心里暖暖的 → ✅这话听着挺舒服；❌悬着的心放下了 → ✅总算放心了。")
    if media_ctx:
        prompt += "\n" + media_ctx
    prompt += "如果实在没话说，输出 SKIP。最近对话：" + dialog
    return prompt


def _ask_llm(client, prompt: str) -> tuple[str, bool]:
    """调一次主动生成；返回 (text, 是否拿到可用回复)。异常/不可用都安全降级。"""
    if client is None or not getattr(client, "available", lambda: False)():
        return "", False
    try:
        result = client.chat(
            [{"role": "system", "content": "你说话像普通人，短句。"},
             {"role": "user", "content": prompt}],
            purpose="selflearn_proactive", max_tokens=150, temperature=0.8)
    except Exception:  # noqa: BLE001 - 生成失败走模板兜底，不影响主链
        return "", False
    if not isinstance(result, dict) or not result.get("ok"):
        return "", False
    return str(result.get("text") or "").strip(), True


def _normalize_content(text: str, kind: str, candidate: dict, *,
                       force_statement: bool = False) -> str:
    """生成文本的硬约束后处理：空/问句配额/followup 锚点，全部走安全兜底。"""
    anchor = str(candidate.get("anchor") or "").strip()
    topic = str(candidate.get("topic") or "").strip()
    value = str(text or "").strip()
    if not value:
        return _safe_fallback(kind, anchor=anchor, topic=topic)
    if force_statement and _ends_question(value):
        return _safe_fallback(kind, anchor=anchor, topic=topic)
    if kind == "followup" and anchor and not _mentions_anchor(value, anchor):
        return _safe_fallback("followup", anchor=anchor)
    return value



def record_proactive_turn(path: Path | str, assistant_message_id: str, reply: str,
                          now: float | None = None) -> str | None:
    """给主动消息补一条 turn（user_message_id 挂最近一条用户消息）。

    这样"反馈 好/差"才能落到具体轮次上（否则主动消息永远收不到反馈）。
    没有用户消息时返回 None（首次对话前不主动）。
    """
    now = time.time() if now is None else float(now)
    try:
        con = db.connect(path)
    except Exception:  # noqa: BLE001
        return None
    try:
        row = con.execute(
            "SELECT id FROM messages WHERE role='user' ORDER BY ts DESC LIMIT 1").fetchone()
        if not row or not row["id"]:
            return None
        turn_id = _id("T-PRO-", row["id"], now)
        exists = con.execute("SELECT 1 FROM turns WHERE id=?", (turn_id,)).fetchone()
        if exists:
            return turn_id
        con.execute(
            "INSERT INTO turns(id,user_message_id,assistant_message_id,reply,skill_id,"
            "variant_id,feedback,score,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (turn_id, row["id"], str(assistant_message_id or ""), str(reply or ""),
             None, None, None, None, now))
        con.commit()
        return turn_id
    except Exception:  # noqa: BLE001
        return None
    finally:
        con.close()


def _tick_impl(path: Path | str = DB_PATH, client=None, now: float | None = None,
               idle_seconds: float = 30, cooldown: float = 900,
               min_score: float | None = None, respect_state: bool = True,
               inner_client=None, drive: float | None = None) -> dict[str, Any]:
    """网页轮询：由它自己的心情/分享欲决定说不说，可一次说 1-2 条。"""
    now = time.time() if now is None else float(now)
    # P6-e：可回滚分流；默认 legacy，运行期 wechat config 可切 simple
    from .simple_tick import proactive_style, tick_simple
    if proactive_style(path) == "simple":
        return tick_simple(path, client=client, now=now,
                           idle_seconds=idle_seconds, cooldown=cooldown,
                           min_score=min_score, respect_state=respect_state,
                           inner_client=inner_client, drive=drive)
    own = state.load(path)["self"]
    mood = float(own.get("mood", 0.55))
    share_drive = float(own.get("sharing", 0.45))
    energy = float(own.get("energy", 0.6))
    if respect_state and (mood < 0.32 or share_drive < 0.30):
        return {"ok": True, "available": False, "reason": "现在不太想说话"}
    last = _last_interaction(path)
    if not last:
        return {"ok": True, "available": False, "reason": "还没有对话"}
    if now - last < 8:
        return {"ok": True, "available": False, "reason": "刚聊过"}
    if respect_state and idle_seconds > 0 and mood < 0.55 and share_drive < 0.65 and now - last < idle_seconds:
        return {"ok": True, "available": False, "reason": "还在聊"}
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive").fetchone()
        last_p = float(row[0] or 0) if row else 0.0
    finally:
        con.close()
    if cooldown > 0 and last_p and now - last_p < cooldown:
        return {"ok": True, "available": False, "reason": "主动表达冷却中"}
    con = db.connect(path)
    try:
        daily = int(con.execute(
            "SELECT count(*) FROM proactive WHERE created_at>=? AND status IN ('spoken','proposed')",
            (now - 86400,)).fetchone()[0])
    finally:
        con.close()
    budget = _daily_budget(path)
    if budget > 0 and daily >= budget:
        return {"ok": True, "available": False, "reason": "今日主动预算已用完"}
    candidates = build_candidates(path, now)
    init_info = {}
    try:
        init_info = initiative.decide(path, now)
    except Exception:  # noqa: BLE001
        init_info = {}
    share_info = {}
    try:
        share_info = sharing.choose_level(path)
    except Exception:  # noqa: BLE001
        share_info = {}
    if drive is None:
        best = choose_candidate(candidates, path, now)   # 兼容旧 monkeypatch（3 参数）
    else:
        best = choose_candidate(candidates, path, now, drive=drive)
    quadrant = str(init_info.get("quadrant") or "")
    if quadrant == "skip" or (quadrant == "hold" and int(init_info.get("confidence_n") or 0) >= 3):
        return {"ok": True, "available": False,
                "reason": f"决策象限 {quadrant}", "initiative": init_info,
                "sharing_level": share_info}
    threshold = MIN_SCORE if min_score is None else float(min_score)
    if best["score"] < threshold:
        return {"ok": True, "available": False, "reason": "分享欲不足"}
    kind = str(best.get("kind") or "care")
    recent = memory.recent_messages(path, limit=8)
    dialog = " | ".join(f"{m['role']}:{m['text']}" for m in recent[-6:])
    anchors = _grounding_anchors(path, best)
    # P0-8：问句配额（最近 3 条里 ≥2 条问号结尾 → 本次强制陈述句）
    recent_proactive = _recent_proactive_texts(path, TOPIC_WINDOW)
    # P0-17：最近 3 条自己的主动消息 + 用户最近 5 条，用于叙事/情绪连续性
    recent_own = _recent_proactive_messages(path, 3)
    continuity_history = [str(item.get("text") or "") for item in recent_own]
    user_recent = [str(m.get("text") or "") for m in memory.recent_messages(path, 40)
                   if m.get("role") == "user"][-5:]
    previous_text = continuity_history[0] if continuity_history else ""
    previous_polarity = emotion_polarity(previous_text) if previous_text else "neutral"
    continuity_ctx = _continuity_context(recent_own, user_recent, mood, energy)
    # P3-d：主动消息用自己的小模型内状态（每 tick 只调一次；off 时 prompt 不变）
    inner_messages = memory.recent_messages(path, 12)
    inner_info = _build_proactive_inner(path, client, inner_client, inner_messages)
    inner_ctx = str(inner_info.pop("ctx", "") or "")
    inner_behavior = dict(inner_info.get("behavior") or {})
    # P0-14：问句概率化（最近 3 条已有任一问句 → 禁问；否则按 energy/mood 掷骰）
    allow_question = should_ask_question(mood, energy, recent_proactive)
    if inner_behavior.get("ask") is False:
        allow_question = False
    force_statement = not allow_question
    voice_max = 12 if inner_behavior.get("length") == "short" else 25
    advice_allowed = inner_behavior.get("advice") is not False
    emoji_allowed = inner_behavior.get("emoji") != "不用"
    opener_mode = choose_opener_mode(recent_proactive)
    media_ctx = ""
    try:
        from . import media_choice
        media_ctx = media_choice.media_prompt_block(path)
    except Exception:  # noqa: BLE001
        media_ctx = ""
    prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                          force_statement=force_statement,
                          opener_mode=opener_mode,
                          continuity_ctx=continuity_ctx, inner_ctx=inner_ctx,
                          media_ctx=media_ctx)
    text, llm_answered = _ask_llm(client, prompt)
    if llm_answered and (not text or text.upper().startswith("SKIP")):
        # 明确让 LLM 自己决定"这次不说话"：它说 SKIP 就不发，不再用模板兜底
        return {"ok": True, "available": False, "reason": "llm_skip"}
    media_directives = None
    try:
        from . import media_choice
        text, media_directives = media_choice.parse_media_directive(text)
    except Exception:  # noqa: BLE001
        media_directives = None
    voice_choice, choice_body = parse_voice_choice(text)
    if choice_body:
        text = choice_body
    action, parsed_body = parse_action(text)
    if parsed_body:
        text = parsed_body
    action = action or infer_action(text, previous_text)
    text = _normalize_content(text, kind, best, force_statement=force_statement)
    # P0-15：progress_check 必须引用用户真实待办 anchor；无 anchor 重生成一次，仍不行走锚点兜底
    progress_anchor = str(best.get("anchor") or "") if kind == "progress_check" else ""
    if progress_anchor and progress_anchor not in text:
        progress_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                       force_statement=False)
        progress_text, progress_answered = _ask_llm(client, progress_prompt)
        if progress_answered and progress_text and not progress_text.upper().startswith("SKIP"):
            candidate_text = _normalize_content(progress_text, kind, best,
                                                force_statement=False)
            if progress_anchor in candidate_text:
                text = candidate_text
        if progress_anchor not in text:
            text = _safe_fallback("progress_check", anchor=progress_anchor)
    # P0-9 反编造：命中共同经历断言但 anchors 里没有依据 → 重生成 1 次（只讲自己），
    # 仍不过 → 该 kind 的安全兜底句（只讲自己的事）。
    if not grounding_ok(text, anchors):
        own_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                  force_statement=force_statement, own_only=True)
        own_text, own_answered = _ask_llm(client, own_prompt)
        if own_answered and own_text and not own_text.upper().startswith("SKIP"):
            text = _normalize_content(own_text, kind, best,
                                      force_statement=force_statement)
        if not grounding_ok(text, anchors):
            if kind == "progress_check" and progress_anchor:
                text = _safe_fallback("progress_check", anchor=progress_anchor)
            else:
                text = _final_safe(kind, progress_anchor if kind == "progress_check" else "")
    # P0-10 run-on：无标点粘连句（连续 ≥12 汉字后接你/您/我）→ 重生成 1 次
    # （prompt：两层意思用句号分开）→ 仍粘连 → 确定性插入「。」（不丢内容、不换兜底句）。
    if find_runon(text) is not None:
        split_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                    force_statement=force_statement, fix_runon=True)
        split_text, split_answered = _ask_llm(client, split_prompt)
        if split_answered and split_text and not split_text.upper().startswith("SKIP"):
            split_candidate = _normalize_content(split_text, kind, best,
                                                 force_statement=force_statement)
            if grounding_ok(split_candidate, anchors):
                text = split_candidate
        text = _insert_runon_periods(text)
    # P0-8 主题去重：与最近 10 条已发主动消息 2-gram Jaccard ≥0.35 →
    # 同 kind 重生成 1 次（prompt 要求换话题）；仍超标 → 换低重叠兜底句。
    # P0-17 例外：主动承接上一条时，允许与「紧邻上一条」内容重叠（否则会被去重误杀）。
    topic_history = list(recent_proactive)
    if (action == "continuation" and continuity_history
            and continuity_ok(text, "continuation", continuity_history)
            and max_topic_overlap(text, continuity_history[:1]) < 0.6
            and _bigram_containment(continuity_history[0], text) < 0.7):
        # 近重复（复读上一条）仍走去重；真正延续同一话题才豁免
        topic_history = topic_history[1:]
    if topic_history and max_topic_overlap(text, topic_history) >= TOPIC_SIMILARITY_MAX:
        retry_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                    force_statement=force_statement, regenerate=True)
        retry_text, retry_answered = _ask_llm(client, retry_prompt)
        if retry_answered and retry_text and not retry_text.upper().startswith("SKIP"):
            text = _normalize_content(retry_text, kind, best,
                                      force_statement=force_statement)
        if max_topic_overlap(text, topic_history) >= TOPIC_SIMILARITY_MAX:
            text = _low_overlap_fallback(kind, best, topic_history)
    # P0-10 最终兜底：主题去重重生成/兜底后仍可能粘连 → 确定性插句号（幂等）
    text = _insert_runon_periods(text)
    # P0-9 最后一道：主题兜底句也不能编造共同经历
    if not grounding_ok(text, anchors):
        text = _final_safe(kind, progress_anchor if kind == "progress_check" else "")
    # P0-17 叙事/情绪连续性：断裂先重生成 1 次（明确承接上条），再做确定性收口
    if _continuity_problem(text, action, continuity_history, previous_polarity, mood):
        hint = (f"上一条你自己说的是：{previous_text[:80]}" if previous_text else "")
        cont_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                   force_statement=force_statement,
                                   opener_mode=opener_mode,
                                   continuity_ctx=continuity_ctx,
                                   continuity_fix=True, continuity_hint=hint)
        cont_text, cont_answered = _ask_llm(client, cont_prompt)
        if cont_answered and cont_text and not cont_text.upper().startswith("SKIP"):
            cont_action, cont_body = parse_action(cont_text)
            cont_candidate = _normalize_content(cont_body or cont_text, kind, best,
                                                force_statement=force_statement)
            if grounding_ok(cont_candidate, anchors) and (
                    not topic_history
                    or max_topic_overlap(cont_candidate, topic_history)
                    < TOPIC_SIMILARITY_MAX):
                text = cont_candidate
                action = cont_action or infer_action(text, previous_text)
        if _continuity_problem(text, action, continuity_history, previous_polarity, mood):
            text, action = _continuity_postprocess(
                text, action, continuity_history, previous_polarity, mood, kind, opener_mode)
    # P0-14 人格边界/开头多样/自我感受/禁问：违规先重生成 1 次，再做确定性收口
    if _style_problem(text, opener_mode, recent_proactive, allow_question):
        style_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                    force_statement=force_statement,
                                    opener_mode=opener_mode, style_fix=True)
        style_text, style_answered = _ask_llm(client, style_prompt)
        if style_answered and style_text and not style_text.upper().startswith("SKIP"):
            style_candidate = _normalize_content(style_text, kind, best,
                                                 force_statement=force_statement)
            if grounding_ok(style_candidate, anchors) and (
                    not topic_history
                    or max_topic_overlap(style_candidate, topic_history)
                    < TOPIC_SIMILARITY_MAX):
                text = style_candidate
        if _style_problem(text, opener_mode, recent_proactive, allow_question):
            text = _style_postprocess(text, kind, opener_mode,
                                      recent_proactive, allow_question)
    # 风格层可能改写文本，最后再确定性复检一次连续性（不再花 LLM）
    if _continuity_problem(text, action, continuity_history, previous_polarity, mood):
        text, action = _continuity_postprocess(
            text, action, continuity_history, previous_polarity, mood, kind, opener_mode)
    # P0-20：发送前最后一道元话语兜底（删完不成立 → 改走「对了，」/兜底）
    stripped = strip_metadiscourse(text)
    if stripped != text:
        if len(stripped) < 6 or stripped.startswith(("，", ",")):
            if len(stripped) >= 2:
                stripped = _pick_transition(stripped) + stripped.lstrip("，,")
                action = "new_topic"
            else:
                stripped = _safe_fallback(kind)
                action = "new_topic"
        text = stripped
    # P0-21：单主题 / 短句 / 口语元素（放在连续性收口之前，避免截断后断承）
    text = fix_single_topic(text, kind)
    text = limit_voice_length(text, 200 if kind == "followup" else 18)
    text = apply_oral_particle(text, recent_proactive)
    text = apply_emoji(text, recent_proactive, now, enabled=emoji_allowed)
    # P0-23 软约束：单条纯情绪放行；仅连续≥3 或完全无上下文时兜底补事由
    concrete_sources = [str(best.get("topic") or ""), str(best.get("anchor") or "")]
    concrete_sources.extend(continuity_history)
    concrete_sources.extend(user_recent)
    force_concrete = needs_concrete_subject(text, continuity_history, user_recent)
    if force_concrete:
        concrete_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                       force_statement=force_statement,
                                       opener_mode=opener_mode,
                                       continuity_ctx=continuity_ctx,
                                       concrete_fix=True)
        concrete_text, concrete_answered = _ask_llm(client, concrete_prompt)
        if concrete_answered and concrete_text and not concrete_text.upper().startswith("SKIP"):
            concrete_action, concrete_body = parse_action(concrete_text)
            concrete_candidate = _normalize_content(concrete_body or concrete_text, kind, best,
                                                    force_statement=force_statement)
            if (grounding_ok(concrete_candidate, anchors)
                    and has_concrete_subject(concrete_candidate)
                    and (not recent_proactive
                         or max_topic_overlap(concrete_candidate, recent_proactive)
                         < TOPIC_SIMILARITY_MAX)):
                text = concrete_candidate
                action = concrete_action or action
        if not has_concrete_subject(text):
            text = ensure_concrete_subject(text, kind, concrete_sources)
    if _continuity_problem(text, action, continuity_history, previous_polarity, mood):
        text, action = _continuity_postprocess(
            text, action, continuity_history, previous_polarity, mood, kind, opener_mode)
    # 收口加了「对了，」可能让开头模式与上一条相同 → 只补开头前缀收口（不碰内容）
    if opener_problem(text, opener_mode, recent_proactive):
        text = vary_opener(text, opener_mode, recent_proactive)
    # 最后一次：去动作汇报开头 + 收掉叠加前缀 + 单主题/短句收口
    text = strip_action_opener(text)
    text = collapse_leading_prefixes(text)
    text = strip_embedded_action_prefixes(text)
    text = fix_single_topic(text, kind)
    matched_prefix = _match_existing_prefix(text)
    if matched_prefix and _strip_prefix_edges(text) == matched_prefix:
        text = _safe_fallback(kind)   # 只剩一个动作/开头前缀，没有内容 → 兜底

    if not has_self_feeling(text):
        text += "，有点烦"
    text = clean_chat_punctuation(text)
    text = limit_voice_length(text, 200 if kind == "followup" else 18)
    if force_concrete and not has_concrete_subject(text):
        text = ensure_concrete_subject(text, kind, concrete_sources)
    # 截断可能留下「这会儿/对了」这类悬空尾巴 → 去掉，并补最小情绪
    while True:
        match = re.search(r"[，,]([^，,]{1,6})$", text)
        if not match:
            break
        tail = match.group(1).strip()
        if (tail in CONCRETE_TIME_WORDS or tail in CONTINUITY_TRANSITIONS
                or tail in CONCRETE_VAGUE_WORDS or tail in VOICE_PARTICLES
                or tail in ("这会儿", "对了", "另外", "说起来")
                or tail in {_strip_prefix_edges(prefix)
                            for prefixes in _OPENER_PREFIXES.values()
                            for prefix in prefixes}):
            text = text[:match.start()]
            continue
        break
    if has_concrete_subject(text) and not has_self_feeling(text):
        if len(text) + 5 <= 25:
            text += "，有点烦"
    matched_after_limit = _match_existing_prefix(text)
    if matched_after_limit and _strip_prefix_edges(text) == matched_after_limit:
        text = _safe_fallback(kind)
    # 动作前缀被去掉后开头模式可能撞上一条 → 用非动作开头再收口一次
    text = ensure_opener_distinct(text, recent_proactive, opener_mode)
    text = strip_action_opener(text)
    text = ensure_opener_distinct(text, recent_proactive, opener_mode)
    # 最终口语元素收口（前面长度截断可能把口语尾巴删掉）
    text = apply_oral_particle(text, recent_proactive)
    text = normalize_particles(text)
    text = strip_trailing_period(text)
    text = _insert_runon_periods(text)
    if not grounding_ok(text, anchors):
        text = _final_safe(kind, progress_anchor if kind == "progress_check" else "")
    # P0-23 最终兜底：只有被判定必须补事由时才补；单条纯情绪不拦
    if not has_concrete_subject(text) and (
            force_concrete or consecutive_bare_emotion(continuity_history) >= 2
            or not continuity_history):
        text = ensure_concrete_subject(text, kind, concrete_sources)
    text = strip_action_opener(text)
    if action == "new_topic" and not has_transition(text):
        text = _pick_transition(text) + text
    text = ensure_opener_distinct(text, recent_proactive, opener_mode)
    text = dedupe_opener_prefixes(text)
    text = collapse_leading_prefixes(text)
    text = clean_reaction_text(text, action == "new_topic")
    text = limit_voice_length(text, 200 if kind == "followup" else voice_max)
    if not has_concrete_subject(text):
        text = ensure_concrete_subject(text, kind, concrete_sources) if force_concrete else text
    text = clean_reaction_text(text, action == "new_topic")
    text = ensure_opener_distinct(text, recent_proactive, opener_mode)
    text = clean_reaction_text(text, action == "new_topic")
    if not advice_allowed:
        text = strip_advice(text, kind)
    text = strip_trailing_period(text)
    # P0-24：口语化安全层（在 polish 之后、grounding/物理/元话语复检之前）
    text = polish_outbound(text)
    text, voice_info = apply_voice_polish(text, kind, anchors, max_chars=voice_max)
    if not grounding_ok(text, anchors):
        text = _final_safe(kind, progress_anchor if kind == "progress_check" else "")
    if not advice_allowed:
        text = strip_advice(text, kind)
    text = limit_voice_length(text, 200 if kind == "followup" else voice_max)
    text = strip_trailing_period(text)
    text = style_profile.tune_reply("", text)[:200]
    # P5-b：文本定稿（已过 grounding/物理/元话语/voice 复检）后解析模型 media 指令
    media_plan = None
    try:
        from . import media_choice
        media_plan = media_choice.build_plan(
            path, media_directives, text,
            state={"mood": mood, "crisis": bool(own.get("crisis"))},
            recent_media=media_choice.recent_media(path),
            allow_sticker=True, now=now)
        media_choice.record_media(path, bool(media_plan and media_plan.get("bubbles")))
    except Exception:  # noqa: BLE001 - 媒体解析失败不影响文本
        media_plan = None
    # P6-a：文本—媒体一致性 / 元话题 / 情绪极性；先重生成 1 次，再确定性删句
    coherence_context = dialog + "\n" + "\n".join(
        str(item.get("text") or "") for item in recent_own)
    text_media_check = {"issues": [], "fixed": False, "final_issues": [],
                        "regen_failed": []}
    try:
        from . import media_choice as _media
        issues = _media.text_media_issues(text, media_plan, anchors=coherence_context)
        text_media_check["issues"] = issues
        if issues:
            fix_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                      force_statement=force_statement,
                                      opener_mode=opener_mode,
                                      continuity_ctx=continuity_ctx,
                                      inner_ctx=inner_ctx, media_ctx=media_ctx,
                                      text_fix=True)
            fix_text, fix_answered = _ask_llm(client, fix_prompt)
            if not fix_answered:
                _media.record_regen_failed(["llm_unavailable"], reason="regen")
            if fix_answered and fix_text and not fix_text.upper().startswith("SKIP"):
                fix_clean, fix_directives = _media.parse_media_directive(fix_text)
                _fix_choice, fix_clean = parse_voice_choice(fix_clean)
                fix_candidate = _normalize_content(fix_clean, kind, best,
                                                   force_statement=force_statement)
                if emotion_conflict(fix_candidate, previous_polarity, mood):
                    for strong in _STRONG_POSITIVE_EMOTION:
                        fix_candidate = fix_candidate.replace(strong, "心情还行")
                if (grounding_ok(fix_candidate, anchors)
                        and find_physical_detail(fix_candidate) is None
                        and len(fix_candidate) <= 25
                        and not (_media.has_concrete_anchor(text, coherence_context)
                                 and not _media.has_concrete_anchor(
                                     fix_candidate, coherence_context))):
                    text = fix_candidate
                    media_directives = fix_directives
                    media_plan = _media.build_plan(
                        path, media_directives, text,
                        state={"mood": mood, "crisis": bool(own.get("crisis"))},
                        recent_media=_media.recent_media(path),
                        allow_sticker=True, now=now)
                    text_media_check["fixed"] = True
            remaining = _media.text_media_issues(text, media_plan,
                                                 anchors=coherence_context)
            if remaining:
                _media.record_regen_failed(remaining, reason="regen")
                text_media_check["regen_failed"] = remaining
            if remaining:
                text = _media.sanitize_text_media(text, media_plan,
                                                  anchors=coherence_context)
                if not text:
                    text = _safe_fallback(kind, anchor=progress_anchor
                                          if kind == "progress_check" else "")
                media_plan = _media.sync_plan_text(media_plan, text)
        text_media_check["final_issues"] = _media.text_media_issues(
            text, media_plan, anchors=coherence_context)
    except Exception:  # noqa: BLE001
        pass
    # 清理模型可能残留的 choice 前缀（即使在重生成/拼接后仍出现）
    text = re.sub(r"(?i)\b(?:choice|表达方式)\s*[:：]\s*"
                  r"(?:share_with_anchor|reaction|[a-z_]*)\s*", "", text)
    text = text.strip("，,；; ")
    if (media_plan and media_plan.get("bubbles")
            and media_plan["bubbles"][0].get("type") == "emoji_text"):
        text = str(media_plan["bubbles"][0].get("text") or text)
    max_bubbles = 3 if share_drive >= 0.7 else (2 if share_drive >= 0.55 or mood >= 0.7 else 1)
    if int(init_info.get("max_bubbles") or 0) > 0:
        max_bubbles = min(max_bubbles, int(init_info["max_bubbles"]))
    if quadrant == "explore":
        max_bubbles = min(max_bubbles, 1)
    bubbles = style_profile.split_bubbles(text, max_bubbles)
    if len(bubbles) == 1:
        bubbles = split_short_bubbles(text, max_bubbles, now)
    # P0-17：多气泡第二段必须是第一段的延续（共享实词/承接词），否则合并成一条
    if len(bubbles) > 1 and not _bubbles_coherent(bubbles):
        bubbles = ["".join(bubbles)]
    bubbles = [style_profile.strip_periods(b) for b in bubbles if style_profile.strip_periods(b)]
    if not bubbles:
        return {"ok": True, "available": False, "reason": "没组织好语言"}
    ids = [memory.store_message(path, "assistant", b, now=now, proactive=True) for b in bubbles]
    joined = " ".join(bubbles)
    turn_id = record_proactive_turn(path, ids[0] if ids else "", joined, now)
    try:
        cand = share_store.record_candidate(
            path, level=str(share_info.get("level") or ""),
            kind=best["kind"], text=joined, reason=best["reason"],
            source="proactive", score=best["score"], status="spoken")
        if cand.get("id"):
            share_store.record_event(path, cand["id"], "spoken",
                                     {"quadrant": quadrant})
    except Exception:  # noqa: BLE001
        pass
    con = db.connect(path)
    try:
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (_id("PRO-", best["kind"], joined, now), best["kind"], joined,
                     best["reason"], best["score"], "spoken", now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "available": True, "message": bubbles[0], "messages": bubbles,
            "message_id": ids[0], "turn_id": turn_id,
            "kind": best["kind"], "action": action, "score": best["score"],
            "inner": inner_info, "voice_polished": voice_info,
            "media": media_plan, "text_media_check": text_media_check,
            "progress_check": ({"anchor": best.get("anchor"),
                                "msg_id": str((best.get("todo") or {}).get("msg_id") or "")}
                               if kind == "progress_check" else None),
            "reason": best["reason"], "mood": mood, "sharing": share_drive,
            "initiative": {k: init_info.get(k) for k in
                            ("available", "score", "reason", "max_bubbles", "bubble_count",
                             "confidence_lcb", "quadrant")},
            "sharing_level": share_info}


def propose(path: Path | str = DB_PATH, now: float | None = None,
            *, drive: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    if not _cooldown_ok(path, now):
        return {"ok": True, "available": False, "reason": "主动表达冷却中"}
    candidates = build_candidates(path, now)
    init_info = {}
    try:
        init_info = initiative.decide(path, now)
    except Exception:  # noqa: BLE001
        init_info = {}
    share_info = {}
    try:
        share_info = sharing.choose_level(path)
    except Exception:  # noqa: BLE001
        share_info = {}
    if drive is None:
        best = choose_candidate(candidates, path, now)   # 兼容旧 monkeypatch（3 参数）
    else:
        best = choose_candidate(candidates, path, now, drive=drive)
    quadrant = str(init_info.get("quadrant") or "")
    if quadrant == "skip" or (quadrant == "hold" and int(init_info.get("confidence_n") or 0) >= 3):
        return {"ok": True, "available": False,
                "reason": f"决策象限 {quadrant}", "initiative": init_info,
                "sharing_level": share_info}
    if best["score"] < MIN_SCORE:
        return {"ok": True, "available": False, "reason": "分享欲分数不足",
                "candidates": candidates}
    pid = _id("PRO-", best["kind"], best["text"], now)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (pid, best["kind"], best["text"], best["reason"], best["score"], "proposed", now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "available": True, "id": pid, **best,
            "candidates": candidates,
            "initiative": {k: init_info.get(k) for k in
                           ("available", "score", "confidence_lcb", "quadrant")},
            "sharing_level": share_info,
            "note": "这是候选，不会自动发送"}


def decide(path: Path | str, proposal_id: str, action: str) -> dict[str, Any]:
    action = str(action).lower()
    if action not in ("accepted", "rejected"):
        return {"ok": False, "error": "action 只允许 accepted/rejected"}
    con = db.connect(path)
    try:
        cur = con.execute("UPDATE proactive SET status=? WHERE id=? AND status='proposed'",
                          (action, proposal_id))
        con.commit()
        row = con.execute("SELECT * FROM proactive WHERE id=?", (proposal_id,)).fetchone()
    finally:
        con.close()
    if cur.rowcount == 0:
        return {"ok": False, "error": "候选不存在或已处理"}
    return {"ok": True, "action": action, "proposal": dict(row)}


def list_proactive(path: Path | str = DB_PATH, limit: int = 20) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM proactive ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    finally:
        con.close()


# P6-e：供测试/可回滚观察使用；实际分流在 tick 顶部
from .simple_tick import proactive_style, tick_simple as _tick_simple  # noqa: E402


def tick(path: Path | str = DB_PATH, client=None, now: float | None = None,
         idle_seconds: float = 30, cooldown: float = 900,
         min_score: float | None = None, respect_state: bool = True,
         inner_client=None, drive: float | None = None) -> dict[str, Any]:
    """P15-A：主动消息对外入口（原逻辑在 ``_tick_impl``）。

    与原来行为逐字节一致；额外**只读**记一条 ``turn_traces(mode=proactive)``
    （候选/kind/概率/丢弃/regen/video_share 等，静默轮询不记）。trace 失败静默。
    """
    result = _tick_impl(path, client=client, now=now, idle_seconds=idle_seconds,
                        cooldown=cooldown, min_score=min_score,
                        respect_state=respect_state, inner_client=inner_client,
                        drive=drive)
    try:
        from . import trace
        trace.record_proactive(path, result, now=now)
    except Exception:  # noqa: BLE001 - trace 失败绝不影响主动消息
        pass
    return result
