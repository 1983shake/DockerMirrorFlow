import asyncio
import base64
import logging
import re
import time
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

RETRYABLE_STATUS_CODES = (403, 500, 502, 503, 504)
REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)

_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_TTL = 240


def _get_timeout(path: str) -> float:
    tbp = config.proxy.timeout_by_path
    if not path:
        return tbp.probe
    if "/manifests/" in path:
        return tbp.manifests
    if "/blobs/" in path:
        return tbp.blobs
    return config.proxy.timeout


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
    cache_key = f"{realm}|{service}|{scope}|{username or ''}"
    if cache_key in _token_cache:
        token, expire = _token_cache[cache_key]
        if time.time() < expire:
            return token
        _token_cache.pop(cache_key, None)

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
                token = data.get("token") or data.get("access_token")
                if token:
                    _token_cache[cache_key] = (token, time.time() + _TOKEN_TTL)
                    if len(_token_cache) > 500:
                        now = time.time()
                        for k, (_, e) in list(_token_cache.items()):
                            if e < now:
                                _token_cache.pop(k, None)
                return token
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
        req = client.build_request(method, url, headers=headers_list, content=content)
        return await client.send(req, stream=True)

    elif "Basic" in auth_header_val and node.username and node.password:
        await r.aclose()
        b64_auth = base64.b64encode(f"{node.username}:{node.password}".encode()).decode()
        retry_headers = list(headers_list)
        retry_headers.append(("Authorization", f"Basic {b64_auth}"))
        req = client.build_request(method, url, headers=retry_headers, content=content)
        return await client.send(req, stream=True)

    return r


async def _try_follow_redirects(
    client: httpx.AsyncClient,
    r: httpx.Response,
    request: Request,
    content: bytes,
    headers_list: list,
    proxy_node: ProxyNode,
) -> httpx.Response | None:
    max_redirects = config.proxy.follow_redirects_max
    redirect_count = 0

    while r.status_code in REDIRECT_STATUS_CODES and r.headers.get("location") and redirect_count < max_redirects:
        redirect_count += 1
        location = r.headers["location"]
        logger.info(f"[follow-redirect {redirect_count}/{max_redirects}] {r.status_code} -> {location[:160]}")

        current_status = r.status_code
        try:
            await r.aclose()
        except Exception:
            pass

        if current_status in (301, 302, 303):
            new_method = "GET"
            new_content = None
        else:
            new_method = request.method
            new_content = content

        redirect_headers = [(k, v) for k, v in headers_list if k.lower() not in ("host", "authorization", "content-length")]

        try:
            redirect_req = client.build_request(new_method, location, headers=redirect_headers, content=new_content)
            r = await client.send(redirect_req, stream=True)

            if new_content:
                traffic_logger.log_traffic(
                    bytes_uploaded=len(new_content),
                    node_id=proxy_node.id if proxy_node else None,
                )
        except Exception as e:
            logger.error(f"跟随重定向失败 ({location[:80]}): {type(e).__name__}: {e}")
            return None

    return r


def _extract_image_tag(path: str) -> tuple[str | None, str | None]:
    if not path or "/manifests/" not in path:
        return None, None
    parts = path.split("/manifests/")
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


async def proxy_v2(path: str, request: Request) -> Response:
    """核心代理逻辑：按速度排序依次尝试所有节点，全失败则停止。"""
    client_ip = request.client.host if request.client else "unknown"

    logger.info(f"[request] {request.method} /v2/{path} from {client_ip}")

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
        image_name = path.split("/manifests/")[0] if "/manifests/" in path else path.split("/blobs/")[0]

        if config.access.image_blacklist_regex and re.search(config.access.image_blacklist_regex, image_name):
            logger.warning(f"镜像 {image_name} 被黑名单拒绝")
            return Response(
                content='{"errors":[{"code":"UNAUTHORIZED","message":"Image blacklisted"}]}',
                status_code=403,
                media_type="application/json",
            )

        if config.access.image_whitelist_regex and not re.search(config.access.image_whitelist_regex, image_name):
            logger.warning(f"镜像 {image_name} 被白名单拒绝")
            return Response(
                content='{"errors":[{"code":"UNAUTHORIZED","message":"Image not in whitelist"}]}',
                status_code=403,
                media_type="application/json",
            )

    # ===== 记录信息 =====
    image_name, image_tag = _extract_image_tag(path)

    # ===== 获取候选节点（已按速度降序，全部返回） =====
    candidates = proxy_manager.get_candidate_proxies(path)

    if candidates:
        cand_desc = ", ".join(f"{n.name}(speed={n.speed:.0f}B/s)" if n.id else f"{n.name}(fallback)" for n, _ in candidates[:5])
        total = len(candidates)
        logger.info(f"[route] path={path!r} -> 候选 {total} 个，前 5: [{cand_desc}]")
    else:
        logger.warning(f"[route] path={path!r} -> 无候选节点")

    # ===== 请求体与请求头 =====
    content = await request.body()

    headers_list = []
    for key, value in request.headers.items():
        if key.lower() not in ("host", "content-length"):
            headers_list.append((key, value))

    timeout = _get_timeout(path)

    client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
    )

    r: httpx.Response | None = None
    proxy_node: ProxyNode | None = None
    last_error = None
    attempts_log: list[str] = []

    for idx, (node, adjusted_path) in enumerate(candidates, start=1):
        upstream_url = f"{node.url.rstrip('/')}/v2/{adjusted_path}"
        if request.url.query:
            upstream_url += f"?{request.url.query}"

        logger.info(f"尝试节点 [{idx}/{len(candidates)}]: {node.name} ({node.url}) timeout={timeout}s")

        try:
            r = await _send_with_auth(
                client,
                request.method,
                upstream_url,
                headers_list,
                content,
                node,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"节点 {node.name} 连接失败: {last_error}")
            attempts_log.append(f"{node.name}:{type(e).__name__}")
            if node.id is not None:
                proxy_manager.mark_node_failed(node.id, last_error)
                proxy_manager.mark_blob_failed(node.id, path)
            r = None
            continue
        except Exception as e:
            last_error = str(e)
            logger.error(f"节点 {node.name} 异常: {last_error}")
            attempts_log.append(f"{node.name}:{type(e).__name__}")
            if node.id is not None:
                proxy_manager.mark_node_failed(node.id, last_error)
                proxy_manager.mark_blob_failed(node.id, path)
            r = None
            continue

        if r.status_code in RETRYABLE_STATUS_CODES:
            reason = f"HTTP {r.status_code}"
            logger.warning(f"节点 {node.name} 返回 {reason}，尝试下一个候选")
            attempts_log.append(f"{node.name}:{reason}")
            try:
                await r.aclose()
            except Exception:
                pass
            if node.id is not None:
                proxy_manager.mark_node_failed(node.id, reason)
                if r.status_code == 403:
                    proxy_manager.mark_blob_failed(node.id, path)
            last_error = reason
            r = None
            continue

        if config.proxy.follow_redirects and r.status_code in REDIRECT_STATUS_CODES:
            new_r = await _try_follow_redirects(client, r, request, content, headers_list, node)
            if new_r is None:
                reason = "follow-redirect failed"
                logger.warning(f"节点 {node.name} 跟随重定向失败，熔断 {config.proxy.follow_redirect_fail_cooldown}s")
                attempts_log.append(f"{node.name}:redirect-fail")
                if node.id is not None:
                    proxy_manager.mark_node_failed(
                        node.id,
                        reason,
                        cooldown=config.proxy.follow_redirect_fail_cooldown,
                    )
                    proxy_manager.mark_blob_failed(node.id, path)
                last_error = reason
                r = None
                continue
            r = new_r

        # 成功
        if node.id is not None:
            proxy_manager.mark_node_success(node.id)
            proxy_manager.pin_node_for_path(path, node)

        proxy_node = node
        traffic_logger.log_traffic(bytes_uploaded=len(content), node_id=node.id)
        logger.info(f"节点 {node.name} 响应 {r.status_code}，采用此节点")
        break

    # ===== 全部候选失败 =====
    if r is None or proxy_node is None:
        await client.aclose()
        summary = " | ".join(attempts_log) if attempts_log else (last_error or "unknown")
        logger.error(f"所有候选节点均失败: {summary}")

        if image_name and image_tag:
            traffic_logger.log_pull(
                image=image_name,
                tag=image_tag,
                client_ip=client_ip,
                status="failed",
                error_message=summary[:500],
            )

        return Response(
            content=('{"errors":[{"code":"UNKNOWN","message":"All upstream nodes failed. ' f'Tried: {summary}"}}]}}'),
            status_code=502,
            media_type="application/json",
        )

    # ===== 记录镜像拉取（成功） =====
    if request.method in ("GET", "HEAD") and image_name and image_tag:
        real_ip = request.headers.get("x-forwarded-for", client_ip)
        try:
            traffic_logger.log_pull(
                image=image_name,
                tag=image_tag,
                client_ip=real_ip,
                node_id=proxy_node.id,
                node_name=proxy_node.name,
                status="success",
            )
        except Exception as e:
            logger.error(f"记录拉取失败: {e}")

    # ===== 响应头 =====
    resp_headers = dict(r.headers)

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

    if r.status_code not in REDIRECT_STATUS_CODES:
        resp_headers.pop("location", None)

    async def iter_response():
        try:
            async for chunk in r.aiter_bytes(chunk_size=config.proxy.stream_chunk_size):
                traffic_logger.log_traffic(bytes_downloaded=len(chunk), node_id=proxy_node.id)
                yield chunk
        except asyncio.CancelledError:
            if image_name and image_tag:
                traffic_logger.log_pull(
                    image=image_name,
                    tag=image_tag,
                    client_ip=client_ip,
                    node_id=proxy_node.id,
                    node_name=proxy_node.name,
                    status="cancelled",
                )
            raise
        except Exception as e:
            logger.error(f"流式传输异常: {e}")
            if image_name and image_tag:
                traffic_logger.log_pull(
                    image=image_name,
                    tag=image_tag,
                    client_ip=client_ip,
                    node_id=proxy_node.id,
                    node_name=proxy_node.name,
                    status="failed",
                    error_message=str(e)[:500],
                )
            raise
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
    return await proxy_v2(path=path, request=request)
