import shutil
import secrets
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app import __version__
from app.config import config, CONFIG_PATH, AppConfig, reload_config
from app.database import engine
from app.models import ProxyNode, HealthCheckLog
from app.services import proxy_manager, traffic_logger, search_service

security = HTTPBasic(auto_error=False)


def verify_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if config.admin.user and config.admin.pass_:
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": 'Basic realm="Restricted Area"'},
            )
        ok_user = secrets.compare_digest(credentials.username, config.admin.user)
        ok_pass = secrets.compare_digest(credentials.password, config.admin.pass_)
        if not (ok_user and ok_pass):
            raise HTTPException(
                status_code=401,
                detail="用户名或密码错误",
                headers={"WWW-Authenticate": 'Basic realm="Restricted Area"'},
            )
    return True


router = APIRouter(dependencies=[Depends(verify_auth)])
templates = Jinja2Templates(directory="app/templates")


# ==================== 页面 ====================


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    proxies = proxy_manager.get_all_proxies()
    stats = traffic_logger.get_traffic_stats()
    pull_stats = traffic_logger.get_pull_stats()
    pull_history = traffic_logger.get_pull_history(limit=200)
    total_download = sum(s.download_bytes for s in stats)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": config.app.name,
            "app_tagline": config.app.tagline,
            "app_version": __version__,
            "current_year": datetime.now().year,  # 版权结束年份（即使与起始年相同也照常显示）
            "proxies": [p.model_dump(mode="json") for p in proxies],
            "stats": [s.model_dump(mode="json") for s in stats],
            "total_download": total_download,
            "pull_stats": pull_stats,
            "pull_history": [p.model_dump(mode="json") for p in pull_history],
        },
    )


# ==================== 节点 API ====================


@router.get("/api/proxies")
async def list_proxies():
    proxies = proxy_manager.get_all_proxies()
    return [p.model_dump(mode="json") for p in proxies]


@router.post("/api/proxies")
async def add_proxy_node(
    name: str = Form(...),
    url: str = Form(...),
    registry_type: str = Form("dockerhub"),
    route_prefix: str = Form(None),
    username: str = Form(None),
    password: str = Form(None),
):
    if not url.startswith("http"):
        raise HTTPException(400, "URL 无效")
    node = proxy_manager.add_proxy(name, url, registry_type, route_prefix, username, password)
    # 单节点添加后：先在线检测，再速度测试
    await proxy_manager._check_one_alive(node.id)
    with Session(engine) as session:
        node = session.get(ProxyNode, node.id)
    if node and node.enabled:
        await proxy_manager._test_one_speed(node)
    return {"status": "ok", "node": node.model_dump(mode="json") if node else None}


@router.put("/api/proxies/{proxy_id}")
async def update_proxy_node(
    proxy_id: int,
    name: str = Form(...),
    url: str = Form(...),
    registry_type: str = Form("dockerhub"),
    route_prefix: str = Form(None),
    username: str = Form(None),
    password: str = Form(None),
):
    if not url.startswith("http"):
        raise HTTPException(400, "URL 无效")
    node = proxy_manager.update_proxy(proxy_id, name, url, registry_type, route_prefix, username, password)
    if not node:
        raise HTTPException(404, "节点不存在")
    return {"status": "ok"}


@router.delete("/api/proxies/{proxy_id}")
async def delete_proxy_node(proxy_id: int):
    if not proxy_manager.delete_proxy(proxy_id):
        raise HTTPException(404, "节点不存在")
    return {"status": "ok"}


@router.post("/api/proxies/{proxy_id}/disable")
async def disable_proxy(proxy_id: int, reason: str = Form("")):
    node = proxy_manager.set_manual_disable(proxy_id, True, reason)
    if not node:
        raise HTTPException(404, "节点不存在")
    return {"status": "ok", "node": node.model_dump(mode="json")}


@router.post("/api/proxies/{proxy_id}/enable")
async def enable_proxy(proxy_id: int):
    node = proxy_manager.set_manual_disable(proxy_id, False)
    if not node:
        raise HTTPException(404, "节点不存在")
    return {"status": "ok", "node": node.model_dump(mode="json")}


@router.post("/api/proxies/{proxy_id}/test")
async def test_single_proxy(proxy_id: int):
    with Session(engine) as session:
        node = session.get(ProxyNode, proxy_id)
        if not node:
            raise HTTPException(404, "节点不存在")

    # 先在线检测，再速度测试
    await proxy_manager._check_one_alive(proxy_id)
    with Session(engine) as session:
        node = session.get(ProxyNode, proxy_id)

    if node and node.enabled:
        await proxy_manager._test_one_speed(node)
    with Session(engine) as session:
        node = session.get(ProxyNode, proxy_id)
    return node.model_dump(mode="json") if node else {}


# ==================== 批量操作 ====================


@router.post("/api/proxies/fetch")
async def fetch_proxies():
    """手动获取免费节点：拉取 → 在线检测 → 速度测试"""
    count = await proxy_manager.fetch_and_update_proxies()
    await proxy_manager.run_health_check()
    await proxy_manager.run_speed_test()
    return {"status": "ok", "added": count}


@router.post("/api/test-health")
async def trigger_health_check(request: Request):
    """手动触发在线检测（可选只测指定节点）。"""
    ids = None
    try:
        data = await request.json()
        if isinstance(data, dict):
            ids = data.get("ids") or None
    except Exception:
        pass
    await proxy_manager.run_health_check(ids=ids)
    return {"status": "ok"}


@router.post("/api/test-speed")
async def trigger_speed_test(request: Request):
    """手动触发速度测试（可选只测指定节点）。"""
    ids = None
    try:
        data = await request.json()
        if isinstance(data, dict):
            ids = data.get("ids") or None
    except Exception:
        pass
    await proxy_manager.run_health_check(ids=ids)
    await proxy_manager.run_speed_test(ids=ids)
    return {"status": "ok"}


@router.post("/api/proxies/batch-disable")
async def batch_disable(request: Request):
    data = await request.json()
    ids = data.get("ids", [])
    reason = data.get("reason", "")
    for pid in ids:
        proxy_manager.set_manual_disable(pid, True, reason)
    return {"status": "ok", "count": len(ids)}


@router.post("/api/proxies/batch-enable")
async def batch_enable(request: Request):
    data = await request.json()
    ids = data.get("ids", [])
    for pid in ids:
        proxy_manager.set_manual_disable(pid, False)
    return {"status": "ok", "count": len(ids)}


# ==================== 导入导出 ====================


@router.get("/api/proxies/export")
async def export_proxies():
    proxies = proxy_manager.get_all_proxies()
    content = [p.model_dump(mode="json") for p in proxies]
    return JSONResponse(
        content=content,
        headers={"Content-Disposition": "attachment; filename=proxies.json"},
    )


@router.post("/api/proxies/import")
async def import_proxies(request: Request):
    try:
        data = await request.json()
        if not isinstance(data, list):
            raise ValueError("需要节点列表")
        imported = 0
        for item in data:
            if not item.get("url"):
                continue
            proxy_manager.add_proxy(
                name=item.get("name", "imported"),
                url=item["url"],
                registry_type=item.get("registry_type", "dockerhub"),
                route_prefix=item.get("route_prefix"),
                username=item.get("username"),
                password=item.get("password"),
            )
            imported += 1
        return {"status": "ok", "imported": imported}
    except Exception as e:
        raise HTTPException(400, f"导入失败: {e}")


# ==================== 拉取记录 ====================


@router.get("/api/pulls")
async def get_pulls(limit: int = 500):
    pulls = traffic_logger.get_pull_history(limit=limit)
    return [p.model_dump(mode="json") for p in pulls]


@router.delete("/api/pulls")
async def clear_pulls():
    traffic_logger.clear_pull_history()
    return {"status": "ok"}


# ==================== 在线检测日志 ====================


@router.get("/api/health-logs/{node_id}")
async def get_health_logs(node_id: int, limit: int = 50):
    with Session(engine) as session:
        logs = session.exec(
            select(HealthCheckLog).where(HealthCheckLog.node_id == node_id).order_by(HealthCheckLog.check_time.desc()).limit(limit)
        ).all()
    return [l.model_dump(mode="json") for l in logs]


# ==================== 任务进度 ====================


@router.get("/api/tasks/status")
async def tasks_status():
    """返回当前正在运行的任务进度（供前端轮询）。"""
    return proxy_manager.get_progress()


# ==================== 镜像搜索 ====================


@router.get("/api/search")
async def search_images(q: str, page_size: int = None):
    if page_size is None:
        page_size = config.search.page_size
    result = await search_service.search_docker_hub(q, page_size)
    return JSONResponse(content=result)


# ============================================================
#  配置文件管理 API
# ============================================================


@router.get("/api/config")
async def get_config():
    if not CONFIG_PATH.exists():
        raise HTTPException(404, f"配置文件不存在: {CONFIG_PATH}")

    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"读取配置失败: {e}")

    try:
        data = yaml.safe_load(text) or {}
    except Exception as e:
        raise HTTPException(500, f"解析配置失败: {e}")

    return {
        "yaml": text,
        "config": data,  # 结构化配置，供表单使用
        "path": str(CONFIG_PATH.resolve()),
        "restart_required_fields": [
            "server.host",
            "server.port",
            "server.debug",
            "logging.*",
            "auto_fetch.interval_minutes",
            "health_check.interval_minutes",
            "speed_test.interval_minutes",
        ],
    }


@router.put("/api/config")
async def update_config(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"请求体不是合法 JSON: {e}")

    yaml_text: str | None = None
    parsed: dict | None = None

    # ---- 模式 1：结构化 config 对象（新表单用） ----
    if isinstance(body.get("config"), dict):
        parsed = body["config"]
        try:
            yaml_text = yaml.dump(
                parsed,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        except Exception as e:
            raise HTTPException(400, f"序列化配置失败: {e}")

    # ---- 模式 2：原始 YAML 文本（兼容旧接口） ----
    elif isinstance(body.get("yaml"), str) and body["yaml"].strip():
        yaml_text = body["yaml"]
        try:
            parsed = yaml.safe_load(yaml_text)
        except yaml.YAMLError as e:
            raise HTTPException(400, f"YAML 语法错误: {e}")

    else:
        raise HTTPException(400, "请求体必须包含 'config' 或 'yaml' 字段")

    if not isinstance(parsed, dict):
        raise HTTPException(400, "配置根节点必须是字典（mapping）")

    # ---- 校验 ----
    try:
        AppConfig(**parsed)
    except Exception as e:
        raise HTTPException(400, f"配置校验失败: {e}")

    # ---- 备份 ----
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    backup_ok = False
    if CONFIG_PATH.exists():
        try:
            shutil.copy2(CONFIG_PATH, backup_path)
            backup_ok = True
        except Exception:
            pass

    # ---- 写入 ----
    try:
        CONFIG_PATH.write_text(yaml_text, encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"写入配置失败: {e}")

    # ---- 重载 ----
    try:
        reload_config()
    except Exception as e:
        if backup_ok:
            try:
                shutil.copy2(backup_path, CONFIG_PATH)
                reload_config()
            except Exception:
                pass
        raise HTTPException(500, f"配置重载失败（已回滚）: {e}")

    try:
        proxy_manager.init_proxies()
    except Exception:
        pass

    return {
        "status": "ok",
        "message": "配置已保存并重载",
        "backup": str(backup_path) if backup_ok else None,
        "restart_required": True,
    }


@router.post("/api/config/reload")
async def reload_config_endpoint():
    try:
        reload_config()
        proxy_manager.init_proxies()
    except Exception as e:
        raise HTTPException(500, f"重载失败: {e}")
    return {"status": "ok", "message": "配置已重载"}


@router.get("/api/config/backup")
async def download_backup():
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if not backup_path.exists():
        raise HTTPException(404, "没有备份文件")
    try:
        text = backup_path.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"读取备份失败: {e}")
    return JSONResponse(content={"yaml": text, "path": str(backup_path)})
