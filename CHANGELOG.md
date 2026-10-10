# CHANGELOG

本文件记录每次改动的摘要（日期倒序）。详细技术档案见对应 commit 或仓库内专题文档。

## 2026-10-10

### 修复（线上已有、本次补交进仓库）
- 后端 `scheduler.py`：API 词缀字段可能返回 dict，优先取 `description` 其次 `text`，避免类型错误
- 前端 `index.html` 搜索：数组元素兼容 dict，取 `description||text`，与后端同一防护
- 全局搜索 `global_search.html`：修复文本过滤框（input 类型 `in`）既未收集也未提交的问题

### 工程
- 补交 `models.py` 默认赛季 S29→S30（2026-08-03 CHANGELOG 记为"已纳入提交"，实际未推，本次更正）
- 本次为线上→仓库单向同步，仓库追平生产；核对方法见 poe-monitor 技能 `references/version-check.md`

## 2026-08-03

### 新增
- 全局搜索页移动端适配：搜索框两行布局（输入框全宽 + 三按钮平分）、下拉物品名单行省略号显示（global_search.html / global_search.css）
- 建立本 CHANGELOG 机制：此后每次改动同步更新本文件并随代码提交
- 缓存优化：`/api/trade/static`、`/api/trade/stats` 增加腾讯数据缓存（6h TTL，首次拉取存本地，所有用户共享，不再每次打腾讯 API；腾讯故障时兜底返回旧缓存）
- 缓存优化：nginx 对 HTML/JSON/CSS/JS 统一 `no-cache` 协商缓存（ETag 自动失效，日常访问 304 零流量）

### 修复
- 手机端点搜索框弹"自动填充密码"：页面登录弹窗含密码框导致浏览器误判登录场景。搜索框 autocomplete 改 new-password，登录框标注 username / current-password

### 工程
- git 仓库追平生产现状（生产领先的 S30 赛季、词缀渲染 m.description||m.text 修复纳入提交）
- 部署后浏览器可能缓存旧页面，验证需清缓存/无痕模式

## 2026-05-01

### 修复
- 宝石词缀问题：品质/等级丢失、强辅宝石词缀为空、内置辅助词缀丢失、sold 事件词缀不完整。详见专题文档 `CHANGELOG_2026-05-01.md`

---

> 更早的改动（2026-04 前后 UI 重构、趋势功能、拖拽排序等）未留下完整记录，此后所有改动强制记录于本文件。
