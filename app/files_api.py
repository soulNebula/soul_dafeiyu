# -*- coding: utf-8 -*-
"""代码工作区：文件树 / 读写 / 运行 Python（写 PyTorch 代码用）。

安全边界：只允许在配置的 root（默认 SoulAgent 与 idea 自身）下读写；
运行脚本默认工作目录为仓库根，超时 600s，输出缓冲到内存供前端轮询。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import audit, paths

IDEA_ROOT = paths.IDEA_ROOT
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache",
             "data", "media", "log", ".dsh-ops", "model", "models"}
TEXT_SUFFIX = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg",
               ".ini", ".js", ".css", ".html", ".sh", ".bat", ".ps1", ".sql", ".mmd"}

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


ROOTS_FILE = IDEA_ROOT / ".data" / "roots.json"


def _default_roots() -> dict[str, str]:
    return {"soulagent": str(paths.soulagent_home()), "idea": str(IDEA_ROOT)}


def roots() -> dict[str, str]:
    """工作台可浏览的根目录：默认两个 + 用户添加的（.data/roots.json）。"""
    result = _default_roots()
    try:
        extra = json.loads(ROOTS_FILE.read_text(encoding="utf-8"))
        if isinstance(extra, dict):
            for name, path in extra.items():
                if isinstance(path, str) and Path(path).is_dir():
                    result[str(name)] = path
    except Exception:  # noqa: BLE001
        pass
    return result


def add_root(name: str, path: str) -> dict:
    name = str(name or "").strip()[:40]
    path = str(Path(path).expanduser().resolve())
    if not name or not Path(path).is_dir():
        return {"ok": False, "error": "invalid_name_or_path"}
    extra = {}
    try:
        extra = json.loads(ROOTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(extra, dict):
            extra = {}
    except Exception:  # noqa: BLE001
        extra = {}
    extra[name] = path
    ROOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROOTS_FILE.write_text(json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "name": name, "path": path}


def remove_root(name: str) -> dict:
    if name in _default_roots():
        return {"ok": False, "error": "cannot_remove_default"}
    try:
        extra = json.loads(ROOTS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        extra = {}
    if not isinstance(extra, dict) or name not in extra:
        return {"ok": False, "error": "root_not_found"}
    extra.pop(name)
    ROOTS_FILE.write_text(json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "name": name}


def browse(path: str = "") -> dict:
    """列出某个目录的子文件夹（用于「打开文件夹」选择器）。"""
    if path:
        base = Path(path).expanduser().resolve()
    else:
        base = Path.home() / "Desktop"
        if not base.is_dir():
            base = Path.home()
    if not base.is_dir():
        return {"ok": False, "error": "not_a_dir", "path": str(base)}
    skip = {"$recycle.bin", "system volume information", "node_modules", ".git", ".venv",
            "__pycache__", "windows", "program files", "program files (x86)",
            "programdata", "appdata"}
    dirs = []
    try:
        for e in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if not e.is_dir():
                continue
            if e.name.lower() in skip or e.name.startswith("."):
                continue
            try:
                dirs.append({"name": e.name, "path": str(e)})
            except OSError:
                continue
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(base)}
    parent = str(base.parent) if base.parent != base else ""
    return {"ok": True, "path": str(base), "parent": parent, "dirs": dirs}


def _resolve(root: str, rel: str) -> Path:
    base = roots().get(root)
    if not base:
        raise ValueError("unknown_root")
    basep = Path(base).resolve()
    target = (basep / rel).resolve()
    if basep != target and basep not in target.parents:
        raise ValueError("path_out_of_root")
    return target


def tree(root: str = "soulagent", rel: str = "", depth: int = 3) -> dict:
    try:
        base = _resolve(root, rel)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not base.exists():
        return {"ok": False, "error": "not_found"}

    def walk(p: Path, level: int) -> list[dict]:
        out = []
        if level > depth:
            return out
        try:
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return out
        for e in entries:
            if e.name in SKIP_DIRS or e.name.startswith("."):
                continue
            if e.is_dir():
                out.append({"name": e.name, "type": "dir", "rel": str(e.relative_to(Path(roots()[root]))),
                            "children": walk(e, level + 1)})
            elif e.suffix.lower() in TEXT_SUFFIX:
                try:
                    size = e.stat().st_size
                except OSError:
                    size = 0
                out.append({"name": e.name, "type": "file", "size": size,
                            "rel": str(e.relative_to(Path(roots()[root])))})
        return out

    return {"ok": True, "root": root, "base": str(base), "items": walk(base, 1)}


def read_file(root: str, rel: str) -> dict:
    try:
        p = _resolve(root, rel)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not p.is_file():
        return {"ok": False, "error": "not_found"}
    if p.suffix.lower() not in TEXT_SUFFIX:
        return {"ok": False, "error": "binary_or_unsupported"}
    raw = p.read_bytes()
    if len(raw) > 2_000_000:
        return {"ok": False, "error": "too_large"}
    # 中文项目里 .bat/.ps1 常见 GBK；按顺序尝试，记住编码，保存时按原编码写回
    text, encoding = None, "utf-8"
    for enc in ("utf-8", "utf-8-sig", "gb18030", "cp936"):
        try:
            text = raw.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return {"ok": False, "error": "decode_failed"}
    return {"ok": True, "rel": rel, "path": str(p), "text": text,
            "encoding": encoding, "mtime": p.stat().st_mtime}


def write_file(root: str, rel: str, text: str, *, encoding: str = "utf-8",
               actor: str = "workbench") -> dict:
    try:
        p = _resolve(root, rel)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if p.suffix.lower() not in TEXT_SUFFIX:
        return {"ok": False, "error": "unsupported_suffix"}
    p.parent.mkdir(parents=True, exist_ok=True)
    before = ""
    if p.is_file():
        try:
            before = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                before = p.read_text(encoding="gb18030")
            except UnicodeDecodeError:
                before = ""
    enc = encoding if encoding in ("utf-8", "utf-8-sig", "gb18030", "cp936") else "utf-8"
    p.write_text(text, encoding=enc)
    audit.record("file.write", str(p), actor=actor,
                 meta={"root": root, "rel": rel, "chars": len(text),
                       "changed": before != text})
    return {"ok": True, "rel": rel, "path": str(p), "chars": len(text)}


def run_python(root: str, rel: str, args: list[str] | None = None,
               *, timeout: int = 600, python: str | None = None) -> dict:
    """启动一个 Python 脚本（后台），返回 job_id。"""
    try:
        p = _resolve(root, rel)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not p.is_file() or p.suffix.lower() != ".py":
        return {"ok": False, "error": "not_a_python_file"}
    py = python or os.environ.get("IDEA_PYTHON", r"C:\Python314\python.exe")
    cmd = [py, str(p)] + [str(a) for a in (args or [])]
    job_id = uuid.uuid4().hex[:12]
    cwd = str(Path(roots()[root]))
    entry = {"id": job_id, "status": "running", "cmd": cmd, "cwd": cwd,
             "started_at": time.time(), "output": "", "returncode": None}

    def worker():
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
            deadline = time.time() + timeout
            for line in proc.stdout or []:
                with _LOCK:
                    entry["output"] += line
                    if len(entry["output"]) > 500_000:
                        entry["output"] = entry["output"][-400_000:]
                if time.time() > deadline:
                    proc.kill()
                    entry["output"] += "\n[超时终止]\n"
                    break
            proc.wait(timeout=10)
            entry["returncode"] = proc.returncode
            entry["status"] = "done" if proc.returncode == 0 else "failed"
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "failed"
            entry["output"] += f"\n[异常] {exc}"
        finally:
            entry["ended_at"] = time.time()

    threading.Thread(target=worker, daemon=True).start()
    _JOBS[job_id] = entry
    audit.record("file.run", str(p), actor="workbench",
                 meta={"root": root, "rel": rel, "args": args, "job": job_id})
    return {"ok": True, "job": job_id, "cmd": " ".join(cmd), "cwd": cwd}


def job_status(job_id: str, *, offset: int = 0) -> dict:
    entry = _JOBS.get(job_id)
    if not entry:
        return {"ok": False, "error": "job_not_found"}
    out = entry["output"]
    return {"ok": True, "id": job_id, "status": entry["status"],
            "returncode": entry["returncode"], "cmd": entry["cmd"],
            "started_at": entry["started_at"],
            "ended_at": entry.get("ended_at"),
            "output": out[offset:], "next_offset": len(out)}


def jobs() -> list[dict]:
    items = []
    for jid, e in sorted(_JOBS.items(), key=lambda kv: kv[1]["started_at"], reverse=True)[:20]:
        items.append({"id": jid, "status": e["status"], "cmd": e["cmd"],
                      "started_at": e["started_at"], "returncode": e["returncode"]})
    return items


def interpreters() -> list[dict]:
    """可选的 Python 解释器（本机常见环境）。"""
    candidates = [
        ("项目 python", os.environ.get("IDEA_PYTHON", r"C:\Python314\python.exe")),
        ("训练环境 venv312", "E:/agent/venv312/Scripts/python.exe"),
        ("工作台 venv", str(IDEA_ROOT / ".venv" / "Scripts" / "python.exe")),
        ("系统 python", "python"),
    ]
    out = []
    for label, path in candidates:
        if path == "python":
            out.append({"label": label, "path": path, "exists": True})
            continue
        if Path(path).is_file():
            out.append({"label": label, "path": path, "exists": True})
    return out


def templates() -> list[dict]:
    """内置代码模板（写 PyTorch 用）。"""
    return [
        {
            "name": "pytorch_train_lora.py",
            "title": "PyTorch · LoRA SFT 训练骨架",
            "desc": "transformers+peft 的最小可跑训练脚本（cpu/自动设备，20 步 smoke）",
            "code": TEMPLATE_LORA,
        },
        {
            "name": "pytorch_hello.py",
            "title": "PyTorch · 环境自检",
            "desc": "打印 torch 版本/CUDA 是否可用/显存",
            "code": TEMPLATE_HELLO,
        },
    ]


TEMPLATE_HELLO = '''# -*- coding: utf-8 -*-
"""PyTorch 环境自检：在 IDEA 里点运行即可看结果。"""
import torch

print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device:", torch.cuda.get_device_name(0))
    free, total = torch.cuda.mem_get_info()
    print(f"显存: {free/1024**3:.1f} GiB free / {total/1024**3:.1f} GiB total")
x = torch.randn(2, 3, device="cuda" if torch.cuda.is_available() else "cpu")
print("张量:", x.shape, x.device)
'''

TEMPLATE_LORA = '''# -*- coding: utf-8 -*-
"""PyTorch + PEFT 的 LoRA SFT 最小骨架（可直接在 IDEA 里改/跑）。

用法：改 BASE_MODEL / DATA 后点「运行」；显存不足把 LOAD_4BIT 改 True。
依赖：pip install torch transformers peft accelerate [bitsandbytes]
"""
import json
import torch
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

BASE_MODEL = r"E:\\agent\\models\\Qwen2.5-0.5B-Instruct"
DATA = "data/train/sft_sample.jsonl"      # 每行 {"messages":[...], "response":"..."}
OUT = "runs/idea_lora_smoke"
LOAD_4BIT = False
MAX_LEN = 512
STEPS = 20
LR = 2e-4

device = "cuda" if torch.cuda.is_available() else "cpu"
print("device:", device)

tok = AutoTokenizer.from_pretrained(BASE_MODEL, trust_remote_code=True)
if tok.pad_token is None:
    tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL, torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
    device_map=None, trust_remote_code=True,
)
if LOAD_4BIT:
    from transformers import BitsAndBytesConfig
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, quantization_config=BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16),
        device_map="auto", trust_remote_code=True,
    )
    model = prepare_model_for_kbit_training(model)
else:
    model = model.to(device)

lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none",
                  task_type="CAUSAL_LM",
                  target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                  "gate_proj", "up_proj", "down_proj"])
model = get_peft_model(model, lora)
model.print_trainable_parameters()


class ChatDataset(Dataset):
    def __init__(self, path):
        self.rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        r = self.rows[i]
        msgs = list(r.get("messages") or []) + [{"role": "assistant", "content": r["response"]}]
        text = tok.apply_chat_template(msgs, tokenize=False)
        enc = tok(text, truncation=True, max_length=MAX_LEN, padding=False)
        # 只对 assistant 段计算 loss：这里用最简单做法（真实场景按模板切分 label）
        enc["labels"] = enc["input_ids"][:]
        return {k: torch.tensor(v) for k, v in enc.items()}


loader = DataLoader(ChatDataset(DATA), batch_size=1, shuffle=True)
opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=LR)
model.train()
step = 0
for batch in loader:
    step += 1
    batch = {k: v.to(device) for k, v in batch.items()}
    out = model(**batch)
    loss = out.loss
    loss.backward()
    opt.step()
    opt.zero_grad()
    print(f"step {step} loss {loss.item():.4f}")
    if step >= STEPS:
        break
model.save_pretrained(OUT)
print("adapter saved to", OUT)
'''
