import asyncio
import logging
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

_MAX_BLOB_CACHE = 5000
_MAX_AFFINITY_CACHE = 2000


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
#  优先级排序
# ============================================================


def _priority_score(node: ProxyNode) -> tuple:
    if not config.proxy.prefer_recent_success:
        return (0, node.latency)

    now = time.time()
    last_ok = _last_success_at.get(node.id, 0)
    recently_succeeded = 1 if (now - last_ok) < config.proxy.recent_success_window else 0
    return (-recently_succeeded, node.latency)


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
    added_count = 0

    async with httpx.AsyncClient(timeout=15.0) as client:
        results = await asyncio.gather(*[_fetch_for_registry(client, rt) for rt in config.auto_fetch.registry_types])

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
    return added_count


# ============================================================
#  健康检查（按 registry 类型选择测试镜像）
# ============================================================


async def check_node_health(node: ProxyNode) -> tuple[float, Optional[str]]:
    """
    健康检查：
      1. 探测 /v2/ 确认节点可达（200/401/302 视为通过）
      2. 按 registry 类型选择对应测试镜像，请求 manifests
         - 200/401 视为通过
         - 404 / 其他 → 节点故障
      3. 某 registry 类型在 test_images_by_type 中配置为空时，
         仅用 /v2/ 判断存活
    """
    base = node.url.rstrip("/")
    auth = None
    if node.username and node.password:
        auth = (node.username, node.password)

    registry_type = (node.registry_type or "dockerhub").lower()

    start = datetime.now()
    try:
        async with httpx.AsyncClient(
            timeout=config.health_check.timeout_seconds,
            follow_redirects=True,
            auth=auth,
        ) as client:
            # ===== 1. 可达性：/v2/ =====
            resp = await client.get(base + "/v2/")
            if resp.status_code not in (200, 401, 302):
                return 9999.0, f"v2 status {resp.status_code}"

            # ===== 2. 按类型选测试镜像 =====
            test_path = config.health_check.test_images_by_type.get(registry_type, "")
            if not test_path:
                # 该类型未配置测试镜像，仅用 /v2/ 判断
                latency = (datetime.now() - start).total_seconds() * 1000
                return latency, None

            test_path = test_path.strip("/")
            test_url = f"{base}/v2/{test_path}"
            headers = {
                "Accept": (
                    "application/vnd.docker.distribution.manifest.v2+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.oci.image.index.v1+json"
                )
            }

            resp2 = await client.get(test_url, headers=headers)
            if resp2.status_code not in (200, 401):
                return 9999.0, f"manifests status {resp2.status_code}"

            latency = (datetime.now() - start).total_seconds() * 1000
            return latency, None

    except httpx.ConnectTimeout:
        return 9999.0, "连接超时"
    except httpx.ReadTimeout:
        return 9999.0, "读取超时"
    except httpx.ConnectError:
        return 9999.0, "连接失败"
    except Exception as e:
        return 9999.0, str(e)


async def check_and_update_node(node: ProxyNode) -> ProxyNode:
    latency, error = await check_node_health(node)

    with Session(engine) as session:
        db_node = session.get(ProxyNode, node.id)
        if not db_node:
            return node

        if not db_node.manually_disabled:
            db_node.latency = latency
            db_node.failure_reason = error
            db_node.last_check = get_shanghai_time()
            db_node.enabled = latency < config.health_check.disable_threshold

        log = HealthCheckLog(
            node_id=db_node.id,
            node_name=db_node.name,
            success=(latency < config.health_check.disable_threshold),
            latency=latency,
            error_message=error,
        )
        session.add(log)
        session.add(db_node)
        session.commit()
        session.refresh(db_node)
        return db_node


async def run_health_check():
    logger.info("开始健康检查...")

    with Session(engine) as session:
        nodes = session.exec(select(ProxyNode).where(ProxyNode.manually_disabled == False)).all()
        node_ids = [n.id for n in nodes]

    batch_size = config.health_check.concurrent_batch

    async def _check_one(nid: int):
        with Session(engine) as session:
            node = session.get(ProxyNode, nid)
            if node:
                await check_and_update_node(node)

    for i in range(0, len(node_ids), batch_size):
        batch = node_ids[i : i + batch_size]
        await asyncio.gather(*[_check_one(nid) for nid in batch])

    logger.info(f"健康检查完成，共检测 {len(node_ids)} 个节点")


# ============================================================
#  候选节点选择
# ============================================================


def get_candidate_proxies(path: str = "", limit: int = None) -> list[tuple[ProxyNode, str]]:
    if limit is None:
        limit = config.proxy.candidate_count

    path = path.lstrip("/")

    with Session(engine) as session:
        proxies = session.exec(
            select(ProxyNode)
            .where(ProxyNode.enabled == True)
            .where(ProxyNode.manually_disabled == False)
            .where(ProxyNode.latency < config.health_check.disable_threshold)
        ).all()

        candidates: list[tuple[ProxyNode, str]] = []
        prefix_matched: list[tuple[int, tuple, ProxyNode, str]] = []
        generic_nodes: list[ProxyNode] = []

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
                    prefix_matched.append((consumed, _priority_score(p), p, adjusted))
                    continue

            if not p.route_prefix:
                generic_nodes.append(p)

        prefix_matched.sort(key=lambda x: (-x[0], x[1]))

        for consumed, _, p, adjusted in prefix_matched:
            candidates.append((p, adjusted))
            if len(candidates) >= limit * 2:
                break

        if len(candidates) < limit:
            generic_nodes.sort(key=_priority_score)
            for p in generic_nodes:
                candidates.append((p, path))
                if len(candidates) >= limit * 2:
                    break

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

    return candidates[:limit]


async def get_candidate_proxies_realtime(path: str = "", limit: int = None) -> list[tuple[ProxyNode, str]]:
    if limit is None:
        limit = config.proxy.candidate_count

    candidates = get_candidate_proxies(path, limit=limit * 2)
    if not candidates:
        return candidates

    async def probe(node: ProxyNode) -> tuple[ProxyNode, float]:
        if not node.id:
            return node, 0.0
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=config.proxy.probe_timeout) as c:
                await c.head(node.url.rstrip("/") + "/v2/", follow_redirects=True)
            return node, (time.time() - start) * 1000
        except Exception:
            return node, 99999.0

    results = await asyncio.gather(*[probe(n) for n, _ in candidates])
    results.sort(key=lambda x: x[1])

    node_to_path = {n.id: p for n, p in candidates}
    return [(n, node_to_path.get(n.id, path)) for n, _ in results[:limit]]


def get_best_proxy(path: str = "") -> tuple[ProxyNode, str]:
    candidates = get_candidate_proxies(path, limit=1)
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

    global _probe_affinity
    if _probe_affinity and _probe_affinity[1] < now:
        _probe_affinity = None

    logger.debug(f"缓存清理：熔断 {len(_failed_until)}，blob {len(_blob_failed_until)}，" f"粘性 {len(_image_affinity)}")


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
