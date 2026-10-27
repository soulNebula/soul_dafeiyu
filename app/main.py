# -*- coding: utf-8 -*-
"""IDEA 工作台后端（M0）：服务管理 + 配置中心 + Git Studio + 审计。"""
from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit, config_store, files_api, gitstudio, paths, services

TOKEN = os.environ.get("IDEA_TOKEN", "8888")

app = FastAPI(title="IDEA · SoulAgent Workbench", version="0.1.0")


# ------------------------------------------------------------------ 鉴权
def require_token(x_admin_token: str | None = Header(default=None),
                  token: str | None = Query(default=None)):
    provided = x_admin_token or token
    if not provided or provided != TOKEN:
        raise HTTPException(status_code=401, detail="admin token required")
    return True


# ------------------------------------------------------------------ 模型
class ConfigSaveBody(BaseModel):
    patch: dict[str, Any]
    auto_commit: bool = True
    auto_push: bool = False


class GitCommitBody(BaseModel):
    message: str
    push: bool = False


class FileWriteBody(BaseModel):
    root: str = "soulagent"
    rel: str
    text: str
    encoding: str = "utf-8"


class FileRunBody(BaseModel):
    root: str = "soulagent"
    rel: str
    args: list[str] = []
    timeout: int = 600
    python: str | None = None


class BranchBody(BaseModel):
    name: str
    create: bool = False


class StageBody(BaseModel):
    paths: list[str] = []


# ------------------------------------------------------------------ 基础
@app.get("/health")
def health():
    return {"ok": True, "service": "idea-workbench", "version": "0.1.0",
            "soulagent": str(paths.soulagent_home()),
            "agent_home": str(paths.agent_home())}


@app.get("/api/wb/env", dependencies=[Depends(require_token)])
def env_info():
    import platform
    import sys
    info = {"python": sys.version.split()[0], "platform": platform.platform(),
            "executable": sys.executable}
    try:
        import torch
        info["torch"] = torch.__version__
        info["cuda"] = bool(torch.cuda.is_available())
        if info["cuda"]:
            info["gpu"] = torch.cuda.get_device_name(0)
    except Exception:  # noqa: BLE001
        info["torch"] = None
    return {"ok": True, **info}


@app.get("/api/wb/overview", dependencies=[Depends(require_token)])
def overview():
    svc = services.list_services()
    git = gitstudio.status()
    cfg = config_store.load_view()
    return {
        "ok": True,
        "soulagent": str(paths.soulagent_home()),
        "config_path": cfg["path"], "config_mtime": cfg["mtime"],
        "services": svc["items"],
        "git": {"ok": git.get("ok"), "branch": git.get("branch"),
                "dirty": git.get("dirty"), "changes": len(git.get("changes") or []),
                "error": git.get("error")},
    }


# ------------------------------------------------------------------ 服务
@app.get("/api/wb/services", dependencies=[Depends(require_token)])
def services_list():
    return services.list_services()


@app.post("/api/wb/services/{name}/{action}", dependencies=[Depends(require_token)])
def services_action(name: str, action: str):
    if action == "start":
        result = services.start(name)
    elif action == "stop":
        result = services.stop(name)
    elif action == "restart":
        result = services.restart(name)
    else:
        raise HTTPException(status_code=400, detail="action 只能是 start/stop/restart")
    if result.get("ok"):
        audit.record("service." + action, name, actor="workbench",
                     meta={"detail": {k: v for k, v in result.items() if k != "text"}})
    return result


@app.get("/api/wb/services/{name}/logs", dependencies=[Depends(require_token)])
def services_logs(name: str, lines: int = Query(default=200, ge=1, le=2000)):
    return services.tail_log(name, lines)


# ------------------------------------------------------------------ 配置
@app.get("/api/wb/config", dependencies=[Depends(require_token)])
def config_get():
    return config_store.load_view()


@app.post("/api/wb/config/validate", dependencies=[Depends(require_token)])
def config_validate(body: ConfigSaveBody):
    return config_store.validate(body.patch)


@app.post("/api/wb/config/save", dependencies=[Depends(require_token)])
def config_save(body: ConfigSaveBody):
    return config_store.save(body.patch, auto_commit=body.auto_commit,
                             auto_push=body.auto_push)


@app.get("/api/wb/config/backups", dependencies=[Depends(require_token)])
def config_backups():
    return {"ok": True, "items": config_store.backups()}


@app.post("/api/wb/config/restore", dependencies=[Depends(require_token)])
def config_restore(body: dict):
    name = str((body or {}).get("name") or "")
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name")
    return config_store.restore_backup(name)


# ------------------------------------------------------------------ Git
@app.get("/api/wb/git/status", dependencies=[Depends(require_token)])
def git_status():
    return gitstudio.status()


@app.get("/api/wb/git/diff", dependencies=[Depends(require_token)])
def git_diff(path: str | None = None, staged: bool = False):
    return gitstudio.diff(path, staged=staged)


@app.get("/api/wb/git/log", dependencies=[Depends(require_token)])
def git_log(limit: int = Query(default=20, ge=1, le=200)):
    return gitstudio.log(limit)


@app.post("/api/wb/git/commit", dependencies=[Depends(require_token)])
def git_commit(body: GitCommitBody):
    result = gitstudio.commit(body.message, push=body.push)
    if result.get("ok"):
        audit.record("git.commit", "repository", actor="workbench",
                     meta={"sha": result.get("sha"), "message": body.message})
    return result


@app.post("/api/wb/git/push", dependencies=[Depends(require_token)])
def git_push():
    result = gitstudio.push_default()
    audit.record("git.push", "repository", actor="workbench", meta=result)
    return result


# ------------------------------------------------------------------ 代码工作区
@app.get("/api/wb/files/roots", dependencies=[Depends(require_token)])
def files_roots():
    return {"ok": True, "items": [{"name": k, "path": v} for k, v in files_api.roots().items()]}


@app.get("/api/wb/files/tree", dependencies=[Depends(require_token)])
def files_tree(root: str = "soulagent", rel: str = "", depth: int = Query(default=3, ge=1, le=6)):
    return files_api.tree(root, rel, depth)


@app.get("/api/wb/files/read", dependencies=[Depends(require_token)])
def files_read(root: str = "soulagent", rel: str = ""):
    return files_api.read_file(root, rel)


@app.post("/api/wb/files/write", dependencies=[Depends(require_token)])
def files_write(body: FileWriteBody):
    result = files_api.write_file(body.root, body.rel, body.text, encoding=body.encoding)
    if result.get("ok"):
        audit.record("file.write", result["path"], actor="workbench", meta={"rel": body.rel})
    return result


@app.post("/api/wb/files/run", dependencies=[Depends(require_token)])
def files_run(body: FileRunBody):
    return files_api.run_python(body.root, body.rel, body.args, timeout=body.timeout,
                                python=body.python)


@app.get("/api/wb/files/job", dependencies=[Depends(require_token)])
def files_job(id: str, offset: int = 0):
    return files_api.job_status(id, offset=offset)


@app.get("/api/wb/files/jobs", dependencies=[Depends(require_token)])
def files_jobs():
    return {"ok": True, "items": files_api.jobs()}


@app.get("/api/wb/files/browse", dependencies=[Depends(require_token)])
def files_browse(path: str = ""):
    return files_api.browse(path)


@app.post("/api/wb/files/roots/add", dependencies=[Depends(require_token)])
def files_add_root(body: dict):
    return files_api.add_root(str(body.get("name") or ""), str(body.get("path") or ""))


@app.post("/api/wb/files/roots/remove", dependencies=[Depends(require_token)])
def files_remove_root(body: dict):
    return files_api.remove_root(str(body.get("name") or ""))


@app.get("/api/wb/files/interpreters", dependencies=[Depends(require_token)])
def files_interpreters():
    return {"ok": True, "items": files_api.interpreters()}


@app.get("/api/wb/files/templates", dependencies=[Depends(require_token)])
def files_templates():
    return {"ok": True, "items": files_api.templates()}


# ------------------------------------------------------------------ Git 分支/暂存
@app.get("/api/wb/git/branches", dependencies=[Depends(require_token)])
def git_branches():
    return gitstudio.branches()


@app.post("/api/wb/git/checkout", dependencies=[Depends(require_token)])
def git_checkout(body: BranchBody):
    result = gitstudio.checkout(body.name, create=body.create)
    if result.get("ok"):
        audit.record("git.checkout", body.name, actor="workbench", meta=result)
    return result


@app.post("/api/wb/git/stage", dependencies=[Depends(require_token)])
def git_stage(body: StageBody):
    return gitstudio.stage(body.paths)


@app.post("/api/wb/git/unstage", dependencies=[Depends(require_token)])
def git_unstage(body: StageBody):
    return gitstudio.unstage(body.paths)


# ------------------------------------------------------------------ 审计
@app.get("/api/wb/audit", dependencies=[Depends(require_token)])
def audit_list(limit: int = Query(default=100, ge=1, le=500)):
    return {"ok": True, "items": audit.list_recent(limit)}


@app.exception_handler(Exception)
def _unhandled(_request, exc):  # pragma: no cover - 兜底
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


# ------------------------------------------------------------------ 静态前端
@app.get("/")
def index():
    return FileResponse(str(paths.WEB_DIR / "index.html"))


app.mount("/web", StaticFiles(directory=str(paths.WEB_DIR)), name="web")
