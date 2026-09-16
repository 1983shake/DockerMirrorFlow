import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx
from sqlmodel import Session, select

from app.database import engine
from app.models import ProxyNode, HealthCheckLog, get_shanghai_time
from app.config import config

logger = logging.getLogger("dockermirrorflow.proxy_manager")

# ============================================================
#  registry 类型映射
# ============================================================
REGISTRY_TYPE_MAP = {
    "hub": "dockerhub",
    "ghcr": "ghcr",
    "quay": "quay",
    "mcr": "mcr",
    "gcr": "gcr",
    "elastic": "elastic",
    "nvcr": "nvcr",
}

ROUTE_PREFIX_MAP = {
    "ghcr": "ghcr",
    "quay": "quay",
    "mcr": "mcr",
    "gcr": "gcr",
    "elastic": "elastic",
    "nvcr": "nvcr",
}

DEFAULT_ROUTE_ALIASES: dict[str, list[str]] = {
    "dockerhub": ["docker.io", "registry-1.docker.io", "index.docker.io"],
    "ghcr": ["ghcr.io", "ghcr"],
    "gcr": ["gcr.io", "k8s.gcr.io", "registry.k8s.io", "gcr"],
    "quay": ["quay.io", "quay"],
    "mcr": ["mcr.microsoft.com", "mcr"],
    "nvcr": ["nvcr.io", "nvcr"],
    "elastic": ["docker.elastic.co", "elastic"],
}


# ============================================================
#  内存状态（不写数据库）
# ============================================================

_failed_until: dict[int, float] = {}
_blob_failed_until: dict[str, float] = {}
_last_success_at: dict[int, float] = {}
_image_affinity: dict[str, tuple[int, float]] = {}
_probe_affinity: Optional[tuple[int, float]] = None

# 缓存上限（优化待机内存占用）
_MAX_BLOB_CACHE = 2000
_MAX_AFFINITY_CACHE = 1000

# 速度测试 token 缓存
_speed_token_cache: dict[str, tuple[str, float]] = {}
_SPEED_TOKEN_TTL = 240
_MAX_SPEED_TOKEN_CACHE = 200


# ============================================================
#  任务进度跟踪（内存态，供 Web 实时反馈）
# ============================================================

_progress: dict[str, dict] = {}
_PROGRESS_TTL = 30  # 已完成任务状态保留秒数


def _progress_start(task: str, label: str, total: int = 0, message: str = ""):
    _progress[task] = {
        "label": label,
        "running": True,
        "done": 0,
        "total": total,
        "message": message,
        "percent": 0,
        "started_at": time.time(),
        "updated_at": time.time(),
    }


def _progress_update(task: str, **kwargs):
    entry = _progress.get(task)
    if entry is None:
        entry = {
            "label": task,
            "running": True,
            "done": 0,
            "total": 0,
            "message": "",
            "percent": 0,
            "started_at": time.time(),
        }
        _progress[task] = entry
    entry.update(kwargs)
    total = entry.get("total") or 0
    done = entry.get("done") or 0
    entry["percent"] = int(done / total * 100) if total > 0 else 0
    entry["updated_at"] = time.time()


def _progress_finish(task: str, message: str = "完成"):
    entry = _progress.get(task)
    if entry is None:
        return
    entry["running"] = False
    entry["message"] = message
    entry["percent"] = 100
    entry["updated_at"] = time.time()


def get_progress() -> dict:
    """返回当前任务进度（自动清理已结束的旧任务）。"""
    now = time.time()
    for k in list(_progress.keys()):
        v = _progress[k]
        if not v.get("running") and now - v.get("updated_at", 0) > _PROGRESS_TTL:
            _progress.pop(k, None)
    return {k: dict(v) for k, v in _progress.items()}


# ============================================================
#  熔断机制
# ============================================================


def mark_node_failed(node_id: int, reason: str = "", cooldown: int = None):
    if node_id is None:
        return

    if cooldown is None:
        r = (reason or "").lower()
        if "readtimeout" in r or "connecttimeout" in r or "read timeout" in r:
            cooldown = config.proxy.timeout_fail_cooldown
        elif "403" in r or "forbidden" in r:
            cooldown = config.proxy.forbidden_fail_cooldown
        elif "500" in r or "502" in r or "503" in r or "504" in r:
            cooldown = config.proxy.server_err_fail_cooldown
        else:
            cooldown = config.proxy.fail_cooldown

    _failed_until[node_id] = time.time() + cooldown
    logger.warning(f"节点 {node_id} 失败，熔断 {cooldown}s: {reason}")


def mark_node_success(node_id: int):
    if node_id is None:
        return
    _failed_until.pop(node_id, None)
    _last_success_at[node_id] = time.time()


def mark_blob_failed(node_id: int, path: str, cooldown: int = None):
    if node_id is None or not path:
        return
    if cooldown is None:
        cooldown = config.proxy.blob_fail_cooldown

    key = f"{node_id}:{path}"
    _blob_failed_until[key] = time.time() + cooldown
    logger.info(f"节点 {node_id} 对 {path[:80]} 失败，冷却 {cooldown}s")

    if len(_blob_failed_until) > _MAX_BLOB_CACHE:
        _cleanup_blob_cache()


def is_blob_failed(node_id: int, path: str) -> bool:
    if node_id is None or not path:
        return False
    key = f"{node_id}:{path}"
    expire = _blob_failed_until.get(key, 0)
    if expire and time.time() >= expire:
        _blob_failed_until.pop(key, None)
        return False
    return time.time() < expire


def _cleanup_blob_cache():
    now = time.time()
    expired = [k for k, v in _blob_failed_until.items() if v < now]
    for k in expired:
        _blob_failed_until.pop(k, None)


def _is_node_available(node_id: int) -> bool:
    expire = _failed_until.get(node_id, 0)
    if expire and time.time() >= expire:
        _failed_until.pop(node_id, None)
        return True
    return time.time() >= expire


# ============================================================
#  镜像级粘性
# ============================================================


def _extract_image(path: str) -> Optional[str]:
    if not path:
        return None
    if "/manifests/" in path:
        return path.split("/manifests/")[0]
    if "/blobs/" in path:
        return path.split("/blobs/")[0]
    return None


def get_pinned_node_for_path(path: str) -> Optional[ProxyNode]:
    global _probe_affinity

    now = time.time()

    if not path:
        if _probe_affinity is None:
            return None
        node_id, expire = _probe_affinity
        if now >= expire:
            _probe_affinity = None
            return None
        with Session(engine) as session:
            node = session.get(ProxyNode, node_id)
            if node and node.enabled and not node.manually_disabled and _is_node_available(node_id):
                return node
        return None

    image = _extract_image(path)
    if not image:
        return None

    entry = _image_affinity.get(image)
    if not entry:
        return None

    node_id, expire = entry
    if now >= expire:
        _image_affinity.pop(image, None)
        return None

    with Session(engine) as session:
        node = session.get(ProxyNode, node_id)
        if node and node.enabled and not node.manually_disabled and _is_node_available(node_id):
            return node
    return None


def pin_node_for_path(path: str, node: ProxyNode):
    global _probe_affinity

    if node.id is None:
        return

    now = time.time()

    if not path:
        _probe_affinity = (node.id, now + config.proxy.probe_node_window)
        return

    image = _extract_image(path)
    if image:
        _image_affinity[image] = (node.id, now + config.proxy.affinity_window)
        if len(_image_affinity) > _MAX_AFFINITY_CACHE:
            _cleanup_affinity()


def _cleanup_affinity():
    now = time.time()
    expired = [k for k, v in _image_affinity.items() if v[1] < now]
    for k in expired:
        _image_affinity.pop(k, None)


# ============================================================
#  路由别名
# ============================================================


def _get_route_aliases() -> dict[str, list[str]]:
    if config.route_aliases:
        return config.route_aliases
    return DEFAULT_ROUTE_ALIASES


def _get_node_prefixes(node: ProxyNode) -> list[str]:
    prefixes: list[str] = []
    if node.route_prefix:
        prefixes.append(node.route_prefix)

    aliases = _get_route_aliases().get(node.registry_type or "dockerhub", [])
    prefixes.extend(aliases)

    seen: set[str] = set()
    result: list[str] = []
    for p in prefixes:
        p_lower = p.strip("/").lower()
        if p_lower and p_lower not in seen:
            seen.add(p_lower)
            result.append(p)
    return result


def _match_any_prefix(path: str, prefixes: list[str]) -> tuple[Optional[str], int]:
    best_prefix: Optional[str] = None
    best_consumed = 0
    path_lower = path.lower()

    for prefix in prefixes:
        p = prefix.strip("/").lower()
        if not p:
            continue

        if path_lower.startswith(p + "/"):
            consumed = len(p) + 1
            if consumed > best_consumed:
                best_consumed = consumed
                best_prefix = prefix

        elif path_lower.startswith(p + "."):
            slash_idx = path.find("/")
            if slash_idx != -1 and slash_idx > len(p):
                consumed = slash_idx + 1
                if consumed > best_consumed:
                    best_consumed = consumed
                    best_prefix = prefix

    return best_prefix, best_consumed


def _match_route_prefix(path: str, prefix: str) -> int:
    _, consumed = _match_any_prefix(path, [prefix])
    return consumed


def _path_has_registry_prefix(path: str) -> bool:
    """判断路径是否带有已知 registry 前缀（如 ghcr.io/...）"""
    if not path:
        return False
    path_lower = path.lower()
    for reg_aliases in _get_route_aliases().values():
        for alias in reg_aliases:
            a = alias.strip("/").lower()
            if not a:
                continue
            if path_lower.startswith(a + "/") or path_lower.startswith(a + "."):
                return True
    return False


# ============================================================
#  初始化
# ============================================================


def init_proxies():
    with Session(engine) as session:
        for cn in config.custom_nodes:
            existing = session.exec(select(ProxyNode).where(ProxyNode.url == cn.url)).first()
            if not existing:
                node = ProxyNode(
                    name=cn.name,
                    url=cn.url.rstrip("/"),
                    registry_type=cn.registry_type,
                    route_prefix=cn.route_prefix,
                    username=cn.username,
                    password=cn.password,
                    enabled=cn.enabled,
                    is_custom=True,
                )
                session.add(node)
                logger.info(f"添加自定义节点: {cn.name} ({cn.url})")
            else:
                existing.name = cn.name
                existing.registry_type = cn.registry_type
                existing.route_prefix = cn.route_prefix
                existing.username = cn.username
                existing.password = cn.password
                existing.enabled = cn.enabled
                existing.is_custom = True
                session.add(existing)

        for md in config.manually_disabled:
            node = session.exec(select(ProxyNode).where(ProxyNode.url == md.url)).first()
            if node:
                node.manually_disabled = True
                node.manual_disable_reason = md.reason
                node.enabled = False
                session.add(node)

        session.commit()


# ============================================================
#  自动拉取
# ============================================================


async def _fetch_for_registry(client: httpx.AsyncClient, registry_type: str) -> list[dict]:
    url = f"{config.auto_fetch.api_url}/status/{registry_type}"
    try:
        resp = await client.get(url, timeout=10.0)
        if resp.status_code != 200:
            logger.warning(f"拉取 {registry_type} 失败: HTTP {resp.status_code}")
            return []
        return resp.json()
    except Exception as e:
        logger.error(f"拉取 {registry_type} 异常: {e}")
        return []


async def fetch_and_update_proxies() -> int:
    if not config.auto_fetch.enabled:
        logger.info("自动拉取已禁用，跳过")
        return 0

    logger.info("开始自动拉取节点...")
    _progress_start("fetch", "获取免费节点", message="正在从上游拉取…")

    added_count = 0

    async with httpx.AsyncClient(timeout=15.0) as client:
        results = await asyncio.gather(*[_fetch_for_registry(client, rt) for rt in config.auto_fetch.registry_types])

    _progress_update("fetch", message="正在写入数据库…")

    with Session(engine) as session:
        existing_urls = {p.url for p in session.exec(select(ProxyNode)).all()}
        manually_disabled_urls = {p.url for p in session.exec(select(ProxyNode).where(ProxyNode.manually_disabled == True)).all()}

        for registry_type, items in zip(config.auto_fetch.registry_types, results):
            for item in items:
                if config.auto_fetch.filters.get("selectable") and not item.get("selectable", False):
                    continue
                if config.auto_fetch.filters.get("access") and item.get("access", "public") != config.auto_fetch.filters["access"]:
                    continue

                node_url = item.get("url", "").rstrip("/")
                if not node_url:
                    continue
                if node_url in existing_urls or (node_url + "/") in existing_urls:
                    continue

                is_manually_disabled = node_url in manually_disabled_urls
                mapped_type = REGISTRY_TYPE_MAP.get(registry_type, registry_type)
                route_prefix = ROUTE_PREFIX_MAP.get(registry_type)

                node = ProxyNode(
                    name=item.get("name", "Unknown Mirror"),
                    url=node_url,
                    registry_type=mapped_type,
                    route_prefix=route_prefix,
                    enabled=not is_manually_disabled,
                    manually_disabled=is_manually_disabled,
                )
                session.add(node)
                existing_urls.add(node_url)
                added_count += 1

        session.commit()

    logger.info(f"自动拉取完成，新增 {added_count} 个节点")
    _progress_finish("fetch", f"完成，新增 {added_count} 个节点")
    return added_count


# ============================================================
#  在线检测（仅 /v2/）
# ============================================================


async def check_node_alive(node: ProxyNode) -> tuple[bool, float, Optional[str]]:
    """
    在线检测：只探测 /v2/。
    200/401/302 视为存活，其余视为失败。
    """
    base = node.url.rstrip("/")
    auth = (node.username, node.password) if node.username and node.password else None

    start = time.time()
    try:
        async with httpx.AsyncClient(
            timeout=config.health_check.timeout_seconds,
            follow_redirects=True,
            auth=auth,
        ) as client:
            resp = await client.get(base + "/v2/")
            latency = (time.time() - start) * 1000
            if resp.status_code in (200, 401, 302):
                return True, latency, None
            return False, 9999.0, f"v2 status {resp.status_code}"
    except httpx.ConnectTimeout:
        return False, 9999.0, "连接超时"
    except httpx.ReadTimeout:
        return False, 9999.0, "读取超时"
    except httpx.ConnectError:
        return False, 9999.0, "连接失败"
    except Exception as e:
        return False, 9999.0, str(e)


async def _check_one_alive(nid: int):
    with Session(engine) as session:
        node = session.get(ProxyNode, nid)
        if not node:
            return
        node_name = node.name
        node_obj = node

    alive, latency, error = await check_node_alive(node_obj)

    with Session(engine) as session:
        db_node = session.get(ProxyNode, nid)
        if not db_node:
            return
        if not db_node.manually_disabled:
            db_node.latency = latency if alive else 9999.0
            db_node.failure_reason = error
            db_node.last_check = get_shanghai_time()
            db_node.enabled = alive
            session.add(db_node)
        log = HealthCheckLog(
            node_id=nid,
            node_name=node_name,
            success=alive,
            latency=latency,
            error_message=error,
        )
        session.add(log)
        session.commit()


async def run_health_check(ids: list[int] = None):
    """在线检测：只判断节点是否可达，不做速度测试。"""
    logger.info("开始在线检测...")
    _progress_start("health_check", "在线检测", message="准备中…")

    with Session(engine) as session:
        query = select(ProxyNode).where(ProxyNode.manually_disabled == False)
        if ids:
            query = query.where(ProxyNode.id.in_(ids))
        nodes = session.exec(query).all()
        node_ids = [n.id for n in nodes]

    total = len(node_ids)
    _progress_update("health_check", total=total, message=f"共 {total} 个节点")

    if total == 0:
        _progress_finish("health_check", "无节点可检测")
        return

    batch_size = config.health_check.concurrent_batch
    done_count = 0
    lock = asyncio.Lock()

    async def _wrap(nid: int):
        nonlocal done_count
        try:
            await _check_one_alive(nid)
        finally:
            async with lock:
                done_count += 1
                _progress_update("health_check", done=done_count)

    for i in range(0, total, batch_size):
        batch = node_ids[i : i + batch_size]
        await asyncio.gather(*[_wrap(nid) for nid in batch])

    logger.info(f"在线检测完成，共检测 {total} 个节点")
    _progress_finish("health_check", f"完成，共 {total} 个节点")


# ============================================================
#  速度测试（固定时长下载 layer，支持 401 认证）
# ============================================================


def _parse_www_authenticate(header: str) -> dict:
    """解析 WWW-Authenticate 头，提取 realm/service/scope"""
    info = {}
    if not header:
        return info
    for key in ("realm", "service", "scope"):
        m = re.search(f'{key}="([^"]+)"', header)
        if m:
            info[key] = m.group(1)
    return info


async def _get_speed_test_token(
    client: httpx.AsyncClient,
    resp: httpx.Response,
    username: str = None,
    password: str = None,
) -> Optional[str]:
    """
    从 401 响应中解析 WWW-Authenticate 并获取 token。
    带内存缓存，减少认证请求。
    """
    header = resp.headers.get("www-authenticate", "")
    info = _parse_www_authenticate(header)
    if not info.get("realm"):
        return None

    cache_key = f"{info['realm']}|{info.get('service', '')}|" f"{info.get('scope', '')}|{username or ''}"

    if cache_key in _speed_token_cache:
        token, expire = _speed_token_cache[cache_key]
        if time.time() < expire:
            return token
        _speed_token_cache.pop(cache_key, None)

    params = {}
    if info.get("service"):
        params["service"] = info["service"]
    if info.get("scope"):
        params["scope"] = info["scope"]

    kwargs = {}
    if username and password:
        kwargs["auth"] = (username, password)

    try:
        r = await client.get(info["realm"], params=params, **kwargs)
        if r.status_code == 200:
            data = r.json()
            token = data.get("token") or data.get("access_token")
            if token:
                _speed_token_cache[cache_key] = (token, time.time() + _SPEED_TOKEN_TTL)
                if len(_speed_token_cache) > _MAX_SPEED_TOKEN_CACHE:
                    now = time.time()
                    for k, (_, e) in list(_speed_token_cache.items()):
                        if e < now:
                            _speed_token_cache.pop(k, None)
                return token
    except Exception as e:
        logger.debug(f"获取速度测试 token 失败: {e}")
    return None


async def _fetch_with_auth(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    node: ProxyNode,
) -> Optional[httpx.Response]:
    """
    GET 请求；遇到 401 时自动获取 token 并重试。
    返回 Response 或 None（网络错误）。
    """
    try:
        r = await client.get(url, headers=headers)
    except Exception as e:
        logger.debug(f"请求失败 {url[:100]}: {e}")
        return None

    if r.status_code != 401:
        return r

    token = await _get_speed_test_token(client, r, node.username, node.password)
    if not token:
        return r

    new_headers = dict(headers)
    new_headers["Authorization"] = f"Bearer {token}"
    try:
        return await client.get(url, headers=new_headers)
    except Exception as e:
        logger.debug(f"带 token 重试失败 {url[:100]}: {e}")
        return None


def _pick_layer_for_speed_test(layers: list[dict]) -> Optional[dict]:
    """
    选择适合速度测试的 layer：
      - 优先选大小在 20MB ~ 100MB 之间的
      - 若没有，则选最接近 50MB 的
    """
    if not layers:
        return None

    target_min = 20 * 1024 * 1024
    target_max = 100 * 1024 * 1024
    target = 50 * 1024 * 1024

    in_range = [l for l in layers if target_min <= l.get("size", 0) <= target_max]
    if in_range:
        return max(in_range, key=lambda l: l.get("size", 0))

    return min(layers, key=lambda l: abs(l.get("size", 0) - target))


async def _download_blob_with_auth(
    client: httpx.AsyncClient,
    blob_url: str,
    base_headers: dict,
    node: ProxyNode,
    duration: float,
) -> tuple[int, float]:
    """下载 blob，支持 401 认证重试。返回 (下载字节数, 耗时秒数)。"""
    start = time.time()
    total_bytes = 0

    try:
        async with client.stream("GET", blob_url, headers=base_headers) as r:
            if r.status_code == 401:
                token = await _get_speed_test_token(client, r, node.username, node.password)
                if not token:
                    return 0, 0.0
                auth_headers = dict(base_headers)
                auth_headers["Authorization"] = f"Bearer {token}"
                async with client.stream("GET", blob_url, headers=auth_headers) as r2:
                    if r2.status_code != 200:
                        return 0, 0.0
                    async for chunk in r2.aiter_bytes():
                        total_bytes += len(chunk)
                        if time.time() - start >= duration:
                            break
            elif r.status_code != 200:
                return 0, 0.0
            else:
                async for chunk in r.aiter_bytes():
                    total_bytes += len(chunk)
                    if time.time() - start >= duration:
                        break
    except Exception as e:
        logger.debug(f"下载 blob 失败 {node.name}: {e}")
        return 0, 0.0

    elapsed = time.time() - start
    return total_bytes, elapsed


async def test_node_speed(node: ProxyNode) -> float:
    """
    固定时长内下载 layer，计算 bytes/sec。
    """
    if not config.speed_test.enabled:
        return 0.0

    registry_type = (node.registry_type or "dockerhub").lower()
    test_image = config.speed_test.test_images_by_type.get(registry_type)
    if not test_image:
        return 0.0

    tag = config.speed_test.tag
    base = node.url.rstrip("/")
    auth = (node.username, node.password) if node.username and node.password else None

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            auth=auth,
        ) as client:
            headers = {
                "Accept": (
                    "application/vnd.docker.distribution.manifest.v2+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.oci.image.index.v1+json"
                )
            }

            manifest_url = f"{base}/v2/{test_image}/manifests/{tag}"
            resp = await _fetch_with_auth(client, manifest_url, headers, node)
            if resp is None or resp.status_code != 200:
                logger.debug(f"速度测试[{node.name}] manifest 请求失败: " f"{resp.status_code if resp else 'None'}")
                return 0.0

            try:
                manifest = resp.json()
            except Exception:
                return 0.0

            if manifest.get("mediaType") in (
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.index.v1+json",
            ):
                manifests = manifest.get("manifests") or []
                if not manifests:
                    return 0.0
                digest = manifests[0]["digest"]
                sub_url = f"{base}/v2/{test_image}/manifests/{digest}"
                sub_resp = await _fetch_with_auth(client, sub_url, headers, node)
                if sub_resp is None or sub_resp.status_code != 200:
                    return 0.0
                try:
                    manifest = sub_resp.json()
                except Exception:
                    return 0.0

            layers = manifest.get("layers") or []
            if not layers:
                return 0.0

            layer = _pick_layer_for_speed_test(layers)
            if not layer:
                return 0.0
            digest = layer["digest"]
            layer_size = layer.get("size", 0)

            blob_url = f"{base}/v2/{test_image}/blobs/{digest}"
            duration = config.speed_test.duration_seconds
            total_bytes, elapsed = await _download_blob_with_auth(
                client,
                blob_url,
                headers,
                node,
                duration,
            )

            if elapsed <= 0 or total_bytes <= 0:
                return 0.0

            speed = total_bytes / elapsed
            logger.debug(
                f"速度测试[{node.name}] layer={layer_size/1024/1024:.1f}MB "
                f"下载={total_bytes/1024/1024:.1f}MB "
                f"耗时={elapsed:.2f}s 速度={speed/1024/1024:.2f}MB/s"
            )
            return speed

    except Exception as e:
        logger.warning(f"速度测试失败 {node.name}: {e}")
        return 0.0


async def _test_one_speed(node: ProxyNode):
    speed = await test_node_speed(node)
    with Session(engine) as session:
        db_node = session.get(ProxyNode, node.id)
        if db_node:
            db_node.speed = speed
            db_node.updated_at = get_shanghai_time()
            session.add(db_node)
            session.commit()


async def run_speed_test(ids: list[int] = None):
    """对当前所有存活节点进行速度测试。"""
    if not config.speed_test.enabled:
        logger.info("速度测试已禁用，跳过")
        return

    logger.info("开始速度测试...")
    _progress_start("speed_test", "速度测试", message="准备中…")

    with Session(engine) as session:
        query = select(ProxyNode).where(ProxyNode.enabled == True).where(ProxyNode.manually_disabled == False)
        if ids:
            query = query.where(ProxyNode.id.in_(ids))
        nodes = session.exec(query).all()
        node_list = list(nodes)

    total = len(node_list)
    _progress_update("speed_test", total=total, message=f"共 {total} 个节点")

    if not node_list:
        logger.info("没有存活节点，跳过速度测试")
        _progress_finish("speed_test", "无存活节点")
        return

    sem = asyncio.Semaphore(config.speed_test.concurrent_batch)
    done_count = 0
    lock = asyncio.Lock()

    async def _wrapped(n: ProxyNode):
        nonlocal done_count
        async with sem:
            try:
                await _test_one_speed(n)
            finally:
                async with lock:
                    done_count += 1
                    _progress_update("speed_test", done=done_count)

    await asyncio.gather(*[_wrapped(n) for n in node_list])
    logger.info(f"速度测试完成，共测试 {total} 个节点")
    _progress_finish("speed_test", f"完成，共 {total} 个节点")


# ============================================================
#  候选节点选择（按速度降序，全部返回）
# ============================================================


def get_candidate_proxies(path: str = "") -> list[tuple[ProxyNode, str]]:
    """
    返回所有可用候选节点，按 speed 降序排列。
    拉取失败时依次尝试，全部失败则停止。
    """
    path = path.lstrip("/")

    with Session(engine) as session:
        proxies = session.exec(
            select(ProxyNode)
            .where(ProxyNode.enabled == True)
            .where(ProxyNode.manually_disabled == False)
            .where(ProxyNode.latency < config.health_check.disable_threshold)
        ).all()
        proxies = list(proxies)

    proxies.sort(key=lambda p: (-(p.speed or 0), p.latency or 9999))

    has_prefix = _path_has_registry_prefix(path)

    candidates: list[tuple[ProxyNode, str]] = []

    for p in proxies:
        if not _is_node_available(p.id):
            continue
        if is_blob_failed(p.id, path):
            continue

        prefixes = _get_node_prefixes(p)
        if prefixes:
            matched, consumed = _match_any_prefix(path, prefixes)
            if consumed > 0:
                adjusted = path[consumed:]
                candidates.append((p, adjusted))
                continue

        if not has_prefix and not p.route_prefix:
            candidates.append((p, path))

    if not candidates:
        candidates.append(
            (
                ProxyNode(
                    name="DockerMirrorFlow Fallback",
                    url="https://registry-1.docker.io",
                ),
                path,
            )
        )

    pinned = get_pinned_node_for_path(path)
    if pinned and pinned.id is not None:
        for i, (n, ap) in enumerate(candidates):
            if n.id == pinned.id:
                if i > 0:
                    candidates.insert(0, candidates.pop(i))
                break

    return candidates


def get_best_proxy(path: str = "") -> tuple[ProxyNode, str]:
    candidates = get_candidate_proxies(path)
    return candidates[0]


# ============================================================
#  缓存清理
# ============================================================


def cleanup_caches():
    now = time.time()

    expired_nodes = [k for k, v in _failed_until.items() if v < now]
    for k in expired_nodes:
        _failed_until.pop(k, None)

    _cleanup_blob_cache()
    _cleanup_affinity()

    expired_tokens = [k for k, (_, e) in _speed_token_cache.items() if e < now]
    for k in expired_tokens:
        _speed_token_cache.pop(k, None)

    global _probe_affinity
    if _probe_affinity and _probe_affinity[1] < now:
        _probe_affinity = None

    logger.debug(
        f"缓存清理：熔断 {len(_failed_until)}，"
        f"blob {len(_blob_failed_until)}，"
        f"粘性 {len(_image_affinity)}，"
        f"token {len(_speed_token_cache)}"
    )


# ============================================================
#  CRUD
# ============================================================


def get_all_proxies() -> list[ProxyNode]:
    with Session(engine) as session:
        return session.exec(select(ProxyNode).order_by(ProxyNode.latency)).all()


def add_proxy(
    name: str,
    url: str,
    registry_type: str = "dockerhub",
    route_prefix: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> ProxyNode:
    with Session(engine) as session:
        node = ProxyNode(
            name=name,
            url=url.rstrip("/"),
            registry_type=registry_type,
            route_prefix=route_prefix,
            username=username,
            password=password,
            is_custom=True,
        )
        session.add(node)
        session.commit()
        session.refresh(node)
        return node


def update_proxy(
    proxy_id: int,
    name: str,
    url: str,
    registry_type: str = "dockerhub",
    route_prefix: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Optional[ProxyNode]:
    with Session(engine) as session:
        node = session.get(ProxyNode, proxy_id)
        if not node:
            return None
        node.name = name
        node.url = url.rstrip("/")
        node.registry_type = registry_type
        node.route_prefix = route_prefix
        node.username = username
        node.password = password
        node.updated_at = get_shanghai_time()
        session.add(node)
        session.commit()
        session.refresh(node)
        return node


def delete_proxy(proxy_id: int) -> bool:
    with Session(engine) as session:
        node = session.get(ProxyNode, proxy_id)
        if not node:
            return False
        session.delete(node)
        session.commit()
        return True


def set_manual_disable(proxy_id: int, disabled: bool, reason: str = "") -> Optional[ProxyNode]:
    with Session(engine) as session:
        node = session.get(ProxyNode, proxy_id)
        if not node:
            return None
        node.manually_disabled = disabled
        node.manual_disable_reason = reason if disabled else None
        node.manual_disable_at = get_shanghai_time() if disabled else None
        node.enabled = not disabled
        session.add(node)
        session.commit()
        session.refresh(node)
        return node
