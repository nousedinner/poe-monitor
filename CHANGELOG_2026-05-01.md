# POE Monitor 词缀修复说明文档

## 修改日期
2026-05-01

## 修复的问题

### 问题1：品质、等级丢失
**原因**：`_extract_mods_by_type` 函数只提取7种标准词缀类型（explicitMods、implicitMods、fracturedMods、craftedMods、enchantMods、mutatedMods、veiledMods），不包含 `properties` 字段。宝石的品质（+20%）和等级（1级）存储在 `properties` 里。

**影响范围**：所有宝石事件

### 问题2：强辅宝石词缀为空
**原因**：启蒙（Enlighten）、增幅（Empower）、赋予（Enhance）等强辅宝石的"等级"、"消耗及保留加成"等数据在 `properties` 里，不在标准词缀类型中。

**影响范围**：强辅宝石事件

### 问题3：内置辅助词缀丢失
**原因**：`_extract_mods_by_type` 函数不处理 `builtInSupport` 字段。

**影响范围**：部分宝石事件

### 问题4：sold 事件词缀不完整
**原因**：`scheduler.py` 中两处创建 sold 事件的代码（account监控的宝石和非宝石）只从快照复制 `explicit_mods`，未复制其他6个词缀字段。

**影响范围**：account监控的 sold 事件

**注意**：全服监控的 sold 事件不处理词缀，因为全服监控只关心市场整体情况，不关心具体售出物品的属性。

## 修改的文件

### scheduler.py

#### 1. 修改 `_extract_mods_by_type` 函数（line 32-64）
**改动内容**：在原有逻辑基础上，增加对 `properties`、`notableProperties`、`builtInSupport` 的处理，把这些字段的内容合并到 `explicitMods` 中返回。

**修改前**：
```python
def _extract_mods_by_type(self, item_info):
    """提取不同类型的词缀"""
    mods = {}
    mod_types = ['explicitMods', 'implicitMods', 'fracturedMods', 'craftedMods', 'enchantMods', 'veiledMods', 'mutatedMods']
    
    for mod_type in mod_types:
        if mod_type in item_info and item_info[mod_type]:
            mods[mod_type] = item_info[mod_type]
        else:
            mods[mod_type] = []
    
    return mods
```

**修改后**：
```python
def _extract_mods_by_type(self, item_info):
    """提取不同类型的词缀"""
    mods = {}
    mod_types = ['explicitMods', 'implicitMods', 'fracturedMods', 'craftedMods', 'enchantMods', 'veiledMods', 'mutatedMods']
    
    for mod_type in mod_types:
        if mod_type in item_info and item_info[mod_type]:
            mods[mod_type] = item_info[mod_type]
        else:
            mods[mod_type] = []
    
    # 新增：把 properties、notableProperties、builtInSupport 合并到 explicitMods
    extra = []
    if 'notableProperties' in item_info:
        for np in item_info['notableProperties']:
            if 'name' in np:
                extra.append(np['name'])
    if 'properties' in item_info:
        for prop in item_info['properties']:
            name = prop.get('name', '')
            values = prop.get('values', [])
            if values and values[0] and values[0][0]:
                value_str = values[0][0]
                extra.append(f"{name}: {value_str}")
    if 'builtInSupport' in item_info and item_info['builtInSupport']:
        extra.append(f"内置辅助: {item_info['builtInSupport']}")
    
    if extra:
        mods['explicitMods'] = mods.get('explicitMods', []) + extra
    
    return mods
```

#### 2. 修复宝石 sold 事件词缀（line 250-268）
**改动内容**：在创建 sold 事件时，增加对其他6个词缀字段的复制。

**修改前**：
```python
event = Event(
    target_id=target.id,
    item_id=snapshot.item_id,
    item_name=snapshot.item_name,
    base_type=snapshot.base_type,
    explicit_mods=snapshot.explicit_mods,
    price=snapshot.price,
    price_currency=snapshot.price_currency,
    event_type='sold',
    is_gem=is_gem,
    fingerprint_hash=fp_hash
)
```

**修改后**：
```python
event = Event(
    target_id=target.id,
    item_id=snapshot.item_id,
    item_name=snapshot.item_name,
    base_type=snapshot.base_type,
    explicit_mods=snapshot.explicit_mods,
    implicit_mods=snapshot.implicit_mods,
    fractured_mods=snapshot.fractured_mods,
    crafted_mods=snapshot.crafted_mods,
    enchant_mods=snapshot.enchant_mods,
    mutated_mods=snapshot.mutated_mods,
    veiled_mods=snapshot.veiled_mods,
    price=snapshot.price,
    price_currency=snapshot.price_currency,
    event_type='sold',
    is_gem=is_gem,
    fingerprint_hash=fp_hash
)
```

#### 3. 修复非宝石 sold 事件词缀（line 367-385）
**改动内容**：同上，增加对其他6个词缀字段的复制。

## 未修改的文件

- **models.py**：不需要修改数据库模型
- **api.py**：已在之前的修改中完成（返回全部词缀字段）
- **index.html**：已在之前的修改中完成（`getAllMods` 合并全部词缀）
- **global_search.html**：搜索功能不涉及，快照显示自动兼容

## 已知缺陷和限制

### 1. 词缀分类模糊
**描述**：`properties`（品质、等级等）被合并到 `explicitMods` 中，无法区分哪些是标准词缀，哪些是属性词缀。

**影响**：如果未来需要按词缀类型分类显示，需要重新设计数据结构。

**建议**：如果需要分类显示，考虑方案二（分离存储）。

### 2. 旧数据不完整
**描述**：2026-04-18 之前创建的事件，其 `explicit_mods` 字段只包含标准词缀，不包含 properties。

**影响**：旧事件的品质、等级信息无法恢复。

**建议**：如果需要恢复旧数据，需要写迁移脚本重新处理历史数据。

### 3. 指纹匹配可能受影响
**描述**：`_compute_fingerprint_hash` 使用 `_collect_all_mods` 计算指纹，新代码的词缀内容可能与旧代码不同。

**影响**：同一个物品的指纹可能与旧代码不同，导致回溯匹配（sold→listed）失败。

**建议**：密切监控回溯匹配的成功率，必要时调整匹配逻辑。

## 测试验证

### 验证方法
1. 检查容器日志，确认服务正常启动
2. 通过 API 查询事件数据，确认词缀完整
3. 在前端页面查看事件显示，确认词缀正确

### 验证命令
```bash
# 检查容器状态
docker ps --filter "name=poe"

# 查看容器日志
docker logs poe-monitor --tail 20

# 进入容器验证代码
docker exec poe-monitor cat /app/app/scheduler.py | head -60
```

## 回滚方案

如果出现问题，可以回滚到修改前的版本：

```bash
# 恢复 scheduler.py
cp /home/zzy/docker/poe/app/scheduler.py.bak /home/zzy/docker/poe/app/scheduler.py

# 复制到容器
docker cp /home/zzy/docker/poe/app/scheduler.py poe-monitor:/app/app/scheduler.py

# 重启容器
docker compose -f /home/zzy/docker/poe/docker-compose.yml restart poe
```

## 后续优化建议

1. **分离存储 properties**：如果需要更清晰的词缀分类，考虑在数据库中新增 `properties_mods` 字段。
2. **数据迁移脚本**：编写脚本处理历史数据，补全丢失的词缀信息。
3. **监控告警**：添加词缀完整性检查，及时发现类似问题。
