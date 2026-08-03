# POE 监控 — 新赛季更新说明

## 需要改的硬编码（5处）

| 文件 | 位置 | 内容 |
|------|------|------|
| `backend/app/models.py` | 第27行 | `default='S29赛季'` → 新赛季名 |
| `backend/app/api.py` | 第173行 | `data.get('league', 'S29赛季')` |
| `backend/app/api.py` | 第716行 | 同上 |
| `backend/app/api.py` | 第851行 | 同上 |
| `frontend/global_search.html` | 第22行 | 下拉框 `<option>S29赛季</option>` |

## 需要重新拉取的数据文件

| 文件 | 说明 | 如何更新 |
|------|------|----------|
| `backend/data/items.json` | 物品库（新物品/改名） | 调用腾讯API `/api/trade/data/static` |
| `backend/data/stats.json` | 词缀库（新词缀/改名） | 调用腾讯API `/api/trade/data/stats` |
| `frontend/items.json` | 前端搜索自动补全 | 同步 backend 的 items.json |

## 可能受影响但暂不需要改的

- **scheduler.py 词缀类型**：当前覆盖7种（explicit/implicit/fractured/crafted/enchant/veiled/mutated），新赛季如果加新词缀类型需要补充
- **frameType == 4**：技能宝石识别，如果官方改了这个值会出问题
- **docs.html**：示例里硬编码了 S29，影响不大但但最好同步更新

## ⏸️ 延期：词缀类型统一方案（暂不做）

### 问题
当前每种词缀类型在数据库里单独一个字段（`explicit_mods`、`implicit_mods` 等），POE API实际有13个分类，我们只处理了7个。未处理的词缀（地心/古神熔炉/异度天灾/致命贪婪/禁域/灌注）会被直接丢弃。

此外本赛季可能新增**蜕化词缀**（名称待确认），也需要支持。

### 方案：统一 `extra_mods` JSON 字段
在 `ItemSnapshot` 和 `Event` 表各加一个 `extra_mods = db.Column(db.JSON)` 字段，将所有非核心词缀类型存入此字段：

```json
{
  "delveMods": ["词缀1", "词缀2"],
  "crucibleMods": ["词缀3"],
  "sanctumMods": ["词缀4"]
}
```

**优点**：
- 不用每出新词缀类型就改数据库加字段
- 新赛季的蜕化词缀只需在 `_extract_mods_by_type` 加一行识别即可
- 搜索/匹配逻辑无需改动

**涉及改动**：
1. `models.py` — `ItemSnapshot` 和 `Event` 加 `extra_mods` 字段
2. `scheduler.py` — `_extract_mods_by_type` 里把非核心类型归入 `extra_mods`
3. 数据库迁移脚本 — 给现有表加字段
4. `api.py` — 查询/展示逻辑兼容 `extra_mods`

## 建议（非必须）

- 将赛季名称提取到配置文件，避免每次改代码
- 考虑从腾讯API的 league list 接口自动获取当前赛季
