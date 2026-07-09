# POE Monitor · 流放之路交易监控系统

一个针对 **流放之路（国服）** 市集交易的全自动监控工具。定期扫描指定账号或全服市场的物品上架/价格变动，实时记录交易事件，帮助你掌握市场动态。

## 功能

- **账号监控** — 定时扫描指定玩家的市集摊位，追踪所有上架物品
- **全服搜索** — 按自定义条件搜索全服物品（装备、宝石、通货等）
- **价格追踪** — 记录上架（listed）、售出（sold）、降价（price_change）事件
- **趋势分析** — 统计每日最低价、均价、数量变化
- **事件去重** — 智能识别重复事件，避免重复提醒
- **Web 管理面板** — 可视化查看监控目标、事件、趋势

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python Flask + SQLAlchemy |
| 数据库 | MySQL 8.0 |
| 任务调度 | APScheduler |
| 认证 | JWT (Flask-JWT-Extended) |
| 前端 | Vue 3 (CDN) + 原生 HTML/CSS |
| 部署 | Docker + Docker Compose |

## 项目结构

```
poe-monitor/
├── backend/                    # Flask 后端 API
│   ├── app/
│   │   ├── api.py              # REST API 路由
│   │   ├── auth.py             # 用户认证（JWT）
│   │   ├── config.py           # 配置（从环境变量读取）
│   │   ├── models.py           # 数据库模型
│   │   ├── poe_api.py          # 流放之路交易 API 封装
│   │   ├── scheduler.py        # 定时任务调度器
│   │   ├── utils.py            # 加密工具（POESESSID 加密/解密）
│   │   ├── fix_mods.py         # 词缀修复工具
│   │   ├── fill_fingerprint.py # 指纹填充工具
│   │   └── migrate_mods.py     # 词缀迁移工具
│   ├── data/                   # 游戏物品/词缀数据
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example            # 环境变量模板
│   └── main.py                 # 入口
│
├── frontend/                   # 静态前端页面（通过 nginx 反代）
│   ├── index.html              # 主控制面板
│   ├── global_search.html      # 全服搜索页面
│   ├── docs.html               # 接口文档
│   └── style.css / trade.css   # 样式
│
└── .gitignore
```

## API 概览

| 路径 | 方法 | 说明 |
|------|------|------|
| `/auth/login` | POST | 用户登录获取 JWT Token |
| `/api/monitor_targets` | GET/POST | 监控目标列表/添加 |
| `/api/monitor_targets/:id` | PUT/DELETE | 修改/删除监控目标 |
| `/api/events` | GET | 查询交易事件 |
| `/api/events/stats` | GET | 事件统计数据 |
| `/api/price_trends` | GET | 价格趋势数据 |
| `/api/global_search` | POST | 全服物品搜索 |
| `/api/user/credentials` | POST/PUT | 设置 POESESSID |
| `/api/scheduler` | GET/POST | 调度器状态/操作 |

## 快速部署

> **无需安装 MySQL、无需配置数据库** — Docker 一条命令全自动启动。

```bash
# 1. 克隆
git clone https://github.com/nousedinner/poe-monitor.git
cd poe-monitor/backend

# 2. 配置密钥
cp .env.example .env
# 建议修改 .env 中的密钥（默认值也可直接使用）

# 3. 一键启动（自动拉取 MySQL + 构建后端）
docker compose up -d --build

# 4. 访问 http://localhost:5000
#    默认账号：admin / 密码从数据库添加
```

> 初次启动约1-2分钟（MySQL 初始化 + 健康检查），之后秒启。

## 关于作者

使用 AI 辅助开发的个人项目。前端使用 Vue 3 CDN 方式编写，后端 Python Flask + MySQL，通过 Docker 部署在 Linux 服务器上。

---

> ⚠️ 本项目仅用于学习和个人使用，请遵守流放之路（国服）用户协议。
