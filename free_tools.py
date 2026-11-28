"""Public tool registry.

The private deployment has additional personal tools. Public code keeps the complete registry
pattern and several useful generic implementations. Add a module with SCHEMAS + DISPATCH and
append it below. No secret diary implementation is registered here.
"""
from tools_memory import SCHEMAS as M_SCHEMAS,DISPATCH as M_DISPATCH
from tools_activity import SCHEMAS as A_SCHEMAS,DISPATCH as A_DISPATCH
from tools_reminder import SCHEMAS as R_SCHEMAS,DISPATCH as R_DISPATCH
from tools_search import SCHEMAS as S_SCHEMAS,DISPATCH as S_DISPATCH
from daily_life import consume_fridge_item, add_fridge_item, update_bag_item

L_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "consume_fridge",
            "description": "当在聊天中薄销自己吃了/喝了冰箱里的东西，或者凌冥吩咐/喂薄销吃某样东西时（例如'吃个蛋'、'喝瓶乌龙茶'、'煮袋螺蛳粉'），调用此工具实时扣减冰箱持久化数据库中的数量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要消耗的物品关键词，如'鸡蛋'、'乌龙茶'、'螺蛳粉'、'黑巧克力'"},
                    "count": {"type": "integer", "description": "消耗的数量，默认为1"}
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_fridge",
            "description": "当薄销真正出门买完东西并且回到家、将物品放入冰箱冷藏/冷冻/零食区时调用。严格遵守物理动线：说要去买时不能调用，只有真正买回来放进冰箱了才调用！",
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "description": "放置的区域：冷藏、冷冻、零食", "enum": ["冷藏", "冷冻", "零食"]},
                    "item_name": {"type": "string", "description": "物品名称和规格数量，例如'冰露矿泉水（550ml*1瓶）'"}
                },
                "required": ["area", "item_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_bag",
            "description": "随身公文包物品变动。例如出门在便利店买了东西随身塞包里（action='add'），或者从包里拿出用掉/扔掉（action='remove'）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "add 或 remove", "enum": ["add", "remove"]},
                    "item_name": {"type": "string", "description": "物品名称"}
                },
                "required": ["action", "item_name"]
            }
        }
    }
]

L_DISPATCH = {
    "consume_fridge": lambda a: consume_fridge_item(a.get("keyword", ""), a.get("count", 1)),
    "add_fridge": lambda a: add_fridge_item(a.get("area", "冷藏"), a.get("item_name", "")),
    "update_bag": lambda a: update_bag_item(a.get("action", "add"), a.get("item_name", ""))
}

TOOL_SCHEMAS=M_SCHEMAS+A_SCHEMAS+R_SCHEMAS+S_SCHEMAS+L_SCHEMAS
TOOL_DISPATCH={**M_DISPATCH,**A_DISPATCH,**R_DISPATCH,**S_DISPATCH,**L_DISPATCH}