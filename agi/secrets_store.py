# -*- coding: utf-8 -*-
"""本机密钥库（``agi/data/secrets.json``）的**唯一读取入口**。

为什么要有这个文件
------------------
历史上真实密钥散落在两个**明文 JSON** 里：

* ``web_config.json`` 的 ``llm.api_key``（DeepSeek API key）；
* 根 ``config.json`` 的 ``db_keys`` / ``db_keys_by_account`` / ``image_aes_key`` /
  ``image_xor_key`` / ``image_keys_by_account``（微信库与图片解密密钥）。

这两个文件承担的是「业务配置」角色，会被用户在网页端保存、被打包、被截图，
明文密钥跟着一起流动，泄漏面远大于必要。``tools/rotate_secrets.py`` 把密钥
单独搬到一个文件里，本模块负责让**各个读取方**以统一、可测试的方式拿到它。

优先级（fail-closed，取不到就是空）
-----------------------------------
1. 环境变量 ``SOULAGENT_SECRETS``（绝对/相对路径，相对路径按仓库根解析）；
2. ``<repo>/agi/data/secrets.json``。

**为什么不是 env > 文件里的值**：本模块只管「密钥文件在哪」；具体某个密钥
（api_key / db_keys）的取值优先级由调用方决定，因为不同链路的历史行为不同
（例如 ``agi.selflearn.llm`` 是 env 优先，而 ``wx_core.config`` 是文件优先）。
统一约定写在 :func:`load` 的文档里，避免「一处 env 优先、一处文件优先」的隐蔽分歧。

安全约束（写代码时请保持）
--------------------------
* **绝不打印值**：本模块不写日志；异常只报类型，不带上文件内容；
* 文件缺失/损坏/不是 dict → 返回空字典（不抛异常，调用方按「没有密钥」处理）；
* 不做网络、不执行文件内容、不跟随符号链接语义（直接 open 目标路径）；
* 读取结果按 (mtime_ns, size) 缓存，避免在轮询热路径里反复读盘；文件被迁移工具
  改写后缓存会自动失效。

典型用法::

    from agi import secrets_store

    secrets_store.llm_api_key()          # secrets.llm.api_key（没有则 ""）
    secrets_store.config_section()       # config 段（db_keys / image_aes_key ...）
    secrets_store.load()                 # 原始 dict（诊断用，别直接打日志）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from agi.paths import data_dir as _default_data_dir

#: 仓库根（agi/ 的上一级）。与 agi.paths.REPO_ROOT 保持一致，但不互相 import，
#: 避免在这个最底层模块里引入包初始化顺序问题。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 覆盖密钥文件位置的唯一环境变量
SECRETS_ENV = "SOULAGENT_SECRETS"

#: 默认位置：与 config.json/web_config.json 同级目录不同，放在已经 gitignore 的
#: agi/data/ 下，天然不会被提交；harden_acl.py 也会收紧 agi/data 的 ACL。
DEFAULT_SECRETS_PATH = _default_data_dir() / "secrets.json"

#: 缓存：{绝对路径: ((mtime_ns, size), dict)}
_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def secrets_path() -> Path:
    """当前密钥文件路径（``SOULAGENT_SECRETS`` 可覆盖，相对路径按仓库根解析）。"""
    raw = str(os.environ.get(SECRETS_ENV) or "").strip()
    if not raw:
        return DEFAULT_SECRETS_PATH
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def reset_cache() -> None:
    """清空读取缓存（测试用；迁移工具写完文件后也可主动调用）。"""
    _CACHE.clear()


def _under_pytest_without_override() -> bool:
    """测试进程里、且没有显式 ``SOULAGENT_SECRETS`` 时，一律当作「没有密钥库」。

    why：否则开发者本机迁移过之后，跑 ``pytest`` 的用例会读到**真实密钥**
    （web_search / webui / wx_core.config 的既有断言都假设「没有 key」），
    既让测试结果随机器漂移，也把真实密钥带进测试进程。
    显式设置 ``SOULAGENT_SECRETS`` 的测试（包括迁移工具自己的测试）不受影响。
    与 ``agi/selflearn/media_choice.py`` 的测试隔离口径保持一致。
    """
    if str(os.environ.get(SECRETS_ENV) or "").strip():
        return False
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def load(path: Path | str | None = None) -> dict[str, Any]:
    """读取密钥文件，返回 dict；任何异常都退化为空 dict。

    返回结构（由 ``tools/rotate_secrets.py`` 写入，版本 1）::

        {
          "version": 1,
          "llm":    {"api_key": "...", "base_url": "..."},
          "config": {"db_keys": {...}, "db_keys_by_account": {...},
                     "image_aes_key": "...", "image_xor_key": "..."}
        }

    这里**不校验**具体字段：迁移工具可能会逐步补充字段，读取方各自按需取用，
    缺字段 = 没有密钥（fail-closed），而不是报错。
    """
    target = Path(path) if path is not None else secrets_path()
    if path is None and _under_pytest_without_override():
        # 测试隔离：见 _under_pytest_without_override 的说明。
        return {}
    try:
        stat = target.stat()
    except OSError:
        return {}
    key = str(target)
    signature = (int(stat.st_mtime_ns), int(stat.st_size))
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        # 损坏/权限不足/编码错误都按「没有密钥」处理。不回显文件内容：
        # 这个文件里全是密钥，异常信息只需要能定位问题。
        return {}
    data = raw if isinstance(raw, dict) else {}
    _CACHE[key] = (signature, data)
    return data


def section(name: str, path: Path | str | None = None) -> dict[str, Any]:
    """取顶层某一段（如 ``"llm"`` / ``"config"``）；不是 dict 时返回空 dict。"""
    value = load(path).get(name)
    return dict(value) if isinstance(value, dict) else {}


def get(*keys: str, default: Any = None, path: Path | str | None = None) -> Any:
    """嵌套取值：``get("llm", "api_key")``；中途缺失返回 ``default``。"""
    current: Any = load(path)
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, (str, int)) else ""


def llm_api_key(path: Path | str | None = None) -> str:
    """``secrets.llm.api_key``；兼容 Bot 包格式 ``providers.chat.api_key``。"""
    text = _text(section("llm", path).get("api_key"))
    if text:
        return text
    providers = section("providers", path)
    chat = providers.get("chat") if isinstance(providers, dict) else None
    return _text((chat or {}).get("api_key") if isinstance(chat, dict) else "")


def llm_base_url(path: Path | str | None = None) -> str:
    return _text(section("llm", path).get("base_url"))


def config_section(path: Path | str | None = None) -> dict[str, Any]:
    """``secrets.config``：与根 ``config.json`` 同名的敏感字段。"""
    return section("config", path)


def db_keys(path: Path | str | None = None) -> dict[str, Any]:
    """当前账号的 WCDB 密钥表：每个库文件的相对路径 → {key, salt, layout}。"""
    value = config_section(path).get("db_keys")
    return dict(value) if isinstance(value, dict) else {}


def db_keys_by_account(path: Path | str | None = None) -> dict[str, Any]:
    value = config_section(path).get("db_keys_by_account")
    return dict(value) if isinstance(value, dict) else {}


def image_aes_key(path: Path | str | None = None) -> str:
    return _text(config_section(path).get("image_aes_key"))


def image_xor_key(path: Path | str | None = None) -> str:
    return _text(config_section(path).get("image_xor_key"))


def image_keys_by_account(path: Path | str | None = None) -> dict[str, Any]:
    value = config_section(path).get("image_keys_by_account")
    return dict(value) if isinstance(value, dict) else {}


def describe() -> dict[str, Any]:
    """诊断信息：**只返回是否存在/有哪些字段**，绝不返回密钥值。"""
    data = load()
    llm = section("llm")
    cfg = section("config")
    return {
        "path": str(secrets_path()),
        "exists": bool(data),
        "version": data.get("version"),
        "llm_fields": sorted(k for k in llm) if llm else [],
        "config_fields": sorted(k for k in cfg) if cfg else [],
    }


def _atomic_write(path: Path, data: dict[str, Any]) -> bool:
    """原子写 JSON（同目录临时文件 + os.replace），尽力 0600；失败返回 False。"""
    import tempfile

    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    descriptor = -1
    tmp_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1  # fdopen 接管后不能再 close(fd)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_name, 0o600)  # POSIX 生效；Windows 靠 ACL，见 tools/harden_acl.py
        except OSError:
            pass
        os.replace(tmp_name, path)
        return True
    except (OSError, ValueError, TypeError):
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        return False


def store_llm_api_key(value: str) -> bool:
    """把 DeepSeek API key 写入 ``secrets.json`` 的 ``llm.api_key``（空串=清除）。

    这是 802 模型配置页「保存 key」的写入口：key 只落本机密钥库，不进
    ``web_config.json`` 明文、不进 git、不回传前端。
    """
    target = secrets_path()
    if _under_pytest_without_override():
        return False
    data = load(target) if target.is_file() else {}
    if not isinstance(data, dict):
        data = {}
    llm = data.get("llm")
    llm = dict(llm) if isinstance(llm, dict) else {}
    text = _text(value).strip()
    if text:
        llm["api_key"] = text
    else:
        llm.pop("api_key", None)
    data["llm"] = llm
    data.setdefault("version", 1)
    data["updated_at"] = _now()
    if not _atomic_write(target, data):
        return False
    reset_cache()
    return True


def store_config_section(values: dict[str, Any]) -> bool:
    """把敏感字段并入 ``secrets.json`` 的 ``config`` 段（迁移后的写入口）。

    为什么需要这个写入函数：迁移之后 ``config.json`` 不再保存 ``db_keys`` 等字段，
    但运行期（例如首次自动从微信内存提取密钥后）仍会产生**新**的密钥。如果这时
    还走旧的 ``config.save()``，新密钥就会重新以明文写回 ``config.json``，迁移白做。
    所以 ``wx_core/config.py::save()`` 在「密钥库存在」时把敏感字段交给这里。

    返回:
        True  —— 已写入（或无需改动，即内容一致）；
        False —— **不要**路由（没有密钥文件 / 读取失败 / 写入失败）。
                 调用方必须回退到旧行为，绝不能因为写库失败而丢掉密钥。
    """
    target = secrets_path()
    if not target.is_file():
        return False  # 没迁移过的机器：保持旧行为（明文写 config.json）
    if _under_pytest_without_override():
        # 测试隔离：没有显式 SOULAGENT_SECRETS 时，测试里的 save() 绝不能写
        # 开发者本机的真实密钥库（那是生产数据，不是测试夹具）。
        return False
    data = load(target)
    if not isinstance(data, dict) or not data:
        return False
    section_data = data.get("config")
    section_data = dict(section_data) if isinstance(section_data, dict) else {}
    changed = False
    for key, value in (values or {}).items():
        if section_data.get(key) != value:
            section_data[key] = value
            changed = True
    if not changed:
        return True  # 幂等：内容一致就不动盘
    data["config"] = section_data
    data.setdefault("version", 1)
    data["updated_at"] = _now()
    if not _atomic_write(target, data):
        return False
    reset_cache()
    return True


def _now() -> str:
    """本地时间戳（只用于审计，不含任何密钥信息）。"""
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S")