from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any

import aiotieba
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict
from starlette.routing import Mount


TIEBA_THREAD_URL = "https://tieba.baidu.com/p/{tid}"
REQUEST_TIMEOUT = float(os.getenv("TIEBA_REQUEST_TIMEOUT", "20"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))


class Item(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    author: str
    tid: int
    forum: str
    url: str
    body: str
    create_time: int | None = None


class SearchResponse(BaseModel):
    forum: str
    keyword: str
    page: int
    has_more: bool
    items: list[Item]


class UserThreadsResponse(BaseModel):
    username: str
    page: int
    has_more: bool
    items: list[Item]


class Post(BaseModel):
    floor: int
    author: str
    body: str
    create_time: int | None = None


class ThreadResponse(Item):
    posts: list[Post]
    start_page: int
    pages_fetched: int
    has_more: bool
    complete: bool
    next_page: int | None


def _show_name(user: Any) -> str:
    if user is None:
        return ""
    for name in ("show_name", "nick_name_new", "user_name"):
        value = getattr(user, name, "")
        if value:
            return str(value)
    return ""


def _raise_upstream(result: Any) -> None:
    err = getattr(result, "err", None)
    if err:
        raise HTTPException(status_code=502, detail=f"贴吧上游请求失败: {err}")


@asynccontextmanager
async def _client():
    # Secrets are read only at runtime. They are never logged or returned.
    async with aiotieba.Client(
        BDUSS=os.getenv("TIEBA_BDUSS", ""),
        STOKEN=os.getenv("TIEBA_STOKEN", ""),
    ) as client:
        yield client


async def _call(awaitable: Any) -> Any:
    try:
        result = await asyncio.wait_for(awaitable, timeout=REQUEST_TIMEOUT)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="贴吧上游请求超时") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"贴吧上游请求失败: {exc}") from exc
    _raise_upstream(result)
    return result


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("API_KEY", "")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="无效或缺少 X-API-Key")


rest_app = FastAPI(
    title="Baidu Tieba Query API",
    version="1.0.0",
    description="基于 aiotieba 的只读贴吧查询接口。",
    dependencies=[Depends(require_api_key)],
)
rest_app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_requests: dict[str, deque[float]] = defaultdict(deque)


@rest_app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",", 1)[0].strip() or (request.client.host if request.client else "unknown")
    now = time.monotonic()
    bucket = _requests[ip]
    while bucket and bucket[0] <= now - 60:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    bucket.append(now)
    return await call_next(request)


@rest_app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"name": rest_app.title, "docs": "/docs", "health": "/health", "mcp": "/mcp"}


@rest_app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@rest_app.get("/api/v1/search", response_model=SearchResponse)
async def search(
    forum: str = Query(min_length=1, max_length=100),
    keyword: str = Query(min_length=1, max_length=100),
    page: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=30, ge=1, le=30),
) -> SearchResponse:
    async with _client() as client:
        results = await _call(
            client.search_exact(forum, keyword, pn=page, rn=limit, only_thread=True)
        )
    items = [
        Item(
            title=item.title or (item.text.splitlines()[0][:120] if item.text else ""),
            author=item.show_name,
            tid=item.tid,
            forum=item.fname or forum,
            url=TIEBA_THREAD_URL.format(tid=item.tid),
            body=item.text,
            create_time=item.create_time or None,
        )
        for item in results
    ]
    return SearchResponse(
        forum=forum,
        keyword=keyword,
        page=page,
        has_more=bool(getattr(results, "has_more", False)),
        items=items,
    )


@rest_app.get("/api/v1/users/{username}/threads", response_model=UserThreadsResponse)
async def user_threads(
    username: str,
    page: int = Query(default=1, ge=1, le=100),
) -> UserThreadsResponse:
    async with _client() as client:
        results = await _call(client.get_user_threads(username, pn=page))
    items = [
        Item(
            title=thread.title,
            author=_show_name(thread.user) or username,
            tid=thread.tid,
            forum=thread.fname,
            url=TIEBA_THREAD_URL.format(tid=thread.tid),
            body=thread.contents.text,
            create_time=thread.create_time or None,
        )
        for thread in results
    ]
    # aiotieba does not expose a page object here. A non-empty page means callers
    # should probe the next page; the first empty page terminates pagination.
    return UserThreadsResponse(username=username, page=page, has_more=bool(items), items=items)


@rest_app.get("/api/v1/threads/{tid}", response_model=ThreadResponse)
async def thread(
    tid: int = Path(gt=0),
    start_page: int = Query(default=1, ge=1, le=10000),
    max_pages: int = Query(default=1, ge=1, le=20),
    page_size: int = Query(default=30, ge=1, le=30),
) -> ThreadResponse:
    all_posts: list[Post] = []
    pages_fetched = 0
    has_more = False
    first_result: Any = None

    async with _client() as client:
        for page in range(start_page, start_page + max_pages):
            result = await _call(client.get_posts(tid, pn=page, rn=page_size))
            if first_result is None:
                first_result = result
            pages_fetched += 1
            all_posts.extend(
                Post(
                    floor=post.floor,
                    author=_show_name(post.user),
                    body=post.text,
                    create_time=post.create_time or None,
                )
                for post in result
            )
            has_more = bool(getattr(result, "has_more", False))
            if not has_more:
                break

    if first_result is None or not first_result.thread.tid:
        raise HTTPException(status_code=404, detail="未找到该帖子")
    meta = first_result.thread
    body = next((post.body for post in all_posts if post.floor == 1), meta.contents.text)
    return ThreadResponse(
        title=meta.title,
        author=_show_name(meta.user),
        tid=meta.tid,
        forum=meta.fname,
        url=TIEBA_THREAD_URL.format(tid=meta.tid),
        body=body,
        create_time=meta.create_time or None,
        posts=all_posts,
        start_page=start_page,
        pages_fetched=pages_fetched,
        has_more=has_more,
        complete=not has_more,
        next_page=(start_page + pages_fetched) if has_more else None,
    )


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

mcp = FastMCP(
    "百度贴吧阅读器",
    instructions=(
        "只读查询百度贴吧。回答贴吧内容问题前必须调用工具。搜索结果只是线索；"
        "涉及剧情、篇幅、完成度或角色重要性时，必须用 read_thread 读取正文和楼层。"
        "若结果 complete=false，继续用 read_thread_page 从 next_page 翻页，直到 has_more=false。"
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
)


@mcp.tool(
    title="搜索百度贴吧",
    description=(
        "按贴吧名称和关键词搜索主题帖。forum 填贴吧名称，不必包含末尾的“吧”。"
        "返回标题、作者、tid、直达链接、正文片段、页码和 has_more。"
        "has_more=true 时增加 page 继续搜索。搜索结果不能代替帖子正文。"
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
async def search_tieba(
    forum: str,
    keyword: str,
    page: int = 1,
    limit: int = 30,
) -> dict[str, Any]:
    """搜索一个贴吧内的主题帖。"""
    result = await search(forum=forum, keyword=keyword, page=page, limit=limit)
    return result.model_dump()


@mcp.tool(
    title="获取用户主题帖",
    description=(
        "按百度贴吧用户名获取该用户发布的主题帖。返回标题、作者、tid、贴吧、"
        "直达链接和首楼正文。has_more=true 时增加 page；遇到空页即已读完。"
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
async def get_user_threads(username: str, page: int = 1) -> dict[str, Any]:
    """读取指定贴吧用户的主题帖列表。"""
    result = await user_threads(username=username, page=page)
    return result.model_dump()


@mcp.tool(
    title="完整读取贴吧帖子",
    description=(
        "根据 tid 从第一页开始连续读取帖子正文和楼层，默认最多读取100页。"
        "回答剧情、篇幅、完成度或角色重要性时必须使用本工具。complete=true 表示已读取到"
        "has_more=false；若 complete=false，必须从 next_page 调用 read_thread_page 继续。"
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
async def read_thread(
    tid: int,
    max_pages: int = 100,
    page_size: int = 30,
) -> dict[str, Any]:
    """自动翻页读取主题帖；超长帖子可再调用 read_thread_page。"""
    max_pages = max(1, min(max_pages, 100))
    page_size = max(1, min(page_size, 30))
    result = await thread(
        tid=tid,
        start_page=1,
        max_pages=max_pages,
        page_size=page_size,
    )
    return result.model_dump()


@mcp.tool(
    title="读取贴吧帖子分页",
    description=(
        "读取指定 tid 从 start_page 开始的若干页。用于 read_thread 返回 complete=false 时续读。"
        "必须按 next_page 继续，直到 has_more=false。"
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
async def read_thread_page(
    tid: int,
    start_page: int,
    max_pages: int = 10,
    page_size: int = 30,
) -> dict[str, Any]:
    """从指定页继续读取主题帖楼层。"""
    max_pages = max(1, min(max_pages, 20))
    page_size = max(1, min(page_size, 30))
    result = await thread(
        tid=tid,
        start_page=max(1, start_page),
        max_pages=max_pages,
        page_size=page_size,
    )
    return result.model_dump()


# The MCP Starlette app owns the lifespan required by Streamable HTTP. The REST
# FastAPI app is the final fallback route, so /mcp and the existing REST API
# coexist on the same Render service.
app = mcp.streamable_http_app()
app.routes.append(Mount("/", app=rest_app))
