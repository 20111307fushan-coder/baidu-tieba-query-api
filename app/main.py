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
from pydantic import BaseModel, ConfigDict


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
    items: list[Item]


class Post(BaseModel):
    floor: int
    author: str
    body: str
    create_time: int | None = None


class ThreadResponse(Item):
    posts: list[Post]
    pages_fetched: int
    has_more: bool


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


app = FastAPI(
    title="Baidu Tieba Query API",
    version="1.0.0",
    description="基于 aiotieba 的只读贴吧查询接口。",
    dependencies=[Depends(require_api_key)],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_requests: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
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


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"name": app.title, "docs": "/docs", "health": "/health"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/search", response_model=SearchResponse)
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


@app.get("/api/v1/users/{username}/threads", response_model=UserThreadsResponse)
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
    return UserThreadsResponse(username=username, page=page, items=items)


@app.get("/api/v1/threads/{tid}", response_model=ThreadResponse)
async def thread(
    tid: int = Path(gt=0),
    max_pages: int = Query(default=1, ge=1, le=20),
    page_size: int = Query(default=30, ge=1, le=30),
) -> ThreadResponse:
    all_posts: list[Post] = []
    pages_fetched = 0
    has_more = False
    first_result: Any = None

    async with _client() as client:
        for page in range(1, max_pages + 1):
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
        pages_fetched=pages_fetched,
        has_more=has_more,
    )
