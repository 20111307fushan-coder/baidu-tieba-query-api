from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main


class Result(list):
    err = None
    has_more = False


class FakeClient:
    async def search_exact(self, forum, keyword, pn, rn, only_thread):
        return Result([
            SimpleNamespace(
                title="测试标题", text="测试正文", show_name="作者甲", tid=123,
                fname=forum, create_time=1700000000,
            )
        ])

    async def get_user_threads(self, username, pn):
        return Result([
            SimpleNamespace(
                title="用户主题", contents=SimpleNamespace(text="首楼正文"),
                user=SimpleNamespace(show_name=username), tid=456, fname="测试吧",
                create_time=1700000001,
            )
        ])

    async def get_posts(self, tid, pn, rn):
        result = Result([
            SimpleNamespace(
                floor=1, user=SimpleNamespace(show_name="楼主"), text="完整正文",
                create_time=1700000002,
            )
        ])
        result.thread = SimpleNamespace(
            title="完整主题", user=SimpleNamespace(show_name="楼主"), tid=tid,
            fname="测试吧", contents=SimpleNamespace(text="完整正文"),
            create_time=1700000002,
        )
        return result


@asynccontextmanager
async def fake_client():
    yield FakeClient()


main._client = fake_client
client = TestClient(main.rest_app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_search():
    data = client.get("/api/v1/search", params={"forum": "测试", "keyword": "关键词"}).json()
    assert data["items"][0]["url"] == "https://tieba.baidu.com/p/123"
    assert data["items"][0]["body"] == "测试正文"


def test_user_threads():
    data = client.get("/api/v1/users/tester/threads").json()
    assert data["items"][0]["tid"] == 456
    assert data["items"][0]["author"] == "tester"
    assert data["has_more"] is True


def test_thread():
    data = client.get("/api/v1/threads/789").json()
    assert data["title"] == "完整主题"
    assert data["body"] == "完整正文"
    assert data["posts"][0]["floor"] == 1
    assert data["complete"] is True
    assert data["next_page"] is None
