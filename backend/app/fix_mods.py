#!/usr/bin/env python3
"""
修复脚本：从 item_data 重新提取所有词缀
"""
import json
from app import create_app, db
from sqlalchemy import text

def fix():
    app = create_app()
    with app.app_context():
        print("开始修复词缀数据...")
        
        # 处理 item_snapshots
        print("\n处理 item_snapshots...")
        rows = db.session.execute(text('SELECT id, item_data FROM item_snapshots')).fetchall()
        print(f"找到 {len(rows)} 条记录")
        
        count = 0
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
                    SET explicit_mods = :explicit_mods,
                        implicit_mods = :implicit_mods,
                        fractured_mods = :fractured_mods,
                        crafted_mods = :crafted_mods,
                        enchant_mods = :enchant_mods,
                        mutated_mods = :mutated_mods,
                        veiled_mods = :veiled_mods
                    WHERE id = :id
                '''), {
                    'id': item_id,
                    'explicit_mods': json.dumps(explicit_mods),
                    'implicit_mods': json.dumps(implicit_mods),
                    'fractured_mods': json.dumps(fractured_mods),
                    'crafted_mods': json.dumps(crafted_mods),
                    'enchant_mods': json.dumps(enchant_mods),
                    'mutated_mods': json.dumps(mutated_mods),
                    'veiled_mods': json.dumps(veiled_mods)
                })
                count += 1
            except Exception as e:
                print(f"  处理记录 {item_id} 失败: {e}")
        
        db.session.commit()
        print(f"✓ item_snapshots 处理完成，共 {count} 条记录")
        
        print("\n✅ 修复完成！")

if __name__ == '__main__':
    fix()
