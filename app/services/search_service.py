import logging
from typing import Any

import httpx

from app.config import config

logger = logging.getLogger("dockermirrorflow.search")


async def search_docker_hub(q: str, page_size: int = 25) -> dict[str, Any]:
    """
    通过配置的搜索代理依次尝试搜索 Docker Hub。

    返回:
      - 成功: {"results": [...], "source": "上游名称", "count": N}
      - 失败: {"results": [], "error": "...", "attempts": [...]}
    """
    q = (q or "").strip()
    if not q:
        return {"results": [], "error": "empty query"}

    if not config.search.enabled:
        return {"results": [], "error": "search disabled"}

    if not config.search.upstreams:
        return {"results": [], "error": "no upstream configured"}

    attempts: list[dict[str, str]] = []

    async with httpx.AsyncClient(
        timeout=config.search.timeout,
        headers={
            "User-Agent": "DockerMirrorFlow/1.0 (+https://github.com/1983shake/dockermirrorflow)",
            "Accept": "application/json",
        },
        follow_redirects=True,
    ) as client:
        for upstream in config.search.upstreams:
            try:
                logger.info(f"[search] 尝试上游: {upstream.name} -> {upstream.url}")
                resp = await client.get(
                    upstream.url,
                    params={"query": q, "page_size": page_size},
                )

                if resp.status_code != 200:
                    attempts.append(
                        {
                            "name": upstream.name,
                            "error": f"HTTP {resp.status_code}",
                        }
                    )
                    logger.warning(f"[search] {upstream.name} 返回 HTTP {resp.status_code}")
                    continue

                try:
                    data = resp.json()
                except Exception as e:
                    attempts.append({"name": upstream.name, "error": f"invalid json: {e}"})
                    logger.warning(f"[search] {upstream.name} 返回非 JSON 内容")
                    continue

                # 兼容多种响应格式
                results = data.get("results") or data.get("data") or data.get("repositories") or []

                logger.info(f"[search] {upstream.name} 成功，返回 {len(results)} 条")
                return {
                    "results": results,
                    "source": upstream.name,
                    "count": len(results),
                }

            except httpx.TimeoutException:
                attempts.append({"name": upstream.name, "error": "timeout"})
                logger.warning(f"[search] {upstream.name} 超时")
            except httpx.ConnectError as e:
                attempts.append({"name": upstream.name, "error": f"connect failed: {e}"})
                logger.warning(f"[search] {upstream.name} 连接失败: {e}")
            except Exception as e:
                attempts.append({"name": upstream.name, "error": str(e)})
                logger.error(f"[search] {upstream.name} 异常: {e}")

    return {
        "results": [],
        "error": "all upstreams failed",
        "attempts": attempts,
    }
