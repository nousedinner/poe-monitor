# POE Monitor · 流放之路交易监控系统

针对流放之路国服市集的个人监控工具：账号监控、全服条件搜索、价格变动与观察事件、趋势图和 Web 管理面板。

后端为 Python Flask + SQLAlchemy + MySQL；网页使用本地固定版本的 Vue 3 与 Sortable。API 由 Waitress 提供，扫描由独立 worker 执行，任务与限速状态存入同一数据库。

全服接口最多返回 100 个 ID；本工具保留此限制和既有窗口退出判断。sold 是可见结果消失的推测，不代表有实际成交回执。POESESSID 过期需自行更新，目前没有自动续期或专项恢复。

账号普通装备重新上架后 ID 可变化，因此仍按指纹识别重挂，并保留 24 小时回溯修正；技能石后端保留原来的 10 分钟回溯。前端排除技能石的冗余分组属于另一层处理。此次已撤回取消指纹回溯的改动，具体范围和原注释见 [指纹回溯说明](docs/FINGERPRINT_BACKTRACKING.md)。

## 新安装

需要 Python 3.12、Docker 和 Docker Compose。以下在 backend 目录执行：

```sh
python manage.py init-env
docker compose up -d --build
docker compose run --rm poe python manage.py create-user admin --admin
```

init-env 只生成新安装的随机密钥，不覆盖已有 .env，不显示密钥。应用没有公开注册或默认密码；创建用户时交互输入密码。

Compose 会先启动 MySQL，再执行显式迁移，随后启动 API 与 worker。API 仅发布到宿主机 127.0.0.1:5000，MySQL 不发布公网端口。前端需将整个 frontend 目录交给 nginx 静态服务，按 nginx-example.conf 配置域名与 HTTPS 证书；同一域名下 /api/ 和 /auth/ 反代到环回地址的 API。直接访问 API 根路径不是网页入口。

## 升级已有 VPS

先备份数据库、旧镜像、原 .env，并确认恢复方法。保留原三类密钥与 DATABASE_URL；不要运行 init-env 重建线上密钥。关闭旧 PoE 容器中的扫描，保持共享 MySQL 和其他项目不动。

如果现有数据库在宿主机 127.0.0.1:3306，应使用独立配置：

```sh
docker compose -f docker-compose.host.yml up -d --build
```

该配置不管理 MySQL，API 在 host 网络上也只监听 127.0.0.1。上线时还应关闭原先允许公网 5000 的防火墙规则；仓库里的配置修改不会自动更改 VPS 防火墙。

迁移会增加任务、告警、会话撤销等表，并将价格列改为 DECIMAL(20,6)。MySQL DDL 自动提交，步骤设计为可重复执行；有重复快照时会在修改前退出。旧价格代码不保证兼容新增小数语义，回滚需要匹配的数据库备份与旧镜像。详细变化、保留项和历史数据边界见 [修复说明](docs/REVIEW_FIXES_2026-10-15.md)。

## 本地开发与测试

```sh
cd backend
python -m venv .venv
# 激活环境后：
pip install -r requirements-dev.txt
python manage.py init-env
# 将 .env 中 DATABASE_URL 改为本地隔离的数据库地址
python manage.py upgrade
python main.py
# 另一个终端：
python worker.py
```

API 的 WSGI 导入不会启动调度线程；无论采用什么 WSGI 容器，都要单独运行一个 worker。默认使用标准错误日志，由 Compose 的 Docker 日志配置轮转。可用 LOG_DIR 为单个进程额外配置文件日志；多个进程不要共用同一轮转文件。

从仓库根目录执行快速检查：

```sh
python -m pytest backend/tests -q
node --test frontend/tests/helpers.test.cjs
```

测试默认使用临时 SQLite，并阻止外部 HTTP。MySQL 事务与精度检查使用明确的本地测试地址，详见 [验证记录](VALIDATION.md)。完整 Docker 角色检查使用 tests/stack_check.py：只允许本地 Docker 上的专用隔离项目，保留实际 Compose 模型，替换为无外网网络和合成 HTTP 上游，结束后清理自己的容器。

## 常用接口

| 路径 | 方法 | 用途 |
|---|---|---|
| /auth/login、/auth/logout | POST | 登录、服务端撤销当前令牌 |
| /api/user | GET | 当前用户与管理员标志 |
| /api/targets | GET / POST | 目标与汇总、创建目标 |
| /api/targets/:id | PUT / DELETE | 修改、删除自己的目标 |
| /api/targets/:id/trigger | POST | 手动入队，重复请求或冷却期间返回 429 |
| /api/targets/:id/snapshots | GET | 当前快照与真实观察时间 |
| /api/targets/:id/events、/api/events/all | GET | 事件查询 |
| /api/stats、/api/targets/:id/sale_stats | GET | 按币种统计 |
| /api/targets/:id/price_trend | GET | 趋势，支持 cursor_id 增量游标 |
| /api/credentials | GET / POST | 查看更新时间、保存加密凭证 |
| /api/search、/api/search/monitor | POST | 全服搜索、从查询创建监控 |
| /api/logs | GET | 自己的最近告警；管理员可见全局 |
| /api/trade/static、/api/trade/stats、/api/trade/filters | GET | 公共静态数据缓存代理 |
| /healthz、/readyz | GET | API 存活、数据库与迁移就绪 |

private API 响应禁止共享缓存；当前批量 mark_redundant 接口返回 410，旧页面也不能自动改写历史。

## 历史维护与资料

历史修复工具默认 dry-run。缺少可核对原文的事件会跳过，旧冗余标记与异常事件未被批量恢复：

```sh
python -m app.fill_fingerprint
python -m app.fix_mods
```

只有备份、核对结果后，才考虑显式 --apply；迁移请使用 python manage.py upgrade。

本项目使用 AI 辅助开发，属于个人学习和使用工具，请遵守流放之路国服相关使用协议。
