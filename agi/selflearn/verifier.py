# -*- coding: utf-8 -*-
"""Learn Loop 验证器：Python 子进程强验证 + 答案关键词弱验证。

强验证（:func:`verify_python`）
--------------------------------
- 代码写入临时目录的 ``solution.py``；测试写入 ``test_solution.py``，
  由一个临时 ``runner.py`` 在子进程中先导入 solution，再执行测试；
- 子进程使用 ``-I`` 隔离、临时目录作为 cwd、UTF-8 模式，stdin 为空；
- 超时直接 kill 子进程，返回已捕获的输出；
- 任何失败都落进返回值，不向上抛异常；
- 子进程不是安全沙箱，只用于学习练习中的小代码，不要执行不可信的生产代码。

测试代码先按**脚本**执行（顶层 ``assert`` / 直接调用 / ``unittest.main()``）；
执行完后还会自动运行测试文件里定义的零参数 ``test_*`` 函数和
``unittest.TestCase`` 子类，避免「只定义不调用」被误判为通过。
需要参数/夹具的 pytest 风格函数无法运行，会 fail-closed（本模块不依赖 pytest）。

弱验证（:func:`verify_answer`）
--------------------------------
- 只检查 ``answer`` 是否覆盖 ``expected_terms`` 中的关键词；
- 不理解同义词、否定、因果或数值等价；``passed=True`` 也不代表答案正确；
- 它明确是弱验证，用于「至少提到这些要点」的初筛，不能替代人工/强 verifier。

返回约定
--------
``ok``：验证器是否得到了可判断的结果。测试断言失败 / 运行时异常仍然是 ``True``
（确实执行了，结论是未通过）；空输入、语法错误、超时、无法启动子进程等
「没能完成验证」的情况为 ``False``。

``passed``：是否真正通过；``ok=False`` 时一定为 ``False``。

本模块只做本地子进程执行，不访问网络。
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

_DEFAULT_TIMEOUT = 5.0
_MAX_TIMEOUT = 120.0
_MAX_STREAM = 20000
_MAX_ERROR = 2000
_SOLUTION_NAME = "solution.py"
_TEST_NAME = "test_solution.py"
_RUNNER_NAME = "runner.py"
_KILL_GRACE = 1.0
_WHITESPACE = re.compile(r"\s+")

# 子进程中的测试启动器：把 solution 的顶层名字放进测试命名空间，
# 这样裸 assert 也能直接使用 add(...)；同时保持测试文件里的行号不变。
_RUNNER_SOURCE = '''# -*- coding: utf-8 -*-
"""由 agi.selflearn.verifier 生成的临时测试启动器。"""
import asyncio as _runner_asyncio
import inspect as _runner_inspect
import os as _runner_os
import sys as _runner_sys
import unittest as _runner_unittest

_HERE = _runner_os.path.dirname(_runner_os.path.abspath(__file__))
_runner_sys.path.insert(0, _HERE)

import solution as _runner_solution

_TEST_FILE = _runner_os.path.join(_HERE, "test_solution.py")
with open(_TEST_FILE, "r", encoding="utf-8") as _runner_fh:
    _TEST_SOURCE = _runner_fh.read()

_runner_namespace = {
    _name: _value
    for _name, _value in vars(_runner_solution).items()
    if not _name.startswith("__")
}
_runner_namespace["__name__"] = "__main__"
_runner_namespace["__file__"] = _TEST_FILE
exec(compile(_TEST_SOURCE, _TEST_FILE, "exec", dont_inherit=True),
     _runner_namespace)


def _runner_is_test_function(value):
    if not _runner_inspect.isfunction(value):
        return False
    code = getattr(value, "__code__", None)
    return code is not None and code.co_filename == _TEST_FILE


def _runner_needs_arguments(value):
    try:
        signature = _runner_inspect.signature(value)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.default is _runner_inspect.Parameter.empty and (
                parameter.kind not in (
                    _runner_inspect.Parameter.VAR_POSITIONAL,
                    _runner_inspect.Parameter.VAR_KEYWORD)):
            return True
    return False


_runner_functions = [
    (_name, _value)
    for _name, _value in _runner_namespace.items()
    if _name.startswith("test") and _runner_is_test_function(_value)
]
_runner_functions.sort(key=lambda item: item[1].__code__.co_firstlineno)

for _runner_name, _runner_test in _runner_functions:
    if _runner_needs_arguments(_runner_test):
        raise RuntimeError(
            "不支持的测试函数（需要参数/夹具）: " + _runner_name)
    _runner_returned = _runner_test()
    if _runner_inspect.iscoroutine(_runner_returned):
        _runner_asyncio.run(_runner_returned)

_runner_cases = [
    _value for _value in _runner_namespace.values()
    if _runner_inspect.isclass(_value)
    and issubclass(_value, _runner_unittest.TestCase)
    and getattr(_value, "__module__", "") == "__main__"
]
if _runner_cases:
    _runner_suite = _runner_unittest.TestSuite()
    for _runner_case in _runner_cases:
        _runner_suite.addTests(
            _runner_unittest.defaultTestLoader.loadTestsFromTestCase(
                _runner_case))
    if _runner_suite.countTestCases():
        _runner_result = _runner_unittest.TextTestRunner(
            stream=_runner_sys.stderr, verbosity=2).run(_runner_suite)
        if not _runner_result.wasSuccessful():
            raise SystemExit(1)
'''


def _as_text(value: Any) -> str:
    """None / bytes / 任意对象转成文本，绝不抛异常。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    try:
        return str(value)
    except Exception:  # pragma: no cover - 极端对象
        return repr(value)


def _clip(text: Any, limit: int) -> str:
    value = _as_text(text)
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[已截断，共 {len(value)} 字符]"


def _timeout_seconds(timeout: Any) -> float:
    """把外部传入的 timeout 收敛到安全范围。"""
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return 0.001
    return min(value, _MAX_TIMEOUT)


def _python_command(script: Path) -> list[str]:
    executable = sys.executable or "python"
    # -I: 隔离模式（忽略 PYTHON* 环境变量和用户 site-packages）
    # -B: 不写 .pyc；-X utf8: 子进程 stdio 强制 UTF-8；-u: 超时前也尽量拿到输出
    return [executable, "-I", "-B", "-X", "utf8", "-u", str(script)]


def _compile_error(source: str, filename: str) -> tuple[str, str]:
    """父进程先编译一次，把语法错误变成干净结果，避免无谓启动子进程。

    返回 ``(error, stderr)``；没有错误时二者都为空。
    """
    try:
        compile(source, filename, "exec", dont_inherit=True)
    except Exception as exc:  # SyntaxError / ValueError / ...
        detail = "".join(
            traceback.format_exception_only(type(exc), exc)).strip()
        return f"{type(exc).__name__}: {exc}", detail
    return "", ""


def _kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _collect_after_kill(proc: subprocess.Popen) -> tuple[str, str]:
    """kill 后尽量收完管道里的残留输出。"""
    try:
        out, err = proc.communicate(timeout=_KILL_GRACE)
        return _as_text(out), _as_text(err)
    except subprocess.TimeoutExpired:
        # 子进程可能又派生了进程占着管道；到这里只做兜底清理。
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=_KILL_GRACE)
        except Exception:
            pass
        return "", ""
    except Exception:
        return "", ""


def _exit_error(stderr: str, returncode: int | None) -> str:
    """从 stderr 取一行失败摘要；没有 stderr 就用退出码。"""
    for line in reversed(stderr.splitlines()):
        if line.strip():
            return _clip(line.strip(), 500)
    return f"子进程退出码 {returncode}"


def _py_result(ok: bool, passed: bool, stdout: Any, stderr: Any,
               error: Any) -> dict:
    return {
        "ok": bool(ok),
        "passed": bool(passed),
        "stdout": _clip(stdout, _MAX_STREAM),
        "stderr": _clip(stderr, _MAX_STREAM),
        "error": _clip(error, _MAX_ERROR),
    }


def _run_subprocess(command: list[str], cwd: Path, timeout: float) -> dict:
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return _py_result(
            False, False, "", "",
            f"无法启动 Python 子进程: {type(exc).__name__}: {exc}")

    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            stdout, stderr = _collect_after_kill(proc)
            return _py_result(
                False, False, stdout, stderr,
                f"超时（timeout）：超过 {timeout:g}s 未结束，已 kill 子进程")

        out = _as_text(stdout)
        err = _as_text(stderr)
        if proc.returncode == 0:
            return _py_result(True, True, out, err, "")
        return _py_result(
            True, False, out, err, _exit_error(err, proc.returncode))
    finally:
        _kill(proc)


def verify_python(code: str, test_code: str = "", timeout=5) -> dict:
    """在临时目录里用子进程执行 Python 代码/测试。

    参数：
        code：待验证代码，写成 ``solution.py``。
        test_code：测试脚本。为空时只把 ``code`` 当脚本跑一遍（冒烟验证）。
            非空时由 ``runner.py`` 先导入 solution，再执行 ``test_code``。
        timeout：子进程超时秒数，默认 5；超时后 kill。

    返回：
        ``{"ok": bool, "passed": bool, "stdout": str, "stderr": str, "error": str}``

    ``ok`` 表示是否得到了可判断结果；断言失败/运行异常仍为 True，
    空输入/语法错误/超时等无法完成验证的情况为 False。本函数不抛异常。
    """
    try:
        code_text = _as_text(code)
        test_text = _as_text(test_code)
        if not code_text.strip():
            return _py_result(
                False, False, "", "",
                "code 为空：没有可执行的代码")

        limit = _timeout_seconds(timeout)
        compile_err, compile_detail = _compile_error(code_text, _SOLUTION_NAME)
        if compile_err:
            return _py_result(False, False, "", compile_detail, compile_err)
        if test_text.strip():
            compile_err, compile_detail = _compile_error(
                test_text, _TEST_NAME)
            if compile_err:
                return _py_result(
                    False, False, "", compile_detail, compile_err)

        with tempfile.TemporaryDirectory(
                prefix="agi-verifier-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / _SOLUTION_NAME).write_text(code_text, encoding="utf-8")
            if test_text.strip():
                (root / _TEST_NAME).write_text(test_text, encoding="utf-8")
                (root / _RUNNER_NAME).write_text(
                    _RUNNER_SOURCE, encoding="utf-8")
                script = root / _RUNNER_NAME
            else:
                script = root / _SOLUTION_NAME
            return _run_subprocess(
                _python_command(script), root, limit)
    except Exception as exc:  # 失败不抛异常
        return _py_result(
            False, False, "", "",
            f"验证器内部错误: {type(exc).__name__}: {exc}")


def _normalise(text: Any) -> str:
    """弱匹配归一化：去空白 + casefold；不做同义词/词形还原。"""
    return _WHITESPACE.sub("", _as_text(text)).casefold()


def _term_list(expected_terms: Any) -> tuple[list[str], str]:
    """校验并去重 expected_terms，返回 ``(terms, error)``。"""
    if isinstance(expected_terms, str):
        raw = [expected_terms]
    elif isinstance(expected_terms, (list, tuple, set, frozenset)):
        raw = list(expected_terms)
    else:
        return [], "expected_terms 必须是字符串列表"

    terms: list[str] = []
    seen: set[str] = set()
    for item in raw:
        term = _as_text(item).strip()
        key = _normalise(term)
        if not term or not key or key in seen:
            continue
        seen.add(key)
        terms.append(term)
    if not terms:
        return [], "expected_terms 为空，无法做关键词验证"
    return terms, ""


def _answer_result(ok: bool, passed: bool, matched: list[str],
                   missing: list[str], error: str) -> dict:
    return {
        "ok": bool(ok),
        "passed": bool(passed),
        "matched": list(matched),
        "missing": list(missing),
        "error": _clip(error, _MAX_ERROR),
    }


def verify_answer(answer: str, expected_terms: list[str]) -> dict:
    """弱验证：检查 answer 是否覆盖 expected_terms 的关键词。

    只做「关键词是否出现」的覆盖检查（去空白、忽略大小写），
    不理解同义词、否定、数字大小或语义等价。因此：

    - ``passed=True`` 只表示要点都提到了，**不代表答案正确**；
    - ``passed=False`` 表示至少缺一个关键词，可把 ``missing`` 回灌给学习循环。

    返回：
        ``{"ok": bool, "passed": bool, "matched": list[str],
        "missing": list[str], "error": str}``

    空 answer / 空 expected_terms 视为无法验证：``ok=False, passed=False``。
    本函数不抛异常。
    """
    try:
        answer_text = _as_text(answer)
        terms, term_error = _term_list(expected_terms)
        if term_error:
            return _answer_result(False, False, [], terms, term_error)
        if not answer_text.strip():
            return _answer_result(
                False, False, [], terms, "answer 为空：没有可验证的内容")

        haystack = _normalise(answer_text)
        matched: list[str] = []
        missing: list[str] = []
        for term in terms:
            if _normalise(term) in haystack:
                matched.append(term)
            else:
                missing.append(term)

        if missing:
            return _answer_result(
                True, False, matched, missing,
                "缺少关键词：" + "、".join(missing))
        return _answer_result(True, True, matched, [], "")
    except Exception as exc:  # 失败不抛异常
        return _answer_result(
            False, False, [], [],
            f"弱验证内部错误: {type(exc).__name__}: {exc}")
