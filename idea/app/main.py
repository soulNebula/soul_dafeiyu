# -*- coding: utf-8 -*-
"""IDEA 工作台后端（精简版）：模型 / 训练 / 数据工作室 + 最小 M0。

M0：健康、概览、服务管理、配置中心、审计。
M1：Data Studio（SQLite）。
M3：TrainLab（数据/训练/导出）、Model Studio（模型/适配器/GGUF）。
"""
from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import (Depends, FastAPI, File, Header, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (annotstudio, audit, bot_manager, config_store, corpusstudio,
               datastudio, distillstudio, idea_config, modelstudio, paths,
               services, termlog, trainstudio)

app = FastAPI(title="IDEA · SoulAgent Workbench", version="0.2.0")


# ------------------------------------------------------------------ 鉴权
#: 803 管理口令：``IDEA_TOKEN`` 环境变量 → ``idea/config.json`` 的 ``admin_token``。
#: 弱默认口令（如 8888）不再被自动接受；首次启动生成 32 位随机 token 写回
#: ``idea/config.json``（已 gitignore），启动日志打印一次带 token 的访问 URL。
def require_token(x_admin_token: str | None = Header(default=None),
                  token: str | None = Query(default=None)):
    provided = str(x_admin_token or token or "").strip()
    expected = idea_config.admin_token()
    if not provided or not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="admin token required")
    return True


#: 本机令牌便利入口：与 802 的 /api/token 同安全口径——只允许回环客户端读取。
#: 目的：803 改随机口令后，用户不必去翻 idea/config.json；浏览器地址栏就是本机回环。
#: 不想暴露可设 IDEA_DISABLE_LOCAL_TOKEN=1。
LOCAL_TOKEN_PORTS_OK = ("127.0.0.1", "localhost", "::1")


def _is_loopback_host(host: str | None) -> bool:
    """Host/peer 字符串是否回环（兼容 127.0.0.1:803 / [::1]:803 / localhost）。"""
    value = str(host or "").strip().lower()
    if not value:
        return False
    if value.startswith("["):
        end = value.find("]")
        value = value[1:end] if end > 0 else value.strip("[")
    else:
        value = value.split(":", 1)[0]
    return value in LOCAL_TOKEN_PORTS_OK


@app.get("/api/wb/local-token")
def local_token(request: Request):
    """仅本机回环可读 803 管理令牌（方便首次连接；不写入日志、不回传前端以外的地方）。"""
    if str(os.environ.get("IDEA_DISABLE_LOCAL_TOKEN") or "").strip().lower() in (
            "1", "true", "yes", "on"):
        raise HTTPException(status_code=403, detail="local token disabled")
    host = request.headers.get("host") or ""
    peer = request.client.host if request.client else ""
    if not (_is_loopback_host(host) and _is_loopback_host(peer)):
        raise HTTPException(status_code=403, detail="loopback only")
    return {"ok": True, "token": idea_config.admin_token(),
            "config_path": str(idea_config.CONFIG_PATH)}


## 启动自检
@app.on_event("startup")
def _print_access_url() -> None:
    """启动时打印一次含 token 的访问 URL（token 落 idea/config.json，不进仓库）。"""
    token = idea_config.admin_token()
    print(f"[803] IDEA 工作台：http://127.0.0.1:803/?token={token}", flush=True)
    print(f"[803] token 保存在 {idea_config.CONFIG_PATH}（已 gitignore）；"
          "可用环境变量 IDEA_TOKEN 覆盖。", flush=True)


# ------------------------------------------------------------------ 请求模型
class ConfigSaveBody(BaseModel):
    patch: dict[str, Any]


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


# ------------------------------------------------------------------ 基础
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response as _Resp
    return _Resp(status_code=204)


@app.get("/health")
def health():
    return {"ok": True, "service": "idea-workbench", "version": "0.2.0",
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


@app.get("/api/wb/health")
def workbench_health():
    """桌面启动器/健康检查用：不需要 token，只报活着。"""
    return {"ok": True, "service": "idea-workbench"}


@app.get("/api/wb/overview", dependencies=[Depends(require_token)])
def overview():
    svc = services.list_services()
    cfg = config_store.load_view()
    models = modelstudio.status()
    train_jobs = trainstudio.list_jobs(limit=5)
    return {
        "ok": True,
        "soulagent": str(paths.soulagent_home()),
        "agent_home": str(paths.agent_home()),
        "config_path": cfg["path"], "config_mtime": cfg["mtime"],
        "services": svc["items"],
        "models": {"running": models.get("running"), "disk": models.get("disk")},
        "train_jobs": train_jobs.get("items", []),
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


@app.get("/api/wb/terminal", dependencies=[Depends(require_token)])
def terminal(lines: int = Query(default=120, ge=10, le=500)):
    """终端面板数据源：只读聚合 803 托管服务的日志尾部（不执行任何 shell）。"""
    return termlog.collect(lines)


# ------------------------------------------------------------------ 配置
@app.get("/api/wb/config", dependencies=[Depends(require_token)])
def config_get():
    return config_store.load_view()


@app.post("/api/wb/config/validate", dependencies=[Depends(require_token)])
def config_validate(body: ConfigSaveBody):
    return config_store.validate(body.patch)


@app.post("/api/wb/config/save", dependencies=[Depends(require_token)])
def config_save(body: ConfigSaveBody):
    return config_store.save(body.patch, actor="workbench")


@app.get("/api/wb/config/backups", dependencies=[Depends(require_token)])
def config_backups():
    return {"ok": True, "items": config_store.backups()}


@app.post("/api/wb/config/restore", dependencies=[Depends(require_token)])
def config_restore(body: dict):
    name = str((body or {}).get("name") or "")
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name")
    return config_store.restore_backup(name, actor="workbench")


# ------------------------------------------------------------------ Data Studio
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



# ------------------------------------------------------------------ 蒸馏语义数据
@app.get("/api/wb/distill/datasets", dependencies=[Depends(require_token)])
def distill_datasets():
    return distillstudio.list_datasets()


@app.get("/api/wb/distill/jobs", dependencies=[Depends(require_token)])
def distill_jobs(limit: int = 20):
    return distillstudio.list_jobs(limit=limit)


@app.post("/api/wb/distill/create", dependencies=[Depends(require_token)])
def distill_create(body: dict | None = None):
    body = body or {}
    # B7：默认真实生成（会调 DeepSeek，必须带确认短语）；模拟模式必须显式 real=false。
    real = bool(body["real"]) if "real" in body else True
    out = distillstudio.create_job(
        limit=body.get("limit") or 300, real=real,
        out_name=body.get("out_name") or "", budget_cny=body.get("budget_cny") or 3.05,
        split=bool(body.get("split")), max_variants=body.get("max_variants") or 7,
        confirm=body.get("confirm") or "", overwrite=bool(body.get("overwrite")))
    if out.get("ok"):
        audit.record("distill.create", (out.get("job") or {}).get("id") or "",
                     actor="workbench",
                     meta={"limit": body.get("limit"), "real": real})
    return out


@app.get("/api/wb/distill/job/{job_id}", dependencies=[Depends(require_token)])
def distill_job(job_id: str):
    return distillstudio.get_job(job_id)


@app.get("/api/wb/distill/job/{job_id}/log", dependencies=[Depends(require_token)])
def distill_job_log(job_id: str, tail: int = 200):
    return distillstudio.read_log(job_id, tail=tail)


@app.post("/api/wb/distill/job/{job_id}/cancel", dependencies=[Depends(require_token)])
def distill_job_cancel(job_id: str):
    return distillstudio.cancel_job(job_id)

# ------------------------------------------------------------------ 训练工厂
@app.get("/api/wb/train/discover", dependencies=[Depends(require_token)])
def train_discover():
    return trainstudio.discover()


@app.get("/api/wb/train/datasets", dependencies=[Depends(require_token)])
def train_datasets():
    return trainstudio.list_datasets()


@app.get("/api/wb/train/sources", dependencies=[Depends(require_token)])
def train_sources():
    return trainstudio.list_sources()


@app.post("/api/wb/train/export_chat", dependencies=[Depends(require_token)])
def train_export_chat(body: dict):
    out = trainstudio.export_chat(
        str(body.get("source_db") or ""),
        out_name=str(body.get("out_name") or ""),
        exclude_test=bool(body.get("exclude_test", True)),
        max_rows=int(body.get("max_rows") or 2000),
        window=int(body.get("window") or 6),
        split=float(body.get("split") or 0.0))
    if out.get("ok"):
        audit.record("train.export_chat", str(out.get("out") or ""), actor="workbench")
    return out


@app.get("/api/wb/train/assets", dependencies=[Depends(require_token)])
def train_assets():
    return trainstudio.list_assets()


@app.get("/api/wb/train/preview", dependencies=[Depends(require_token)])
def train_preview(path: str, limit: int = Query(default=20, ge=1, le=200)):
    return trainstudio.preview_dataset(path, limit=limit)


@app.post("/api/wb/train/filter", dependencies=[Depends(require_token)])
def train_filter(body: dict):
    return trainstudio.filter_dataset(
        str(body.get("path") or ""), name=str(body.get("name") or ""),
        min_chars=int(body.get("min_chars") or 0),
        max_chars=int(body.get("max_chars") or 0),
        blacklist=body.get("blacklist") or [],
        dedupe=bool(body.get("dedupe", True)))


@app.post("/api/wb/train/create", dependencies=[Depends(require_token)])
def train_create(body: dict):
    out = trainstudio.create_training_job(
        str(body.get("dataset") or ""),
        model_path=str(body.get("model_path") or ""),
        out_name=str(body.get("out_name") or ""),
        steps=int(body.get("steps") or 0),
        lr=float(body.get("lr") or 2e-4),
        lora_r=int(body.get("lora_r") or 16),
        lora_alpha=int(body.get("lora_alpha") or 32),
        load_4bit=bool(body.get("load_4bit", False)),
        max_len=int(body.get("max_len") or 512),
        epochs=int(body.get("epochs") or 1),
        format=str(body.get("format") or "auto"),
        mode=str(body.get("mode") or "lora"))
    if out.get("ok"):
        audit.record("train.create", str(out.get("job", {}).get("id") or ""), actor="workbench",
                     meta={"dataset": body.get("dataset")})
    return out


@app.post("/api/wb/train/start", dependencies=[Depends(require_token)])
def train_start(body: dict):
    job_id = str(body.get("id") or body.get("job_id") or "")
    out = trainstudio.start_job(job_id)
    if out.get("ok"):
        audit.record("train.start", job_id, actor="workbench")
    return out


@app.post("/api/wb/train/stop", dependencies=[Depends(require_token)])
def train_stop(body: dict):
    out = trainstudio.stop_job(str(body.get("id") or ""))
    if out.get("ok"):
        audit.record("train.stop", str(body.get("id")), actor="workbench")
    return out


@app.get("/api/wb/train/jobs", dependencies=[Depends(require_token)])
def train_jobs(limit: int = Query(default=30, ge=1, le=200)):
    return trainstudio.list_jobs(limit=limit)


@app.get("/api/wb/train/job/{job_id}", dependencies=[Depends(require_token)])
def train_job(job_id: str, tail: int = Query(default=80, ge=0, le=5000)):
    return trainstudio.job_status(job_id, tail=tail)


@app.get("/api/wb/train/job/{job_id}/log", dependencies=[Depends(require_token)])
def train_job_log(job_id: str, lines: int = Query(default=100, ge=1, le=5000)):
    return trainstudio.tail_log(job_id, lines=lines)


@app.post("/api/wb/train/export/preview", dependencies=[Depends(require_token)])
def train_export_preview(body: dict):
    return trainstudio.export_plan(str(body.get("adapter") or ""),
                                   str(body.get("base") or ""),
                                   str(body.get("name") or ""),
                                   out_dir=str(body.get("out_dir") or "") or trainstudio.DEFAULT_GGUF_DIR,
                                   merged_dir=str(body.get("merged_dir") or ""),
                                   quant=str(body.get("quant") or "Q4_K_M"))


@app.post("/api/wb/train/export", dependencies=[Depends(require_token)])
def train_export(body: dict):
    out = trainstudio.start_export(str(body.get("adapter") or ""),
                                   str(body.get("base") or ""),
                                   str(body.get("name") or ""),
                                   out_dir=str(body.get("out_dir") or "") or trainstudio.DEFAULT_GGUF_DIR,
                                   merged_dir=str(body.get("merged_dir") or ""),
                                   quant=str(body.get("quant") or "Q4_K_M"))
    if out.get("ok"):
        audit.record("train.export", str(out.get("job", {}).get("id") or ""), actor="workbench")
    return out


# ------------------------------------------------------------------ 语料清洗
@app.post("/api/wb/corpus/jobs", dependencies=[Depends(require_token)])
def corpus_new_job(body: dict | None = None):
    return corpusstudio.new_job(name=str((body or {}).get("name") or ""))


@app.get("/api/wb/corpus/jobs", dependencies=[Depends(require_token)])
def corpus_jobs():
    return corpusstudio.list_jobs()


@app.get("/api/wb/corpus/job/{job_id}", dependencies=[Depends(require_token)])
def corpus_job(job_id: str):
    return corpusstudio.get_job(job_id)


@app.delete("/api/wb/corpus/job/{job_id}", dependencies=[Depends(require_token)])
def corpus_job_delete(job_id: str):
    return corpusstudio.delete_job(job_id)


@app.post("/api/wb/corpus/job/{job_id}/upload", dependencies=[Depends(require_token)])
async def corpus_upload(job_id: str, file: UploadFile = File(...)):
    data = await file.read()
    return corpusstudio.save_upload(job_id, file.filename or "upload.bin", data)


@app.post("/api/wb/corpus/job/{job_id}/parse", dependencies=[Depends(require_token)])
def corpus_parse(job_id: str, body: dict | None = None):
    """异步清洗：立即返回 started，前端轮询 /parse/status；body.sync=true 时同步返回结果。"""
    body = body or {}
    me = body.get("me_names") or "我,me,assistant,小鱼,她"
    you = body.get("you_names") or "你,user,用户,对方"
    if body.get("sync"):
        out = corpusstudio.parse_job(job_id, me_names=me, you_names=you)
        if out.get("ok"):
            audit.record("corpus.parse", job_id, actor="workbench",
                         meta={"total": (out.get("stats") or {}).get("total")})
        return out
    out = corpusstudio.start_parse(job_id, me_names=me, you_names=you)
    if out.get("ok"):
        audit.record("corpus.parse_start", job_id, actor="workbench",
                     meta={"total": out.get("total")})
    return out


@app.get("/api/wb/corpus/job/{job_id}/parse/status", dependencies=[Depends(require_token)])
def corpus_parse_status(job_id: str):
    return corpusstudio.parse_status(job_id)


@app.get("/api/wb/corpus/job/{job_id}/items", dependencies=[Depends(require_token)])
def corpus_items(job_id: str):
    return corpusstudio.items_view(job_id)


@app.post("/api/wb/corpus/job/{job_id}/save", dependencies=[Depends(require_token)])
def corpus_save(job_id: str, body: dict):
    out = corpusstudio.save_dataset(
        job_id, body.get("selected_ids") or [],
        format=str(body.get("format") or "messages"),
        window=int(body.get("window") or 6),
        split=float(body.get("split") or 0.0),
        out_name=str(body.get("out_name") or ""),
        system=str(body.get("system") or ""))
    if out.get("ok"):
        audit.record("corpus.save", str(out.get("out") or ""), actor="workbench")
    return out


# ------------------------------------------------------------------ 用户标注
@app.get("/api/wb/annot/source/{job_id}", dependencies=[Depends(require_token)])
def annot_source(job_id: str, window: int = Query(default=6, ge=1, le=20)):
    return annotstudio.source_view(job_id, window=window)


@app.post("/api/wb/annot/save", dependencies=[Depends(require_token)])
def annot_save(body: dict):
    out = annotstudio.save_annotations(
        body.get("source"), body.get("annotations") or [],
        window=int(body.get("window") or 6),
        out_name=str(body.get("out_name") or ""),
        min_score=int(body.get("min_score") or 0),
        split=float(body.get("split") if body.get("split") is not None else 0.1),
        system=str(body.get("system") or ""))
    if out.get("ok"):
        audit.record("annot.save", str(out.get("out") or ""), actor="workbench")
    return out


# ------------------------------------------------------------------ 模型中心
@app.get("/api/wb/models", dependencies=[Depends(require_token)])
def models_list():
    return modelstudio.list_models()


@app.get("/api/wb/models/list", dependencies=[Depends(require_token)])
def models_list_alias():
    return modelstudio.list_models()


@app.get("/api/wb/models/inventory", dependencies=[Depends(require_token)])
def models_inventory_alias():
    return modelstudio.model_inventory()


@app.get("/api/wb/models/running", dependencies=[Depends(require_token)])
def models_running():
    return modelstudio.running_models()


@app.get("/api/wb/models/adapters", dependencies=[Depends(require_token)])
def models_adapters():
    return modelstudio.list_adapters()


@app.get("/api/wb/models/disk", dependencies=[Depends(require_token)])
def models_disk():
    return modelstudio.disk_usage()


@app.get("/api/wb/models/status", dependencies=[Depends(require_token)])
def models_status():
    return modelstudio.status()


@app.post("/api/wb/models/start", dependencies=[Depends(require_token)])
def models_start(body: dict):
    out = modelstudio.start_model(str(body.get("path") or ""),
                                  int(body.get("port") or 0),
                                  ngl=int(body.get("ngl") or 99),
                                  ctx=int(body.get("ctx") or 4096))
    if out.get("ok"):
        audit.record("model.start", str(out.get("model_path") or body.get("path")),
                     actor="workbench", meta={"port": out.get("port")})
    return out


@app.post("/api/wb/models/stop", dependencies=[Depends(require_token)])
def models_stop(body: dict):
    out = modelstudio.stop_model(int(body.get("port") or 0))
    if out.get("ok"):
        audit.record("model.stop", str(body.get("port")), actor="workbench")
    return out


@app.post("/api/wb/models/merge", dependencies=[Depends(require_token)])
def models_merge(body: dict):
    return modelstudio.merge_adapter(str(body.get("adapter") or ""),
                                     out=body.get("out") or None,
                                     base=body.get("base") or None,
                                     device=str(body.get("device") or "cpu"),
                                     dtype=str(body.get("dtype") or "float16"))


@app.post("/api/wb/models/import", dependencies=[Depends(require_token)])
def models_import(body: dict):
    out = modelstudio.import_model(body)
    if out.get("ok"):
        audit.record("model.import", str(out.get("name") or ""), actor="workbench")
    return out


@app.post("/api/wb/models/delete", dependencies=[Depends(require_token)])
def models_delete(body: dict):
    out = modelstudio.delete_model(body)
    if out.get("ok"):
        audit.record("model.delete", str(out.get("moved_to") or ""), actor="workbench")
    return out


@app.post("/api/wb/models/download", dependencies=[Depends(require_token)])
def models_download(body: dict):
    out = modelstudio.download_model(body)
    if out.get("ok"):
        audit.record("model.download", str(out.get("job_id") or ""), actor="workbench")
    return out


@app.get("/api/wb/models/downloads", dependencies=[Depends(require_token)])
def models_downloads(job_id: str | None = None):
    return modelstudio.download_status(job_id)


@app.post("/api/wb/models/download/cancel", dependencies=[Depends(require_token)])
def models_download_cancel(body: dict):
    return modelstudio.cancel_download(str(body.get("job_id") or ""))


@app.post("/api/wb/models/validate", dependencies=[Depends(require_token)])
def models_validate(body: dict):
    return modelstudio.validate_model_path(str(body.get("path") or ""))


@app.post("/api/wb/models/convert", dependencies=[Depends(require_token)])
def models_convert(body: dict):
    return modelstudio.convert_gguf(str(body.get("merged_dir") or ""),
                                    quant=str(body.get("quant") or "Q4_K_M"),
                                    name=body.get("name") or None,
                                    out_dir=body.get("out_dir") or None)


# ------------------------------------------------------------------ 审计
@app.get("/api/wb/audit", dependencies=[Depends(require_token)])
def audit_list(limit: int = Query(default=100, ge=1, le=500)):
    return {"ok": True, "items": audit.list_recent(limit)}


# ------------------------------------------------------------------ V1 Bot Manager
class BotCreateBody(BaseModel):
    bot_id: str
    name: str = ""
    plan: str = "cloud"
    soul_provider: str = "cloud"
    llm_api_key: str = ""
    soul_api_base: str = ""
    soul_model: str = ""
    soul_api_key: str = ""
    password: str = ""


class BotSaveBody(BaseModel):
    name: str | None = None
    plan: str | None = None
    soul_provider: str | None = None
    llm_api_key: str | None = None
    soul_api_base: str | None = None
    soul_model: str | None = None
    soul_api_key: str | None = None
    password: str | None = None
    settings: dict | None = None


@app.get("/bots")
def bots_page():
    return FileResponse(str(paths.WEB_DIR / "bots.html"))


@app.get("/api/bots")
def api_bots(_: bool = Depends(require_token)):
    return {"ok": True, "items": bot_manager.list_bots(),
            "root": str(bot_manager.bots_root())}


@app.post("/api/bots")
def api_bot_create(body: BotCreateBody, _: bool = Depends(require_token)):
    try:
        return bot_manager.create_bot(
            body.bot_id, name=body.name, plan=body.plan,
            soul_provider=body.soul_provider, llm_key=body.llm_api_key,
            soul_base=body.soul_api_base, soul_model=body.soul_model,
            soul_key=body.soul_api_key, password=body.password)
    except bot_manager.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/bots/{bot_id}")
def api_bot_get(bot_id: str, _: bool = Depends(require_token)):
    try:
        return {"ok": True, "bot": bot_manager.get_bot(bot_id)}
    except bot_manager.BotError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/bots/{bot_id}/save")
def api_bot_save(bot_id: str, body: BotSaveBody,
                 _: bool = Depends(require_token)):
    try:
        return bot_manager.save_bot(bot_id, body.model_dump(exclude_none=True))
    except bot_manager.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/selfcheck")
def api_selfcheck(bot_id: str = "", models: int = 0,
                  _: bool = Depends(require_token)):
    """PR10 自检：默认只查端口/bot 包（快、免费）；``models=1`` 才测模型通道。"""
    try:
        return bot_manager.selfcheck(bot_id or None, models=bool(models))
    except bot_manager.BotError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/bots/{bot_id}/load")
def api_bot_load(bot_id: str, _: bool = Depends(require_token)):
    try:
        return bot_manager.load_bot(bot_id)
    except bot_manager.BotError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ModelCheckBody(BaseModel):
    purposes: list[str] = ["chat", "soul"]


@app.post("/api/bots/{bot_id}/model_check")
def api_bot_model_check(bot_id: str, body: ModelCheckBody | None = None,
                        _: bool = Depends(require_token)):
    """PR8：按 bot 既有路由做最小模型调用，返回各通道连通性（不含密钥）。"""
    try:
        purposes = tuple(body.purposes) if body else ("chat", "soul")
        return bot_manager.model_check(bot_id, purposes=purposes)
    except bot_manager.BotError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/bots/{bot_id}/validate")
def api_bot_validate(bot_id: str, _: bool = Depends(require_token)):
    try:
        return bot_manager.validate_bot(bot_id)
    except bot_manager.BotError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/bots/{bot_id}/launch")
def api_bot_launch(bot_id: str, port: int = 802,
                   _: bool = Depends(require_token)):
    try:
        return bot_manager.launch_bot(bot_id, port=port)
    except bot_manager.BotError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.exception_handler(Exception)
def _unhandled(_request, exc):  # pragma: no cover - 兜底
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.get("/")
def index():
    return FileResponse(str(paths.WEB_DIR / "index.html"))


app.mount("/web", StaticFiles(directory=str(paths.WEB_DIR)), name="web")
