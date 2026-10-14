# 性能与运行约束

2026-10-15 更新。旧文档中“Flask 单进程只能处理一个请求”“SQLAlchemy 没有连接池”和未经压测的人数容量表已撤回。

已实现：

- API 使用 Waitress；独立 worker 负责扫描。WSGI 导入有数据库 dispatcher，不会在每个 Web 进程复制调度器。
- 每目标最多一个持久化任务，手动冷却不依赖成功扫描；worker 有单例租约和单任务租约，可在重启后恢复。
- 有界工作池按用户公平取任务，所有交易查询与详情通过跨进程数据库请求门。请求间隔可配置，429 的 Retry-After 延迟共享；401 不做专项恢复。
- 列表按集合聚合，SQL 次数不随目标数量线性增长；快照保留实例行，只更新变动字段和观察时间。
- events(target_id,created_at,id)、events(created_at)、price_trend(target_id,scan_time)、price_trend(scan_time) 索引；清理按小批执行。
- 公共静态数据验证成功后原子缓存；私有 /api 不共享缓存，响应 no-store。
- MySQL 已有连接池，连接参数通过 SQLALCHEMY_ENGINE_OPTIONS 设置；设置连接/读写超时、pool_pre_ping 与 recycle。
- 镜像不保留编译工具、不嵌入 .env；非 root，Compose 只读文件系统、资源上限和标准输出日志轮转。
- Vue/Sortable 本地固定版本，保留包来源、license 与 SHA256，不依赖运行时浮动 CDN。

仍需实测的指标：健康凭证下的请求成功率、扫描耗时和排队时长、API p95、数据库连接等待、CPU/内存与出口带宽。现有测试覆盖正确性和隔离环境中的并发边界，未给出生产可承载用户数，也未证明任何 Go 性能收益。

腾讯 100 条窗口与凭证过期的处理按用户要求不扩展。实际部署前需要数据库备份与恢复验证；历史误标修复另行核对。
