import asyncio
import base64
import logging
import re
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from app.config import config
from app.models import ProxyNode
from app.services import proxy_manager, traffic_logger

router = APIRouter()
logger = logging.getLogger("dockermirrorflow.proxy")

DOCKER_AUTH_URL = "https://auth.docker.io/token"


async def parse_www_authenticate(header: str) -> dict:
    info = {}
    for key in ("realm", "service", "scope"):
        match = re.search(f'{key}="([^"]+)"', header)
        if match:
            info[key] = match.group(1)
    return info


async def get_upstream_token(
    realm: str,
    service: str = None,
    scope: str = None,
    username: str = None,
    password: str = None,
) -> str | None:
    params = {}
    if service:
        params["service"] = service
    if scope:
        params["scope"] = scope

    try:
        auth_kwargs = {}
        if username and password:
            auth_kwargs["auth"] = (username, password)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(realm, params=params, **auth_kwargs)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("token") or data.get("access_token")
            logger.error(f"获取 token 失败: {resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"获取 token 异常: {e}")
        return None


async def _send_with_auth(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers_list: list,
    content: bytes,
    node: ProxyNode,
) -> httpx.Response:
    """发送请求，遇到 401 自动携带凭据重试。"""
    req = client.build_request(method, url, headers=headers_list, content=content)
    r = await client.send(req, stream=True)

    if r.status_code != 401:
        return r

    auth_header_val = r.headers.get("www-authenticate")
    if not auth_header_val:
        return r

    if "Bearer" in auth_header_val:
        await r.aclose()
        auth_info = await parse_www_authenticate(auth_header_val)
        if auth_info.get("realm"):
            token = await get_upstream_token(
                auth_info["realm"],
                auth_info.get("service"),
                auth_info.get("scope"),
                node.username,
                node.password,
            )
            if token:
                retry_headers = list(headers_list)
                retry_headers.append(("Authorization", f"Bearer {token}"))
                req = client.build_request(method, url, headers=retry_headers, content=content)
                return await client.send(req, stream=True)

    elif "Basic" in auth_header_val and node.username and node.password:
        await r.aclose()
        b64_auth = base64.b64encode(f"{node.username}:{node.password}".encode()).decode()
        retry_headers = list(headers_list)
        retry_headers.append(("Authorization", f"Basic {b64_auth}"))
        req = client.build_request(method, url, headers=retry_headers, content=content)
        return await client.send(req, stream=True)

    return r


async def proxy_v2(path: str, request: Request) -> Response:
    """核心代理逻辑：多候选节点 fallback + 熔断。"""
    client_ip = request.client.host if request.client else "unknown"

    # ===== 访问控制 =====
    whitelist = config.access.ip_whitelist
    if whitelist and client_ip not in whitelist:
        logger.warning(f"IP {client_ip} 被白名单拒绝")
        return Response(
            content='{"errors":[{"code":"UNAUTHORIZED","message":"IP not in whitelist"}]}',
            status_code=403,
            media_type="application/json",
        )

    if path and ("/manifests/" in path or "/blobs/" in path):
        image_name = (
            path.split("/manifests/")[0]
            if "/manifests/" in path
            else path.split("/blobs/")[0]
        )

        if config.access.image_blacklist_regex and re.search(
            config.access.image_blacklist_regex, image_name
        ):
            logger.warning(f"镜像 {image_name} 被黑名单拒绝")
            return Response(
                content='{"errors":[{"code":"UNAUTHORIZED","message":"Image blacklisted"}]}',
                status_code=403,
                media_type="application/json",
            )

        if config.access.image_whitelist_regex and not re.search(
            config.access.image_whitelist_regex, image_name
        ):
            logger.warning(f"镜像 {image_name} 被白名单拒绝")
            return Response(
                content='{"errors":[{"code":"UNAUTHORIZED","message":"Image not in whitelist"}]}',
                status_code=403,
                media_type="application/json",
            )

    # ===== 获取候选节点 =====
    if config.proxy.realtime_probe:
        candidates = await proxy_manager.get_candidate_proxies_realtime(path)
    else:
        candidates = proxy_manager.get_candidate_proxies(path)

    # ===== 请求体 =====
    content = await request.body()

    # ===== 构造请求头 =====
    headers_list = []
    for key, value in request.headers.items():
        if key.lower() not in ("host", "content-length"):
            headers_list.append((key, value))

    # ===== 依次尝试候选节点 =====
    client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=config.proxy.timeout,
    )

    r: httpx.Response | None = None
    proxy_node: ProxyNode | None = None
    last_error = None

    for node, adjusted_path in candidates:
        upstream_url = f"{node.url.rstrip('/')}/v2/{adjusted_path}"
        if request.url.query:
            upstream_url += f"?{request.url.query}"

        try:
            logger.info(f"尝试节点: {node.name} ({node.url})")
            r = await _send_with_auth(
                client,
                request.method,
                upstream_url,
                headers_list,
                content,
                node,
            )

            if node.id is not None:
                proxy_manager.mark_node_success(node.id)

            proxy_node = node
            traffic_logger.log_traffic(bytes_uploaded=len(content), node_id=node.id)
            break

        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"节点 {node.name} 连接失败: {last_error}")
            if node.id is not None:
                proxy_manager.mark_node_failed(node.id, last_error)
            continue

        except Exception as e:
            last_error = str(e)
            logger.error(f"节点 {node.name} 异常: {last_error}")
            if node.id is not None:
                proxy_manager.mark_node_failed(node.id, last_error)
            continue

    if r is None or proxy_node is None:
        await client.aclose()
        logger.error(f"所有候选节点均失败: {last_error}")
        return Response(
            content=f'{{"errors":[{{"code":"UNKNOWN","message":"All upstream nodes failed: {last_error}"}}]}}',
            status_code=502,
            media_type="application/json",
        )

    # ===== 处理响应头 =====
    resp_headers = dict(r.headers)

    # 重写 WWW-Authenticate
    auth_header = resp_headers.get("www-authenticate")
    if auth_header:
        my_host = f"{request.url.scheme}://{request.url.netloc}"
        realm_match = re.search(r'realm="([^"]+)"', auth_header)
        if realm_match:
            upstream_realm = realm_match.group(1)
            b64_realm = base64.urlsafe_b64encode(upstream_realm.encode()).decode()
            new_realm = f"{my_host}/token?_upstream_realm={quote(b64_realm)}"
            resp_headers["www-authenticate"] = auth_header.replace(upstream_realm, new_realm)

    if request.method != "HEAD":
        resp_headers.pop("content-length", None)
    resp_headers.pop("content-encoding", None)

    # 重定向流量统计
    location = resp_headers.get("location")
    if r.status_code in (301, 302, 303, 307, 308) and location and proxy_node.id:
        async def log_redirect_size(loc: str, n_id: int):
            try:
                async with httpx.AsyncClient() as bg_client:
                    head_r = await bg_client.head(loc, follow_redirects=True, timeout=10.0)
                    size = int(head_r.headers.get("content-length", 0))
                    if size > 0:
                        traffic_logger.log_traffic(bytes_downloaded=size, node_id=n_id)
            except Exception:
                pass

        asyncio.create_task(log_redirect_size(location, proxy_node.id))

    # ===== 流式返回 =====
    async def iter_response():
        try:
            async for chunk in r.aiter_bytes(chunk_size=config.proxy.stream_chunk_size):
                traffic_logger.log_traffic(bytes_downloaded=len(chunk), node_id=proxy_node.id)
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(
        iter_response(),
        status_code=r.status_code,
        headers=resp_headers,
    )


@router.get("/token")
async def proxy_token(request: Request):
    upstream_realm_b64 = request.query_params.get("_upstream_realm")
    url = DOCKER_AUTH_URL

    if upstream_realm_b64:
        try:
            decoded = unquote(upstream_realm_b64)
            missing_padding = len(decoded) % 4
            if missing_padding:
                decoded += "=" * (4 - missing_padding)
            url = base64.urlsafe_b64decode(decoded).decode("utf-8")
            logger.info(f"解析上游 token URL: {url}")
        except Exception as e:
            logger.warning(f"解析 _upstream_realm 失败: {e}")

    params = dict(request.query_params)
    params.pop("_upstream_realm", None)

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, headers=headers)
            traffic_logger.log_traffic(bytes_uploaded=len(str(request.query_params)))

            resp_headers = dict(resp.headers)
            resp_headers.pop("content-length", None)
            resp_headers.pop("content-encoding", None)

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=resp_headers,
            )
    except Exception as e:
        logger.error(f"Token 代理错误: {e}")
        return Response(content=f"Auth Error: {e}", status_code=500)


@router.api_route("/v2/", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_v2_root(request: Request):
    return await proxy_v2(path="", request=request)


@router.api_route("/v2/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_v2_path(path: str, request: Request):
    if request.method in ("GET", "HEAD") and "/manifests/" in path:
        try:
            parts = path.split("/manifests/")
            if len(parts) == 2:
                image = parts[0]
                tag = parts[1]
                client_ip = request.headers.get(
                    "x-forwarded-for",
                    request.client.host if request.client else "unknown",
                )
                node, _ = proxy_manager.get_best_proxy(path)
                traffic_logger.log_pull(
                    image=image,
                    tag=tag,
                    client_ip=client_ip,
                    node_id=node.id,
                    node_name=node.name,
                )
        except Exception as e:
            logger.error(f"记录拉取失败: {e}")

    return await proxy_v2(path=path, request=request)