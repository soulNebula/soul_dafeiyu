"""中文姓名/昵称 → 拼音首字母（头像用，纯标准库、无第三方依赖）。

规则：
* 英文字母开头 → 返回大写首字母（``Alice`` → ``A``，``lily`` → ``L``）；
* 数字/符号开头 → ``#``；
* 中文 → 按 GB2312 编码区间映射到拼音首字母（``刘`` → ``L``，``何`` → ``H``）；
* GB2312 覆盖不到的字符（生僻字/emoji）→ ``#``。

只用于网页头像展示，多音字按 GB2312 区段取默认首字母，不追求 100% 准确。
"""
from __future__ import annotations

# GB2312 一级汉字按拼音排序，区间边界（含边界编码）→ 首字母
_GBK_BOUNDS: tuple[tuple[int, str], ...] = (
    (0xB0A1, "A"), (0xB0C5, "B"), (0xB2C1, "C"), (0xB4EE, "D"),
    (0xB6EA, "E"), (0xB7A2, "F"), (0xB8C1, "G"), (0xB9FE, "H"),
    (0xBBF7, "J"), (0xBFA6, "K"), (0xC0AC, "L"), (0xC2E8, "M"),
    (0xC4C3, "N"), (0xC5B6, "O"), (0xC5BE, "P"), (0xC6DA, "Q"),
    (0xC8BB, "R"), (0xC8F6, "S"), (0xCBFA, "T"), (0xCDDA, "W"),
    (0xCEF4, "X"), (0xD1B9, "Y"), (0xD4D1, "Z"),
)

_FALLBACK = "#"


def pinyin_initial(text: str) -> str:
    """返回显示名第一个有效字符的拼音/英文首字母（大写），失败返回 ``#``。"""
    for ch in str(text or "").strip():
        if not ch:
            continue
        if ch.isascii():
            if ch.isalpha():
                return ch.upper()
            if ch.isdigit():
                return "#"
            # 跳过前导空格/符号，找后面的有效字符
            continue
        try:
            raw = ch.encode("gb2312")
        except UnicodeEncodeError:
            try:
                raw = ch.encode("gbk")
            except UnicodeEncodeError:
                return _FALLBACK
        if len(raw) < 2:
            return _FALLBACK
        code = raw[0] * 256 + raw[1]
        # 从后往前找第一个 <= code 的边界
        result = _FALLBACK
        for bound, letter in _GBK_BOUNDS:
            if code >= bound:
                result = letter
            else:
                break
        # 超出 Z 区（0xD7F9 之后）仍按 Z 之外处理
        if code > 0xD7F9:
            return _FALLBACK
        return result
    return _FALLBACK


if __name__ == "__main__":  # pragma: no cover - 手工自检
    for name in ("刘工", "何工", "白云一片去悠悠", "Alice", "lily", "123", "🙂x"):
        print(name, "->", pinyin_initial(name))
