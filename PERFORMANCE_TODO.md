# POE Monitor 性能优化待办

## 当前架构问题

### 1. Flask 单进程
- **现状**：`python main.py` 单进程运行，一次只能处理一个请求
- **影响**：并发一多就排队，30-50人同时访问就吃力
- **修复**：Gunicorn + gevent 多进程
  ```bash
  # docker-compose.yml 改 command
  command: gunicorn -w 4 -k gevent -b 0.0.0.0:5000 main:app
  # 内存开销：约 50MB/worker × 4 = 200MB
  ```

### 2. 无 API 响应缓存
- **现状**：每个用户轮询都打到后端，100人在线 = 100倍后端压力
- **影响**：后端成为瓶颈，用户越多越慢
- **修复**：Nginx 缓存 API 响应
  ```nginx
  # poe.cancanneed.top 配置
  location /api/ {
      proxy_pass http://127.0.0.1:5000/api/;
      proxy_cache_valid 200 90s;  # 缓存90秒（后端120秒扫一次）
      add_header X-Cache-Status $upstream_cache_status;
      # ... 其他 proxy 配置保持不变
  }
  ```
  **效果**：后端每120秒只处理1次请求，其他人全走缓存。这是最大的性能提升点。

### 3. 无静态文件缓存
- **现状**：每次刷新都重新下载 4.7M 页面资源
- **影响**：浪费带宽，用户等待时间长
- **修复**：Nginx 加 Cache-Control
  ```nginx
  location ~* \.(js|css|png|jpg|json)$ {
      expires 1h;
      add_header Cache-Control "public, immutable";
  }
  ```

### 4. MySQL 无连接池
- **现状**：每次查询新建连接
- **影响**：并发高了数据库连接耗尽
- **修复**：SQLAlchemy 配置
  ```python
  SQLALCHEMY_POOL_SIZE = 10
  SQLALCHEMY_POOL_RECYCLE = 3600
  ```

### 5. 全局限速队列（多用户瓶颈）
- **现状**：`scheduler.py` 全局一个 `request_queue` + 单消费者线程，扫完一个任务强制 `time.sleep(6)` 才处理下一个
- **背景**：6秒间隔是根据腾讯API限速反推出来的最合理值（针对**单个 POESESSID** 的请求节奏）
- **问题**：6秒限速被实现成**全局串行**——所有用户的扫描任务共用一个队列。单用户时没问题（120秒扫一次，6秒间隔绰绰有余）；多用户时任务会堆积（如100用户每120秒扫一次 = 平均每1.2秒来一个任务，但消费者每6秒才处理一个）
- **影响**：用户越多，扫描排队越长，数据越陈旧
- **修复**：限速粒度从"全局"改为"按 POESESSID 独立"——每个凭证各维护自己的请求节奏（6秒间隔），不同用户的凭证互不排队
  ```python
  # 伪代码：按凭证维度限速
  self.rate_limiters = {}  # {poesessid_hash: (last_scan_time, lock)}
  # 每个目标入队前，先检查自己凭证的限速器，而不是全局队列
  ```

## 优化后容量估算

| 优化程度 | 同时在线 | 瓶颈 |
|---------|---------|------|
| 不优化（现状） | 30-50 人 | Flask 单进程 |
| 只加 Gunicorn | 100-150 人 | 内存（3.8G） |
| Gunicorn + Nginx 缓存 | 300-500 人 | 带宽（20Mbps） |
| 全部优化 | 500+ 人 | 接近带宽极限 |

**关键结论**：瓶颈从"服务器处理不过来"变成"20Mbps带宽够不够用"。因为前端轮询间隔=后端扫描间隔（默认120秒），每个用户每2分钟才请求一次，每次才几百KB，带宽绰绰有余。

## 实施优先级

1. **P0 - Nginx API 缓存**：效果最大，改动最小，10分钟搞定
2. **P1 - Gunicorn 多进程**：配合缓存效果更好
3. **P2 - 静态文件缓存**：省带宽，提升用户体验
4. **P3 - MySQL 连接池**：预防性优化

## 注意事项

- Gunicorn 的 worker 数量受内存限制（3.8G总内存，swap已满），建议最多4个 worker
- Nginx 缓存时间不能超过后端扫描间隔（120秒），否则数据会过期
- 优化前先备份 nginx 配置和 docker-compose.yml
