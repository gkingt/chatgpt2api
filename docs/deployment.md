# 部署与升级指南

本文介绍 ChatGPT2API 的常见部署方式，以及后续升级项目时需要保留的数据和执行步骤。

## 部署前准备

服务器需要安装：

- Docker
- Docker Compose v2
- Git

首次部署前建议确认：

```bash
docker version
docker compose version
git --version
```

项目核心持久化文件：

| 路径 | 作用 |
| --- | --- |
| `config.json` | 主配置、后台密钥、代理、图片、备份等配置 |
| `.env` | Docker compose 环境变量 |
| `data/` | 账号、注册配置、日志、图片、任务记录等运行数据 |

升级和迁移时重点保留以上内容。

## 方式一：普通 Docker 部署

适合不需要 WARP / FlareSolverr 清障的场景。

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
```

设置 `config.json` 中的 `auth-key`，或在 `docker-compose.yml` 中配置：

```yaml
environment:
  - CHATGPT2API_AUTH_KEY=your_secret_key
```

启动：

```bash
docker compose up -d
```

访问：

```text
http://localhost:3000
```

API 基础地址：

```text
http://localhost:3000/v1
```

查看日志：

```bash
docker logs -f chatgpt2api
```

停止：

```bash
docker compose down
```

## 方式二：WARP / FlareSolverr 部署

适合注册流程经常遇到 Cloudflare 拦截的场景。该方式会启动：

- `warp-proxy`
- `privoxy`
- `flaresolverr`
- `init-config`
- `app`

复制环境变量模板：

```bash
cp .env.example .env
```

至少修改 `.env` 中的：

```text
CHATGPT2API_AUTH_KEY=your_secret_key_here
```

启动：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

访问：

```text
http://localhost:3000
```

FlareSolverr 相关配置可以在后台设置页的 `FlareSolverr` tab 中查看和测试。更详细的 Cloudflare 清障说明见：

```text
docs/flaresolverr-cloudflare.md
```

查看容器状态：

```bash
docker compose -f docker-compose.warp.yml ps
```

查看日志：

```bash
docker logs -f chatgpt2api-warp
docker logs -f chatgpt2api-flaresolverr
```

停止：

```bash
docker compose -f docker-compose.warp.yml down
```

## 方式三：源码运行

适合本地开发或临时调试。

后端：

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

前端开发服务：

```bash
cd web
bun install
bun run dev
```

源码方式运行时，后端默认读取项目根目录的 `config.json` 和 `data/`。

## 存储后端

默认使用本地 JSON 文件：

```text
STORAGE_BACKEND=json
```

可选值：

| 值 | 说明 |
| --- | --- |
| `json` | 本地 JSON 文件，默认方式 |
| `sqlite` | 本地 SQLite，通常存放在 `data/accounts.db` |
| `postgres` | 外部 PostgreSQL |
| `git` | Git 私有仓库存储账号数据 |

PostgreSQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

SQLite 示例：

```yaml
environment:
  - STORAGE_BACKEND=sqlite
  - DATABASE_URL=sqlite:////app/data/accounts.db
```

## 升级前备份

升级前建议备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

如果没有 `.env`，可以去掉：

```bash
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json data
```

也可以在后台设置页配置 Cloudflare R2 备份，用于定时备份关键数据。

## 升级：普通 Docker 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码和镜像：

```bash
git pull
docker compose pull
docker compose up -d
```

查看状态：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 升级：WARP / FlareSolverr 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码并重新构建：

```bash
git pull
docker compose -f docker-compose.warp.yml up -d --build
```

查看状态：

```bash
docker compose -f docker-compose.warp.yml ps
docker logs -f chatgpt2api-warp
```

## 升级：源码运行

```bash
cd chatgpt2api
git pull
uv sync
```

如果需要重新构建前端静态产物：

```bash
cd web
bun install
bun run build
```

然后按你的进程管理方式重启后端服务。

## 回滚

如果升级后需要回滚代码：

```bash
git log --oneline -n 20
git checkout <旧版本commit>
```

普通 Docker 部署：

```bash
docker compose up -d
```

WARP / FlareSolverr 部署：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

如果需要恢复数据：

```bash
tar -xzf backups/你的备份文件.tgz
```

恢复数据前建议先停止容器，避免运行中写入覆盖：

```bash
docker compose down
```

或：

```bash
docker compose -f docker-compose.warp.yml down
```

## 常用维护命令

查看容器：

```bash
docker compose ps
```

查看主服务日志：

```bash
docker logs -f chatgpt2api
```

查看 WARP 部署主服务日志：

```bash
docker logs -f chatgpt2api-warp
```

重启普通部署：

```bash
docker compose restart
```

重启 WARP 部署：

```bash
docker compose -f docker-compose.warp.yml restart
```

清理未使用镜像：

```bash
docker image prune
```

不要直接删除 `data/`、`config.json`、`.env`，除非已经确认有可用备份。

## 大日志与图片清理

- 日志查询按块从文件末尾读取，最多返回 200 条匹配记录；页面的“全选”仅选择这些返回的记录，不代表清空全部历史日志。
- 删除所选日志采用流式重写和原子替换，保留重写期间新追加的日志。重写需要额外磁盘空间，接近剩余日志文件的大小；空间不足时操作失败并保留原文件。
- 不会因为本次优化自动清空、截断或轮转历史日志。大日志删除或很早日期的筛选仍可能耗时，但不会再把整个日志文件一次性读入内存。
- 单条记录超过 4 MiB 时不在日志列表展示，避免异常大行耗尽内存；删除其他记录时仍原样保留这些超大记录。
- 图片过期清理由后台任务在启动后及每 30 分钟执行，遵循 `image_retention_days`（默认 30 天），并同步清理缩略图、标签和存储索引；图片列表和每次保存图片不再触发全盘清理。
- 按日期删除包含所选截止日。手动删除和过期清理会删除已索引的 WebDAV 副本；WebDAV 删除失败时保留该图片的本地副本、索引和标签，以便重试。
- “清理至指定剩余空间”只清理本地图片，支持 PNG、JPG、JPEG、WebP、GIF、SVG；已有 WebDAV 副本的图片保留远端索引和标签。是否达到目标根据清理后的实际磁盘剩余空间判断。
- 图片列表首次加载会建立旧图片索引，随后对外部新增文件的目录扫描最多每 30 秒一次；应用自身保存的图片会立即入索引。批量下载较大 ZIP 时自动使用临时磁盘文件，避免整个压缩包驻留内存。

应用代码更新后，Docker 部署需要重新构建并重建应用容器，仅执行 `docker restart` 不会加载镜像外的新代码：

```bash
docker compose up -d --build app
```

请使用实际部署时的 Compose 文件；通过面板或其他方式创建的容器，需要在对应部署入口重新构建，不能仅凭容器名称另建一套服务。

## 注册失败保护与配置恢复

- 主设置存放在项目根目录 `config.json`；注册邮箱提供商、密钥和注册参数单独存放在 `data/register.json`。升级和迁移时两者都必须保留，不能只复制主配置文件。
- 未启用邮箱提供商、空 Outlook 邮箱池或部分必填项缺失时，注册启动接口返回 400，不创建任务。运行期间普通失败按 2、4、8、16、32、60 秒退避；连续失败达到 10 次或遇到不可重试配置错误时保护停止，并对自动启动设置 60 秒冷却。
- “停止”独立于“保存中”状态，取消信号先于配置落盘发出。远端请求退出前显示“停止中”，此时不能重新启动或改动后端配置，避免新旧任务共用取消状态。
- 实时状态推送只更新运行数据，不再覆盖本页面未保存的表单；注册配置版本冲突时拒绝旧页面覆盖，需刷新核对后再保存。
- 旧版注册任务会每 2 秒把运行统计与邮箱配置一起覆盖写入 `data/register.json`；启用自动补号后，账号刷新/健康检查即使判定“额度充足、不启动”，也可能重写此文件。因此无需点击保存也可能发生文件写入。若写入中断，旧版会在重启后静默使用默认邮箱配置，并可能被下一次后台检查写回。新版只更新这些检查的内存统计，不再因“不启动”的检查写入邮箱配置；运行期间落盘仍使用安全保存与备份。
- 保存采用临时文件、同步落盘和替换，并保留上一份可读配置：注册备份为 `data/register.json.bak`，主配置备份为 `data/config.json.bak`。单文件 Docker 挂载无法原子替换时，先保存持久化备份，再原位写入。
- WARP 的 `init-config` 初始化服务使用相同的安全保存和恢复逻辑；对应 Compose 配置需要挂载 `utils/` 和持久化 `data/`，不要只更新初始化脚本而遗漏挂载。
- 主文件损坏但备份有效时尝试读取备份；注册配置恢复后不会自动启动。主文件及备份均不可读时，注册页面显示错误并禁止覆盖保存，避免把默认配置写回原文件；主配置则拒绝静默降级为默认设置。

备份中含有认证密钥和邮箱凭据，不要公开。恢复文件前先停止原应用，并另外保留损坏文件用于排查；旧版本已丢失且没有备份的凭据，无法由修复代码自动还原。
