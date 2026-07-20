# 百度贴吧查询 API

一个基于 FastAPI 和 [aiotieba](https://github.com/lumina37/aiotieba) 的只读查询服务，适合部署到 Render。

## 接口

- `GET /api/v1/search?forum=贴吧名&keyword=关键词&page=1&limit=30`
- `GET /api/v1/users/{用户名}/threads?page=1`
- `GET /api/v1/threads/{tid}?max_pages=1&page_size=30`
- `GET /health`
- Swagger 文档：`GET /docs`
- MCP Streamable HTTP：`POST /mcp`

所有帖子结果均包含标题、作者、tid、贴吧直达链接和正文。按 tid 查询还返回各楼层；将 `max_pages` 调高可继续读取回帖，最高 20 页。

## ChatGPT MCP App

在 ChatGPT Developer Mode 中连接：

```text
https://baidu-tieba-query-api.onrender.com/mcp
```

提供四个只读工具：`search_tieba`、`get_user_threads`、`read_thread` 和
`read_thread_page`。`read_thread` 默认自动读取最多 100 页；超长帖子会返回
`complete=false` 与 `next_page`，再由 `read_thread_page` 续读至
`has_more=false`。

## 安全配置

公开查询通常无需登录。若百度限制匿名访问，可在 Render 的 **Environment** 页面添加 `TIEBA_BDUSS` 和 `TIEBA_STOKEN`。不要把 Cookie 写入源码、`.env.example` 或 GitHub Actions。

可选地设置 `API_KEY`；设置后，除 `/health` 外的请求须带请求头：

```text
X-API-Key: 你的密钥
```

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest -q
```

## Render 部署

仓库根目录的 `render.yaml` 已配置免费 Web Service、健康检查和自动部署。在 Render Dashboard 选择 **New > Blueprint**，连接此仓库并应用 Blueprint 即可。

> Render 免费实例会在空闲后休眠，因此第一次请求可能较慢；若要求真正无冷启动的 24/7 在线服务，应在 Render 将方案升级为付费实例。

本项目仅封装公开查询能力。请遵守百度贴吧服务条款、隐私规则和合理请求频率。
