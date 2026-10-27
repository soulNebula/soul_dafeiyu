# -*- coding: utf-8 -*-
"""verifier.py：Python 子进程强验证 + 答案关键词弱验证。

只跑简单数学/字符串，不联网；重点覆盖通过、断言失败、超时、语法错误、空输入。
"""
from __future__ import annotations

import time

from agi.selflearn import verifier

PY_KEYS = {"ok", "passed", "stdout", "stderr", "error"}
ANSWER_KEYS = {"ok", "passed", "matched", "missing", "error"}


def _code() -> str:
    return (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def reverse(text):\n"
        "    return text[::-1]\n"
    )


def test_verify_python_pass_and_stdout():
    result = verifier.verify_python(
        _code(),
        "assert add(2, 3) == 5\n"
        "assert add(-1, 1) == 0\n"
        "assert reverse('abc') == 'cba'\n"
        "print('all-math-ok')\n",
    )
    assert set(result) == PY_KEYS
    assert result["ok"] is True
    assert result["passed"] is True
    assert result["error"] == ""
    assert "all-math-ok" in result["stdout"]
    assert result["stderr"] == ""


def test_verify_python_test_can_import_solution():
    result = verifier.verify_python(
        "def mul(a, b):\n    return a * b\n",
        "import solution\nassert solution.mul(6, 7) == 42\n",
    )
    assert result["ok"] is True and result["passed"] is True, result


def test_verify_python_runs_pytest_style_test_functions():
    passed = verifier.verify_python(
        _code(), "def test_math():\n    assert add(1, 2) == 3\n")
    assert passed["ok"] is True and passed["passed"] is True, passed

    failed = verifier.verify_python(
        _code(), "def test_math():\n    assert add(1, 2) == 4\n")
    assert failed["ok"] is True and failed["passed"] is False
    assert "AssertionError" in failed["stderr"]


def test_verify_python_runs_unittest_cases():
    test_code = (
        "import unittest\n"
        "\n"
        "class TestMath(unittest.TestCase):\n"
        "    def test_bad(self):\n"
        "        self.assertEqual(add(1, 2), 4)\n"
    )
    result = verifier.verify_python(_code(), test_code)
    assert result["ok"] is True
    assert result["passed"] is False
    assert "FAILED" in result["stderr"]


def test_verify_python_rejects_fixture_style_test_without_running_it():
    # 需要参数的 pytest 函数本模块跑不了，必须 fail-closed，不能静默通过。
    result = verifier.verify_python(
        _code(),
        "def test_needs_tmp(tmp_path):\n    assert add(1, 2) == 3\n",
    )
    assert result["ok"] is True
    assert result["passed"] is False
    assert "RuntimeError" in result["error"]


def test_verify_python_assertion_failure_is_ok_but_not_passed():
    result = verifier.verify_python(
        _code(), "assert add(2, 2) == 5\n")
    assert set(result) == PY_KEYS
    assert result["ok"] is True, result
    assert result["passed"] is False
    assert "AssertionError" in result["stderr"]
    assert "AssertionError" in result["error"]


def test_verify_python_runtime_error_fails_closed_without_raising():
    result = verifier.verify_python(
        "def boom():\n    raise ValueError('boom')\n", "boom()\n")
    assert result["ok"] is True
    assert result["passed"] is False
    assert "ValueError" in result["stderr"]
    assert "ValueError" in result["error"]


def test_verify_python_timeout_kills_child():
    started = time.monotonic()
    result = verifier.verify_python(
        "import time\ntime.sleep(30)\n", "", timeout=0.4)
    elapsed = time.monotonic() - started
    assert result["ok"] is False, result
    assert result["passed"] is False
    assert "超时" in result["error"]
    # 必须真的 kill，而不是等 30 秒
    assert elapsed < 10, elapsed


def test_verify_python_syntax_error_is_reported():
    result = verifier.verify_python("def broken(:\n    pass\n")
    assert set(result) == PY_KEYS
    assert result["ok"] is False
    assert result["passed"] is False
    assert "SyntaxError" in result["error"]
    assert result["stderr"]
    assert result["stdout"] == ""


def test_verify_python_syntax_error_in_test_code():
    result = verifier.verify_python(_code(), "assert add(1, 2) ==\n")
    assert result["ok"] is False and result["passed"] is False
    assert "SyntaxError" in result["error"]


def test_verify_python_empty_input_fails_without_raising():
    result = verifier.verify_python("", "")
    assert set(result) == PY_KEYS
    assert result["ok"] is False
    assert result["passed"] is False
    assert result["error"]
    assert result["stdout"] == "" and result["stderr"] == ""

    # None / 纯空白同样不应抛异常
    assert verifier.verify_python(None, None)["ok"] is False
    assert verifier.verify_python("  \n\t", "")["ok"] is False


def test_verify_python_smoke_run_without_test_code():
    result = verifier.verify_python("print('hello 中文')", "")
    assert result["ok"] is True and result["passed"] is True, result
    assert "hello 中文" in result["stdout"]


def test_verify_answer_all_terms_pass():
    result = verifier.verify_answer(
        "列表推导式可以用 for 循环实现",
        ["列表推导式", "for循环"],
    )
    assert set(result) == ANSWER_KEYS
    assert result["ok"] is True
    assert result["passed"] is True
    assert result["matched"] == ["列表推导式", "for循环"]
    assert result["missing"] == []
    assert result["error"] == ""


def test_verify_answer_is_case_and_whitespace_insensitive():
    result = verifier.verify_answer(
        "Use a LIST comprehension here",
        ["list", "Comprehension"],
    )
    assert result["passed"] is True, result
    assert result["matched"] == ["list", "Comprehension"]


def test_verify_answer_partial_reports_missing():
    result = verifier.verify_answer(
        "列表推导式很快", ["列表推导式", "生成器表达式"])
    assert set(result) == ANSWER_KEYS
    assert result["ok"] is True
    assert result["passed"] is False
    assert result["matched"] == ["列表推导式"]
    assert result["missing"] == ["生成器表达式"]
    assert "生成器表达式" in result["error"]


def test_verify_answer_empty_input_fails_closed():
    empty_answer = verifier.verify_answer("", ["列表推导式"])
    assert empty_answer["ok"] is False
    assert empty_answer["passed"] is False
    assert empty_answer["matched"] == []
    assert empty_answer["missing"] == ["列表推导式"]
    assert empty_answer["error"]

    empty_terms = verifier.verify_answer("列表推导式", [])
    assert empty_terms["ok"] is False
    assert empty_terms["passed"] is False
    assert empty_terms["error"]

    bad_terms = verifier.verify_answer("列表推导式", None)
    assert bad_terms["ok"] is False and bad_terms["passed"] is False


def test_verify_answer_is_explicitly_weak():
    # 关键词出现就算覆盖；不解析否定，所以弱验证不能当正确性证明。
    result = verifier.verify_answer(
        "这不是列表推导式，只是普通循环", ["列表推导式"])
    assert result["ok"] is True
    assert result["passed"] is True
