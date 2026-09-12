import secrets
import httpx

from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import config
from app.database import engine
from app.models import ProxyNode, HealthCheckLog
from app.services import proxy_manager, traffic_logger

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
    pull_count = traffic_logger.get_total_pull_count()
    pull_history = traffic_logger.get_pull_history(limit=200)
    total_download = sum(s.download_bytes for s in stats)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": config.app.name,
            "app_tagline": config.app.tagline,
            "proxies": [p.model_dump(mode="json") for p in proxies],
            "stats": [s.model_dump(mode="json") for s in stats],
            "total_download": total_download,
            "pull_count": pull_count,
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
    await proxy_manager.check_and_update_node(node)
    return {"status": "ok", "node": node.model_dump(mode="json")}


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
    node = proxy_manager.update_proxy(
        proxy_id, name, url, registry_type, route_prefix, username, password
    )
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
    updated = await proxy_manager.check_and_update_node(node)
    return updated.model_dump(mode="json")


# ==================== 批量操作 ====================

@router.post("/api/proxies/fetch")
async def fetch_proxies():
    count = await proxy_manager.fetch_and_update_proxies()
    return {"status": "ok", "added": count}


@router.post("/api/test-speed")
async def trigger_speed_test():
    await proxy_manager.run_health_check()
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


# ==================== 健康检查日志 ====================

@router.get("/api/health-logs/{node_id}")
async def get_health_logs(node_id: int, limit: int = 50):
    with Session(engine) as session:
        logs = session.exec(
            select(HealthCheckLog)
            .where(HealthCheckLog.node_id == node_id)
            .order_by(HealthCheckLog.check_time.desc())
            .limit(limit)
        ).all()
    return [l.model_dump(mode="json") for l in logs]


# ==================== 镜像搜索 ====================

@router.get("/api/search")
async def search_images(q: str):
    url = f"https://hub.docker.com/v2/search/repositories/?query={q}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url)
            return JSONResponse(content=resp.json())
        except Exception:
            return JSONResponse(content={"results": []}, status_code=500)