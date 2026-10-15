# 验证记录 · 2026-10-15

基线 eaf14feeec79bab8b95b04dcd889baff4bbf6968；修复及本次指纹回溯撤回提交到 GitHub review 分支，没有部署 VPS。

**本次撤回取消指纹回溯的验证**

| 检查 | 结果 |
|---|---|
| 完整 Python / SQLite 回归 | 95 项：93 通过、2 项 MySQL 专项跳过、0 失败 |
| 新增回溯检查 | 20 项通过，包含两个原窗口边界、两种 ID 均变化、跨扫描重挂、多实例数量、币种和目标隔离 |
| 原扫描代码对照 | 直接执行基线的原扫描方法；普通装备和技能石逐轮事件内容与恢复后的实现一致 |
| Node 前端逻辑 | 6 项通过 |
| Ruff、git diff --check | 通过 |
| 本次 MySQL / 容器复核 | 本机 Docker 引擎不可用，未重跑；不能将下述旧版本结果当作本次已通过 |

对照测试保留与目标物品无关的另一条在架记录，避免混入已单独修复的“全部空结果提前退出”差异。旧方法使用无新增唯一约束的测试快照表，匹配其原数据库结构；回溯方法本身未改写。87 条警告来自执行旧代码的 datetime.utcnow、Query.get 和原迁移重放，未抑制断言或跳过业务场景。

原来的“不同 ID、同指纹必须保留 sold 和 listed”断言已撤回；这种情况可能正是同一物品重挂。现在验证普通装备跨 ID 指纹衔接、24 小时回溯及旧后端技能石 10 分钟回溯，详见 docs/FINGERPRINT_BACKTRACKING.md。

原始本地结果：output/verification/fingerprint-rollback-sqlite.xml。复现命令为 `python -m pytest backend/tests -q`、`node --test frontend/tests/helpers.test.cjs` 和 `python -m ruff check backend`。没有使用生产数据库或真实交易凭据。

**以下是此前 0d0689f 的历史验证记录**

这些结果保留作审查记录，不代替上面的本次验证。此前有关稳定物品 ID、真实重复成交保留的测试只验证了预设假设，不能作为取消回溯符合业务需求的证据。

| 检查 | 结果 |
|---|---|
| Python / SQLite 回归 | 75 项：73 通过，2 项真实 MySQL 专项按设计跳过，0 失败 |
| Python / MySQL 8.0.45 回归 | 75 项全部通过，0 跳过、0 失败 |
| Node 前端逻辑检查 | 6 项全部通过 |
| Ruff Python 静态检查 | 通过，无未使用导入、未定义名称等 F 类错误 |
| Python 编译、页面脚本语法、git diff --check | 通过 |
| Docker 镜像 | 使用实际 backend/Dockerfile 构建成功，核心 app 源码哈希与最终工作树一致 |
| 实际 Compose 拓扑 | 从提交的标准 Compose 模型生成隔离配置；迁移、Waitress API、worker、MySQL 与合成 HTTP 上游实际运行通过 |
| HTTPS nginx 示例 | 使用临时自签证书，在 nginx:stable-alpine 容器中 nginx -t 通过 |
| 共享 MySQL 的 host 配置 | Compose 解析通过，三个服务均为 host 网络且 API_BIND=127.0.0.1，无 ports 配置；未在 VPS 执行 |
| 真实浏览器 | 登录、账号历史、搜索、趋势、创建监控、原空 POST 刷新、小数最低价、退出撤销、文本注入防护与 360px 无横向溢出已检查 |

Python 测试的 4 条警告来自用旧提交模型重建升级前数据库时的 datetime.utcnow，属于基线重放；新代码使用明确 UTC 时间函数。测试不抑制或绕过断言来处理失败。

**自动检查覆盖**

当时检查了事件字段和币种归属、同指纹多实例、预设稳定物品 ID 衔接、重复观察事件保留、空结果差分、不完整详情保留、配置变更拒绝旧结果和重建基线、旧租约拒绝写入、未变化快照不删除或重写 JSON、query_id 持久化、分数均价与金额边界、北京时间统计、查询数与分页范围、权限隔离、旧页面写回关闭、告警保留与权限、缓存错误/陈旧回退/并发/304、登录限次及 JWT 撤销、共享限速与 429、取消与租约恢复、旧库升级可重复且不清除历史、维护脚本 dry-run 和跨 200 条分页。

100 条窗口及上游 401 的两个测试用于证明用户要求保留的行为没有改变，不把它们算成已修复问题。

**真实容器流程**

交易测试网络设为 internal，交易请求只进入合成 HTTP 上游，不使用真实 Cookie，不访问腾讯接口。Docker Desktop 不发布 internal 网络的端口，因此测试从 API 容器内通过真实 HTTP 请求 Waitress；另启仅 API 的隔离存活探针，验证标准桥接网络的宿主机环回端口可访问。

已确认：

- 同名称同词缀的两件实例生成两条上架事件。
- A 从 10 改为 12.125，事件保留 A 的实例 ID、旧价 10 和新价 12.125；重复触发返回 429。
- 停止 worker 后新建目标，启动 worker 后持久化任务继续执行，并生成正确事件。
- 进程 UID/GID 为 10001，根文件系统只读，端口绑定 127.0.0.1，镜像没有 /app/.env。
- HTTP 退出后，原令牌调用 /api/user 返回 401。
- 试验用容器、网络和数据库在流程结束后清理；不操作共享的其他项目容器。

**浏览器检查与证据**

真实 Chrome/Playwright 操作原页面，后端使用本地测试库与合成上游。确认新建监控保留自由输入条件，在首次扫描前显示等待扫描；网页初始化、登录和刷新没有 mark_redundant 写请求。测试用恶意名称、词缀、币种与 Toast 内容作为文本显示，不创建注入元素；javascript 图片 URL 被移除。趋势同小时替换、滚动裁剪由自动检查与页面检查共同验证。

中途发现的搜索失焦清空条件已修复并补回归，随后再次实际创建监控成功。小数最低价 1.125 经页面修改和接口读取保持一致。退出测试主动使用旧令牌产生的预期 401 不属于页面脚本错误。保留原视觉主题，未做主题重设计。

原始本地证据位于 output/verification、output/stack、output/nginx、output/playwright，均未提交，避免混入日志、临时证书和合成会话。可提交的复现代码位于 backend/tests 与 frontend/tests。

**复现**

```sh
python -m pytest backend/tests -q
node --test frontend/tests/helpers.test.cjs
python -m ruff check backend
docker build -t poe-monitor:review-local backend
python backend/tests/stack_check.py
```

真实 MySQL 回归仅接受 localhost/127.0.0.1:13307 的 poe_test 测试库：创建隔离 MySQL 8.0.45 容器和对应测试用户，再把 POE_TEST_DATABASE 设置为该测试地址。测试会重建此专用数据库，不能指向业务库；代码会拒绝其他 MySQL 地址。默认不设置此变量即可运行临时 SQLite 测试。

**边界与待核对项**

未使用真实有效凭证进行生产扫描或压测，没有验证生产吞吐、长期平衡、实际成交真伪或备份恢复。没有修改线上 Cookie、任务、数据和防火墙；共享数据库 host 模式仅解析验证，实际部署仍需按现场环境验证。历史误标与异常事件、sold 是否改为疑似下架/成交的业务口径，已列入 docs/REVIEW_FIXES_2026-10-15.md，留待人工核对。
