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

# 自动拉取时为节点设置的 route_prefix（保留字段，作为显式前缀）
ROUTE_PREFIX_MAP = {
    "ghcr": "ghcr",
    "quay": "quay",
    "mcr": "mcr",
    "gcr": "gcr",
    "elastic": "elastic",
    "nvcr": "nvcr",
}

# ============================================================
#  默认路由别名（按 registry_type 归类）
#  即使节点没有 route_prefix，也能命中这些域名
# ============================================================
DEFAULT_ROUTE_ALIASES: dict[str, list[str]] = {
    "dockerhub": ["docker.io", "registry-1.docker.io", "index.docker.io"],
    "ghcr":      ["ghcr.io", "ghcr"],
    "gcr":       ["gcr.io", "k8s.gcr.io", "registry.k8s.io", "gcr"],
    "quay":      ["quay.io", "quay"],
    "mcr":       ["mcr.microsoft.com", "mcr"],
    "nvcr":      ["nvcr.io", "nvcr"],
    "elastic":   ["docker.elastic.co", "elastic"],
}

# 短期熔断表：{node_id: 过期时间戳}
_failed_until: dict[int, float] = {}


# ============================================================
#  熔断机制
# ============================================================

def mark_node_failed(node_id: int, reason: str = ""):
    if node_id is not None:
        cooldown = config.proxy.fail_cooldown
        _failed_until[node_id] = time.time() + cooldown
        logger.warning(f"节点 {node_id} 失败，熔断 {cooldown}s: {reason}")


def mark_node_success(node_id: int):
    _failed_until.pop(node_id, None)


def _is_node_available(node_id: int) -> bool:
    expired = _failed_until.get(node_id, 0)
    return time.time() >= expired


# ============================================================
#  路由别名辅助
# ============================================================

def _get_route_aliases() -> dict[str, list[str]]:
    """优先用 config 中的 route_aliases，否则用内置默认。"""
    if config.route_aliases:
        return config.route_aliases
    return DEFAULT_ROUTE_ALIASES


def _get_node_prefixes(node: ProxyNode) -> list[str]:
    """
    获取节点的所有有效路由前缀 = 显式 route_prefix + registry_type 的别名。
    去重保序。
    """
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
    """
    匹配多个前缀，返回最长匹配的 (prefix, consumed)。
    consumed = 0 表示不匹配。

    支持形式：
      - "ghcr/owner/img"     prefix="ghcr"     消耗 5
      - "ghcr.io/owner/img"  prefix="ghcr"     消耗 8
      - "ghcr.io/owner/img"  prefix="ghcr.io"  消耗 8
    """
    best_prefix: Optional[str] = None
    best_consumed = 0
    path_lower = path.lower()

    for prefix in prefixes:
        p = prefix.strip("/").lower()
        if not p:
            continue

        # 形式 1: prefix/
        if path_lower.startswith(p + "/"):
            consumed = len(p) + 1
            if consumed > best_consumed:
                best_consumed = consumed
                best_prefix = prefix

        # 形式 2: prefix.domain/
        elif path_lower.startswith(p + "."):
            slash_idx = path.find("/")
            if slash_idx != -1 and slash_idx > len(p):
                consumed = slash_idx + 1
                if consumed > best_consumed:
                    best_consumed = consumed
                    best_prefix = prefix

    return best_prefix, best_consumed


# 向后兼容（旧接口）
def _match_route_prefix(path: str, prefix: str) -> int:
    _, consumed = _match_any_prefix(path, [prefix])
    return consumed


# ============================================================
#  初始化
# ============================================================

def init_proxies():
    """初始化：加载 YAML 中的自定义节点，恢复手动禁用列表。"""
    with Session(engine) as session:
        for cn in config.custom_nodes:
            existing = session.exec(
                select(ProxyNode).where(ProxyNode.url == cn.url)
            ).first()
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
            node = session.exec(
                select(ProxyNode).where(ProxyNode.url == md.url)
            ).first()
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
        results = await asyncio.gather(
            *[_fetch_for_registry(client, rt) for rt in config.auto_fetch.registry_types]
        )

    with Session(engine) as session:
        existing_urls = {p.url for p in session.exec(select(ProxyNode)).all()}
        manually_disabled_urls = {
            p.url
            for p in session.exec(
                select(ProxyNode).where(ProxyNode.manually_disabled == True)
            ).all()
        }

        for registry_type, items in zip(config.auto_fetch.registry_types, results):
            for item in items:
                if config.auto_fetch.filters.get("selectable") and not item.get("selectable", False):
                    continue
                if (
                    config.auto_fetch.filters.get("access")
                    and item.get("access", "public") != config.auto_fetch.filters["access"]
                ):
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
#  活体检测与测速
# ============================================================

async def check_node_health(node: ProxyNode) -> tuple[float, Optional[str]]:
    url = node.url.rstrip("/") + "/v2/"
    start = datetime.now()
    auth = None
    if node.username and node.password:
        auth = (node.username, node.password)

    try:
        async with httpx.AsyncClient(
            timeout=config.health_check.timeout_seconds,
            follow_redirects=True,
            auth=auth,
        ) as client:
            resp = await client.get(url)
            if resp.status_code in (200, 401):
                latency = (datetime.now() - start).total_seconds() * 1000
                return latency, None
            return 9999.0, f"HTTP {resp.status_code}"
    except httpx.ConnectTimeout:
        return 9999.0, "连接超时"
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
        nodes = session.exec(
            select(ProxyNode).where(ProxyNode.manually_disabled == False)
        ).all()
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
#  候选节点选择（核心路由）
# ============================================================

def get_candidate_proxies(path: str = "", limit: int = None) -> list[tuple[ProxyNode, str]]:
    """
    获取候选节点列表（按优先级排序），返回 [(node, adjusted_path), ...]。
    优先级：
      1. 前缀匹配（consumed 降序 → latency 升序）
      2. 通用节点（无 route_prefix）
      3. 官方 Docker Hub fallback
    """
    if limit is None:
        limit = config.proxy.candidate_count

    path = path.lstrip("/")

    with Session(engine) as session:
        proxies = session.exec(
            select(ProxyNode)
            .where(ProxyNode.enabled == True)
            .where(ProxyNode.manually_disabled == False)
            .where(ProxyNode.latency < config.health_check.disable_threshold)
            .order_by(ProxyNode.latency)
        ).all()

        candidates: list[tuple[ProxyNode, str]] = []
        prefix_matched: list[tuple[int, float, ProxyNode, str]] = []
        generic_nodes: list[ProxyNode] = []

        for p in proxies:
            prefixes = _get_node_prefixes(p)

            if prefixes:
                matched, consumed = _match_any_prefix(path, prefixes)
                if consumed > 0:
                    adjusted = path[consumed:]
                    logger.debug(
                        f"节点 {p.name} 匹配前缀 {matched!r}，消耗 {consumed}，"
                        f"path: {path!r} -> {adjusted!r}"
                    )
                    prefix_matched.append((consumed, p.latency, p, adjusted))
                    continue

            # 无前缀的节点，作为通用候选
            if not p.route_prefix:
                generic_nodes.append(p)

        # 前缀匹配优先：consumed 降序，latency 升序
        prefix_matched.sort(key=lambda x: (-x[0], x[1]))

        for consumed, _, p, adjusted in prefix_matched:
            if not _is_node_available(p.id):
                continue
            candidates.append((p, adjusted))
            if len(candidates) >= limit:
                return candidates

        # 通用节点
        for p in generic_nodes:
            if not _is_node_available(p.id):
                continue
            candidates.append((p, path))
            if len(candidates) >= limit:
                return candidates

        # 回退
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

        return candidates


async def get_candidate_proxies_realtime(path: str = "", limit: int = None) -> list[tuple[ProxyNode, str]]:
    """实时探测候选节点的延迟，并按实时延迟排序。"""
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