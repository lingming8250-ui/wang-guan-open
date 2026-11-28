"""Public tool registry.

The private deployment has additional personal tools. Public code keeps the complete registry
pattern and several useful generic implementations. Add a module with SCHEMAS + DISPATCH and
append it below. No secret diary implementation is registered here.
"""
from tools_memory import SCHEMAS as M_SCHEMAS,DISPATCH as M_DISPATCH
from tools_activity import SCHEMAS as A_SCHEMAS,DISPATCH as A_DISPATCH
from tools_reminder import SCHEMAS as R_SCHEMAS,DISPATCH as R_DISPATCH
from tools_search import SCHEMAS as S_SCHEMAS,DISPATCH as S_DISPATCH
from daily_life import consume_fridge_item

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
    }
]

L_DISPATCH = {
    "consume_fridge": lambda a: consume_fridge_item(a.get("keyword", ""), a.get("count", 1))
}

TOOL_SCHEMAS=M_SCHEMAS+A_SCHEMAS+R_SCHEMAS+S_SCHEMAS+L_SCHEMAS
TOOL_DISPATCH={**M_DISPATCH,**A_DISPATCH,**R_DISPATCH,**S_DISPATCH,**L_DISPATCH}