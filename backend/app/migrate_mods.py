#!/usr/bin/env python3
"""
迁移脚本：添加新的词缀字段并处理过往数据
"""
import json
from app import create_app, db
from sqlalchemy import text

def migrate():
    app = create_app()
    with app.app_context():
        print("开始数据库迁移...")
        
        # 1. 添加新字段到 item_snapshots 表
        print("\n1. 迁移 item_snapshots 表...")
        try:
            db.session.execute(text('ALTER TABLE item_snapshots ADD COLUMN implicit_mods TEXT'))
            print("  ✓ 添加 implicit_mods 字段")
        except Exception as e:
            print(f"  - implicit_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE item_snapshots ADD COLUMN fractured_mods TEXT'))
            print("  ✓ 添加 fractured_mods 字段")
        except Exception as e:
            print(f"  - fractured_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE item_snapshots ADD COLUMN crafted_mods TEXT'))
            print("  ✓ 添加 crafted_mods 字段")
        except Exception as e:
            print(f"  - crafted_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE item_snapshots ADD COLUMN enchant_mods TEXT'))
            print("  ✓ 添加 enchant_mods 字段")
        except Exception as e:
            print(f"  - enchant_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE item_snapshots ADD COLUMN mutated_mods TEXT'))
            print("  ✓ 添加 mutated_mods 字段")
        except Exception as e:
            print(f"  - mutated_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE item_snapshots ADD COLUMN veiled_mods TEXT'))
            print("  ✓ 添加 veiled_mods 字段")
        except Exception as e:
            print(f"  - veiled_mods 字段已存在或添加失败: {e}")
        
        db.session.commit()
        
        # 2. 添加新字段到 events 表
        print("\n2. 迁移 events 表...")
        try:
            db.session.execute(text('ALTER TABLE events ADD COLUMN implicit_mods TEXT'))
            print("  ✓ 添加 implicit_mods 字段")
        except Exception as e:
            print(f"  - implicit_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE events ADD COLUMN fractured_mods TEXT'))
            print("  ✓ 添加 fractured_mods 字段")
        except Exception as e:
            print(f"  - fractured_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE events ADD COLUMN crafted_mods TEXT'))
            print("  ✓ 添加 crafted_mods 字段")
        except Exception as e:
            print(f"  - crafted_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE events ADD COLUMN enchant_mods TEXT'))
            print("  ✓ 添加 enchant_mods 字段")
        except Exception as e:
            print(f"  - enchant_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE events ADD COLUMN mutated_mods TEXT'))
            print("  ✓ 添加 mutated_mods 字段")
        except Exception as e:
            print(f"  - mutated_mods 字段已存在或添加失败: {e}")
        
        try:
            db.session.execute(text('ALTER TABLE events ADD COLUMN veiled_mods TEXT'))
            print("  ✓ 添加 veiled_mods 字段")
        except Exception as e:
            print(f"  - veiled_mods 字段已存在或添加失败: {e}")
        
        db.session.commit()
        
        # 3. 处理过往数据
        print("\n3. 处理过往数据...")
        
        # 处理 item_snapshots
        print("  处理 item_snapshots...")
        rows = db.session.execute(text('SELECT id, item_data FROM item_snapshots WHERE implicit_mods IS NULL')).fetchall()
        print(f"  找到 {len(rows)} 条需要处理的记录")
        
        for row in rows:
            item_id, item_data_json = row
            try:
                item_data = json.loads(item_data_json) if isinstance(item_data_json, str) else item_data_json
                item_info = item_data.get('item', {})
                
                # 提取各种词缀
                explicit_mods = item_info.get('explicitMods', [])
                implicit_mods = item_info.get('implicitMods', [])
                fractured_mods = item_info.get('fracturedMods', [])
                crafted_mods = item_info.get('craftedMods', [])
                enchant_mods = item_info.get('enchantMods', [])
                mutated_mods = item_info.get('mutatedMods', [])
                veiled_mods = item_info.get('veiledMods', [])
                
                # 更新数据库
                db.session.execute(text('''
                    UPDATE item_snapshots 
                    SET implicit_mods = :implicit_mods,
                        fractured_mods = :fractured_mods,
                        crafted_mods = :crafted_mods,
                        enchant_mods = :enchant_mods,
                        mutated_mods = :mutated_mods,
                        veiled_mods = :veiled_mods
                    WHERE id = :id
                '''), {
                    'id': item_id,
                    'implicit_mods': json.dumps(implicit_mods),
                    'fractured_mods': json.dumps(fractured_mods),
                    'crafted_mods': json.dumps(crafted_mods),
                    'enchant_mods': json.dumps(enchant_mods),
                    'mutated_mods': json.dumps(mutated_mods),
                    'veiled_mods': json.dumps(veiled_mods)
                })
            except Exception as e:
                print(f"    处理记录 {item_id} 失败: {e}")
        
        db.session.commit()
        print(f"  ✓ item_snapshots 处理完成")
        
        # 处理 events
        print("  处理 events...")
        rows = db.session.execute(text('SELECT id, item_data FROM events WHERE implicit_mods IS NULL AND item_data IS NOT NULL')).fetchall()
        print(f"  找到 {len(rows)} 条需要处理的记录")
        
        for row in rows:
            event_id, item_data_json = row
            try:
                if not item_data_json:
                    continue
                item_data = json.loads(item_data_json) if isinstance(item_data_json, str) else item_data_json
                item_info = item_data.get('item', {})
                
                # 提取各种词缀
                explicit_mods = item_info.get('explicitMods', [])
                implicit_mods = item_info.get('implicitMods', [])
                fractured_mods = item_info.get('fracturedMods', [])
                crafted_mods = item_info.get('craftedMods', [])
                enchant_mods = item_info.get('enchantMods', [])
                mutated_mods = item_info.get('mutatedMods', [])
                veiled_mods = item_info.get('veiledMods', [])
                
                # 更新数据库
                db.session.execute(text('''
                    UPDATE events 
                    SET implicit_mods = :implicit_mods,
                        fractured_mods = :fractured_mods,
                        crafted_mods = :crafted_mods,
                        enchant_mods = :enchant_mods,
                        mutated_mods = :mutated_mods,
                        veiled_mods = :veiled_mods
                    WHERE id = :id
                '''), {
                    'id': event_id,
                    'implicit_mods': json.dumps(implicit_mods),
                    'fractured_mods': json.dumps(fractured_mods),
                    'crafted_mods': json.dumps(crafted_mods),
                    'enchant_mods': json.dumps(enchant_mods),
                    'mutated_mods': json.dumps(mutated_mods),
                    'veiled_mods': json.dumps(veiled_mods)
                })
            except Exception as e:
                print(f"    处理记录 {event_id} 失败: {e}")
        
        db.session.commit()
        print(f"  ✓ events 处理完成")
        
        print("\n✅ 迁移完成！")

if __name__ == '__main__':
    migrate()
