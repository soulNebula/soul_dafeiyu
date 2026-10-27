# -*- coding: utf-8 -*-
"""IDEA 工作台后端（M0）：服务管理 + 配置中心 + Git Studio + 审计。"""
from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit, config_store, datastudio, files_api, gitstudio, memstudio, paths, services, wechat_proxy

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
    block_secrets: bool | None = None


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


class DbSqlBody(BaseModel):
    db: str
    sql: str
    write: bool = False
    confirm: str = ""


class DbUpdateBody(BaseModel):
    db: str
    table: str
    keys: dict[str, Any]
    values: dict[str, Any]
    confirm: str = ""


class DbRefBody(BaseModel):
    db: str


class MemIdBody(BaseModel):
    id: str
    contact: str | None = None
    boost: float | None = None


class MemPurgeBody(BaseModel):
    contact: str | None = None
    days: int | None = None
    confirm: str = ""


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
    result = gitstudio.commit(body.message, push=body.push,
                              block_secrets=body.block_secrets)
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


# ------------------------------------------------------------------ Data Studio（M1-A）
@app.get("/api/wb/db/databases", dependencies=[Depends(require_token)])
def db_databases():
    return datastudio.list_databases()


@app.get("/api/wb/db/schema", dependencies=[Depends(require_token)])
def db_schema(db: str):
    return datastudio.schema(db)


@app.get("/api/wb/db/rows", dependencies=[Depends(require_token)])
def db_rows(db: str, table: str, page: int = Query(default=1, ge=1),
            page_size: int = Query(default=50, ge=1, le=500),
            order_by: str = "", direction: str = "asc", q: str = "",
            filters: str = ""):
    parsed = {}
    if filters:
        try:
            import json as _json
            parsed = _json.loads(filters) or {}
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="filters 必须是 JSON 对象")
    return datastudio.read_rows(db, table, page=page, page_size=page_size,
                                order_by=order_by, direction=direction,
                                filters=parsed, q=q)


@app.post("/api/wb/db/sql", dependencies=[Depends(require_token)])
def db_sql(body: DbSqlBody):
    return datastudio.run_sql(body.db, body.sql, write=body.write,
                              confirm=body.confirm)


@app.post("/api/wb/db/update", dependencies=[Depends(require_token)])
def db_update(body: DbUpdateBody):
    return datastudio.update_row(body.db, body.table, body.keys, body.values,
                                 confirm=body.confirm)


@app.post("/api/wb/db/backup", dependencies=[Depends(require_token)])
def db_backup(body: DbRefBody):
    return datastudio.backup_database(body.db, actor="workbench")


@app.get("/api/wb/db/backups", dependencies=[Depends(require_token)])
def db_backups(limit: int = Query(default=50, ge=1, le=500)):
    return datastudio.list_backups(limit)


# ------------------------------------------------------------------ Memory Studio（M1-B）
@app.get("/api/wb/mem/overview", dependencies=[Depends(require_token)])
def mem_overview(contact: str | None = None):
    return memstudio.overview(contact)


@app.get("/api/wb/mem/list", dependencies=[Depends(require_token)])
def mem_list(contact: str | None = None, kind: str = "", stage: str = "",
             q: str = "", limit: int = Query(default=50, ge=1, le=500),
             offset: int = Query(default=0, ge=0)):
    return memstudio.list_memories(contact, kind=kind, stage=stage, q=q,
                                   limit=limit, offset=offset)


@app.get("/api/wb/mem/get", dependencies=[Depends(require_token)])
def mem_get(id: str, contact: str | None = None):
    return memstudio.get_memory(id, contact)


@app.get("/api/wb/mem/rules", dependencies=[Depends(require_token)])
def mem_rules():
    return memstudio.rules()


@app.post("/api/wb/mem/rules", dependencies=[Depends(require_token)])
def mem_save_rules(body: dict):
    return memstudio.save_rules(body.get("patch") or {}, actor="workbench")


@app.get("/api/wb/mem/decay", dependencies=[Depends(require_token)])
def mem_decay():
    return memstudio.decay_params()


@app.post("/api/wb/mem/decay", dependencies=[Depends(require_token)])
def mem_save_decay(body: dict):
    return memstudio.save_rules(body.get("patch") or {}, actor="workbench")


@app.post("/api/wb/mem/revive", dependencies=[Depends(require_token)])
def mem_revive(body: MemIdBody):
    return memstudio.revive_memory(body.id, body.contact, boost=body.boost)


@app.post("/api/wb/mem/purge-dry-run", dependencies=[Depends(require_token)])
def mem_purge_dry(body: MemPurgeBody):
    return memstudio.purge_dry_run(body.contact, days=body.days)


@app.post("/api/wb/mem/purge", dependencies=[Depends(require_token)])
def mem_purge(body: MemPurgeBody):
    fn = getattr(memstudio, "purge", None)
    if fn is None:
        raise HTTPException(status_code=501, detail="purge 未实现")
    return fn(body.contact, confirm=body.confirm, days=body.days)


@app.get("/api/wb/mem/timeline", dependencies=[Depends(require_token)])
def mem_timeline(contact: str | None = None, days: int = Query(default=30, ge=1, le=365)):
    return memstudio.timeline(contact, days=days)


# ------------------------------------------------------------------ Git 设置/密钥扫描（M1-C）
@app.get("/api/wb/git/settings", dependencies=[Depends(require_token)])
def git_settings():
    return {"ok": True, "settings": gitstudio.settings()}


@app.post("/api/wb/git/settings", dependencies=[Depends(require_token)])
def git_save_settings(body: dict):
    return {"ok": True, "settings": gitstudio.update_settings(body or {})}


@app.get("/api/wb/git/secret-scan", dependencies=[Depends(require_token)])
def git_secret_scan():
    return gitstudio.secret_scan()


@app.post("/api/wb/git/secret-scan", dependencies=[Depends(require_token)])
def git_secret_scan_files(body: dict):
    return gitstudio.secret_scan(body.get("files"))


# ------------------------------------------------------------------ 微信草稿箱（confirm 模式）
@app.get("/api/wb/wechat/drafts", dependencies=[Depends(require_token)])
def wechat_drafts():
    return wechat_proxy.drafts()


@app.post("/api/wb/wechat/drafts/{draft_id}/approve", dependencies=[Depends(require_token)])
def wechat_approve(draft_id: str, force_quiet: bool = True):
    out = wechat_proxy.approve(draft_id, force_quiet=force_quiet)
    audit.record("wechat.draft_approve", draft_id, actor="workbench",
                 meta={"ok": out.get("ok"), "status": out.get("status"),
                       "error": out.get("error")})
    return out


@app.post("/api/wb/wechat/drafts/{draft_id}/reject", dependencies=[Depends(require_token)])
def wechat_reject(draft_id: str):
    out = wechat_proxy.reject(draft_id)
    audit.record("wechat.draft_reject", draft_id, actor="workbench",
                 meta={"ok": out.get("ok")})
    return out


@app.post("/api/wb/wechat/send", dependencies=[Depends(require_token)])
def wechat_send(body: dict):
    contact = str(body.get("contact") or "")
    text = str(body.get("text") or "")
    if not contact or not text:
        raise HTTPException(status_code=400, detail="contact/text 不能为空")
    out = wechat_proxy.send_now(contact, text,
                                force_quiet=bool(body.get("force_quiet", True)))
    audit.record("wechat.send", contact, actor="workbench",
                 meta={"ok": out.get("ok"), "status": out.get("status")})
    return out


# ------------------------------------------------------------------ 发送模式开关
@app.get("/api/wb/wechat/send-mode", dependencies=[Depends(require_token)])
def wechat_get_send_mode():
    return wechat_proxy.get_send_mode()


@app.post("/api/wb/wechat/send-mode", dependencies=[Depends(require_token)])
def wechat_set_send_mode(body: dict):
    mode = str(body.get("mode") or "")
    out = wechat_proxy.set_send_mode(mode, actor="workbench")
    if out.get("ok"):
        audit.record("wechat.send_mode", "config", actor="workbench",
                     meta={"mode": out.get("mode"), "contacts": out.get("contact_modes")})
    return out


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
