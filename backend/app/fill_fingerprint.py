import sys
import os
sys.path.insert(0, '/path/to/your/project')  # 需要根据实际路径修改

from app import create_app
from app.models import db, Event
import json
import hashlib

def compute_fingerprint_hash(item_info):
    """复现 scheduler 中的指纹计算逻辑"""
    name = item_info.get('name', '')
    base_type = item_info.get('typeLine', '')
    # 提取词缀的逻辑与 scheduler 中的 _collect_all_mods 一致
    mods = []
    for key in ['explicitMods', 'implicitMods', 'fracturedMods', 'craftedMods', 'enchantMods', 'veiledMods']:
        if key in item_info and item_info[key]:
            mods.extend(item_info[key])
    if 'notableProperties' in item_info:
        for np in item_info['notableProperties']:
            if 'name' in np:
                mods.append(np['name'])
    if 'properties' in item_info:
        for prop in item_info['properties']:
            name = prop.get('name', '')
            values = prop.get('values', [])
            if values and values[0] and values[0][0]:
                value_str = values[0][0]
                mods.append(f"{name}: {value_str}")
    if 'builtInSupport' in item_info and item_info['builtInSupport']:
        mods.append(f"内置辅助: {item_info['builtInSupport']}")
    mods_sorted = sorted(mods)
    fingerprint_str = f"{name}|{base_type}|{json.dumps(mods_sorted)}"
    return hashlib.sha256(fingerprint_str.encode()).hexdigest()

def main():
    app = create_app()  # 如果 create_app 函数名不同，请调整
    with app.app_context():
        events = Event.query.filter(Event.fingerprint_hash.is_(None)).all()
        print(f"找到 {len(events)} 条未填充指纹的事件")
        for event in events:
            # 从 event 表中已有的字段构建 item_info
            # 注意：event 表中没有完整的 item_data，只有 item_name、base_type、explicit_mods
            # 所以需要模拟一个 item_info 字典
            item_info = {
                'name': event.item_name,
                'typeLine': event.base_type,
                'explicitMods': json.loads(event.explicit_mods) if event.explicit_mods else []
            }
            # 其他词缀字段在 event 中没有存储，但指纹计算需要包含所有词缀。然而 event 中只存了 explicit_mods，
            # 但之前的扫描中，explicit_mods 已经包含了所有提取的词缀（见 scheduler 中 _collect_all_mods 返回的完整列表）。
            # 所以直接用 event.explicit_mods 即可。但为了保持一致，我们还是调用 compute_fingerprint_hash 传入 item_info，
            # 但注意 item_info 中其他字段可能缺失，导致指纹与实际扫描时计算的不一致。
            # 更好的做法是：直接用 event.explicit_mods（已经是 JSON 字符串）和 item_name、base_type 组合。
            # 这里简化：直接用 explicit_mods 作为 mods，因为 scheduler 中存入的就是 _collect_all_mods 的 JSON 数组。
            mods = json.loads(event.explicit_mods) if event.explicit_mods else []
            mods_sorted = sorted(mods)
            fingerprint_str = f"{event.item_name}|{event.base_type}|{json.dumps(mods_sorted)}"
            fp_hash = hashlib.sha256(fingerprint_str.encode()).hexdigest()
            event.fingerprint_hash = fp_hash
        db.session.commit()
        print("指纹填充完成")

if __name__ == '__main__':
    main()
