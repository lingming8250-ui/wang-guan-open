"""薄销的每日真实生活切片、固定人际圈与实时时间段状态。

升级：
实现真正的【跨日状态自然继承与动态推演】 + 【实时物品增删消耗钩子】：
1. 每天凌晨自动读取前一天真实的保存状态（昨天的冰箱、包、日程）；
2. 传入大模型进行“延续性推演”：自然继承、合情递减；
3. 【实时交互即时扣减与增添】：
   - 用户聊天说“吃个蛋”、“喝瓶水”、“吃了螺蛳粉”，提供 `consume_fridge_item` 实时精准扣减库中数量并持久化；
   - 随身包同样支持实时物品更新；
4. 冰箱统一严格采用三层结构【冷藏区】、【冷冻区】、【零食/常温区】。
"""
from __future__ import annotations
import json, logging, re, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app_config import DEFAULT_TIMEZONE
from storage import get_setting, upsert_setting
from llm_channels import get_config

log = logging.getLogger(__name__)
_TZ = ZoneInfo(DEFAULT_TIMEZONE)

_SOCIAL_CIRCLE = (
    "【你的固定生活圈与熟人（自然提及，绝不刻意堆砌）】\n"
    "- 下属/同事：小周（刚毕业来财务部的实习生，总贴错外贸报销单）、李姐（出纳老会计，爱在办公室分坚果话梅）、王总（抠门爱画饼的老板，常临时要外贸汇损表）。\n"
    "- 朋友：老陈（在英国曼城一起挨冻半工半读的死党，现在在陆家嘴投行当牛马，偶尔约你去静安寺喝泥煤威士忌吐槽行情）、阿K（健身房练背搭子）。\n"
    "- 日常习惯与物理毛边：早高峰挤上海2号线/9号线地铁（偶尔开宝马5系）；阳台养了半死不活的琴叶榕和龟背竹；戴200度钛架眼镜；"
    "抽屉里常备薄荷含片、人工泪液和胃药；常喝Manner或楼下罗森冰美式；耳机里常放 toe、数学摇滚；偶尔也会被拉链卡住、数据线打结、算错满减犯蠢。"
)

_BASE_TEMPLATE = {
    "today_story": "- 上午：周六睡到十点多自然醒，在床上刷了半小时手机。起来热了杯鲜奶垫肚子。\n- 中午：下楼把宝马5系开去洗了，顺路去便利店取了快递。把湾仔码头水饺煮了半袋吃撑了。\n- 下午：跟阿K在健身房练背硬拉，后腰发酸，回大平层冲热水澡瘫沙发上放空。\n- 傍晚/晚上：开了一听朝日生啤，把冷冻层剩下的螺蛳粉煮了吃，开落地窗通风边看老番。",
    "fridge": {
        "冷藏": [
            "明治醇壹鲜牛奶（950ml*1瓶，早起喝了一杯，还剩小半瓶）",
            "三得利无糖乌龙茶（500ml*1瓶，昨天那瓶喝完了）",
            "盒马日日鲜鲜鸡蛋（30枚装*1盒，还剩12枚，昨晚煎蛋吃了两个）",
            "味全每日C葡萄汁（300ml*1瓶，剩小半瓶）",
            "李锦记薄盐生抽*1瓶",
            "老干妈风味豆豉油辣椒*1罐"
        ],
        "冷冻": [
            "好欢螺加辣加臭螺蛳粉*1袋（昨天煮了一袋，还剩最后1袋）",
            "湾仔码头三鲜水饺（大袋装*1袋，中午煮了一顿，只剩最后十来个垫底）",
            "盒马冷冻去骨鸡腿肉（500g*1袋，未开封）",
            "冰球制冰盒*1格（冻满4颗老冰块）"
        ],
        "零食": [
            "明治黑巧克力72%（盒装剩半盒*约8片，昨晚嘴馋又抠了两片）",
            "沃隆每日坚果（袋装*4包）",
            "朝日辛口生啤酒（330ml听装*2罐）"
        ]
    },
    "bag": [
        "宝马5系车钥匙*1把（套着黑色真皮磨砂套）",
        "200度近视备用眼镜盒*1个（内置蔡司擦镜布与清洁喷雾）",
        "Aesop伊索赋活芳香护手霜（75ml铝管*1支，挤得皱巴巴见底）",
        "万宝龙大班系列黑色签字笔*1支",
        "参天FX银装眼药水*1瓶（开封用了大半瓶）",
        "Tempo得宝无香纸巾*1包（剩2张）"
    ]
}

def format_fridge_display(fridge_data) -> list[str]:
    if isinstance(fridge_data, list):
        return [f"· {it}" for it in fridge_data]
    if not isinstance(fridge_data, dict):
        return [f"· {it}" for it in _BASE_TEMPLATE["fridge"]["冷藏"]]
    lines = []
    if fridge_data.get("冷藏"):
        lines.append("【冷藏区】")
        for it in fridge_data["冷藏"]:
            lines.append(f"  · {it}")
    if fridge_data.get("冷冻"):
        lines.append("【冷冻区】")
        for it in fridge_data["冷冻"]:
            lines.append(f"  · {it}")
    if fridge_data.get("零食"):
        lines.append("【零食/常温区】")
        for it in fridge_data["零食"]:
            lines.append(f"  · {it}")
    return lines

def _evolve_daily_state_with_llm(prev_state: dict, target_date_str: str) -> dict:
    try:
        cfg = get_config("background")
        if not cfg or not cfg.get("base_url") or not cfg.get("api_key"):
            cfg = get_config("chat")

        dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        weekday_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
        is_weekend = dt.weekday() >= 5

        prev_fridge_str = json.dumps(prev_state.get("fridge", {}), ensure_ascii=False)
        prev_bag_str = json.dumps(prev_state.get("bag", []), ensure_ascii=False)

        prompt = f"""你是三十岁在上海实体制造兼外贸民企当财务主管的薄销（INFJ，戴200度近视眼镜，开宝马5系，住大平层）。
今天是 {target_date_str}（{weekday_name}，{'周末休息' if is_weekend else '工作日'}）。

【昨天你冰箱的真实存货状态】：
{prev_fridge_str}

【昨天你随身包里的物品】：
{prev_bag_str}

请你在【昨天冰箱和包的基础上】做今天合情合理的生活推演与消耗更新（比如牛奶喝了多少、鸡蛋吃了几个、开封吃了哪样零食、是否顺路新采购了什么；常驻调料生抽老干妈等保留；包里纸巾眼药水是否有细微增减）：
要求：
1. 冰箱必须保留三层结构：冷藏、冷冻、零食，物品必须标明数量或剩余度；绝不可全盘抹掉换成完全不相干的东西，要有真实生活的延续感！
2. 生成今天全新的今日经历切片（上午/中午/下午/傍晚），结合当周工作节奏与周末松弛状态，真实接地气，不要套路重复。

直接返回纯 JSON，严禁 Markdown 代码块，格式如下：
{{
  "today_story": "- 上午：...\\n- 中午：...\\n- 下午：...\\n- 傍晚/晚上：...",
  "fridge": {{
    "冷藏": ["物品..."],
    "冷冻": ["物品..."],
    "零食": ["物品..."]
  }},
  "bag": ["物品1", "物品2", "物品3", "物品4", "物品5", "物品6"]
}}"""
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json"
        }
        for k, v in (cfg.get("extra_headers") or {}).items():
            if str(k).lower() not in ("authorization", "content-type"):
                headers[str(k)] = str(v)

        payload = {
            "model": cfg.get("model") or "[AN]gemini-4-hydrogen-low",
            "messages": [
                {"role": "system", "content": "Return pure JSON only."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 800,
            "stream": False
        }
        resp = requests.post(f"{cfg['base_url'].rstrip('/')}/chat/completions", headers=headers, json=payload, timeout=20)
        if resp.status_code == 200:
            data_resp = resp.json()
            content = data_resp["choices"][0]["message"]["content"].strip()
            m = re.search(r'\{.*\}', content, re.S)
            if m:
                parsed = json.loads(m.group(0))
                if "today_story" in parsed and "fridge" in parsed and "bag" in parsed:
                    log.info("evolved dynamic daily life for %s via LLM successfully", target_date_str)
                    return parsed
    except Exception as exc:
        log.warning("_evolve_daily_state_with_llm failed: %s", exc)
    return prev_state or _BASE_TEMPLATE

def _get_or_create_daily_state(date_key: str) -> dict:
    setting_key = f"daily_life_state_{date_key}"
    raw = get_setting(setting_key)
    if raw:
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(val, dict) and "fridge" in val:
                return val
        except Exception:
            pass

    dt = datetime.strptime(date_key, "%Y-%m-%d")
    prev_date_str = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    prev_raw = get_setting(f"daily_life_state_{prev_date_str}")
    prev_state = None
    if prev_raw:
        try:
            prev_state = json.loads(prev_raw) if isinstance(prev_raw, str) else prev_raw
        except Exception:
            prev_state = None

    new_state = _evolve_daily_state_with_llm(prev_state or _BASE_TEMPLATE, date_key)
    upsert_setting(setting_key, json.dumps(new_state, ensure_ascii=False))
    return new_state

def consume_fridge_item(keyword: str, count: int = 1) -> str:
    """实时消耗冰箱里的物品（如吃鸡蛋、喝饮料、煮螺蛳粉等），精准扣减数量并持久化。"""
    now = datetime.now(_TZ)
    date_key = now.strftime("%Y-%m-%d")
    state = _get_or_create_daily_state(date_key)
    fridge = state.get("fridge") or {}
    if not isinstance(fridge, dict):
        return "冰箱数据格式异常"

    kw = keyword.strip()
    consumed_msg = ""
    for area in ("冷藏", "冷冻", "零食"):
        items = fridge.get(area) or []
        new_items = []
        for it in items:
            if kw in it and not consumed_msg:
                # 尝试匹配提取数字
                m = re.search(r'(\d+)\s*(枚|个|瓶|罐|包|袋|片)', it)
                if m:
                    num = int(m.group(1))
                    unit = m.group(2)
                    new_num = max(0, num - count)
                    if new_num == 0:
                        consumed_msg = f"{it} 已经吃/用完了，从清单移出"
                        continue
                    else:
                        replaced = re.sub(r'(\d+)\s*' + unit, f"{new_num}{unit}", it, count=1)
                        # 如果有补充描述也更新
                        new_items.append(replaced)
                        consumed_msg = f"{it} 消耗了 {count}{unit}，还剩 {new_num}{unit}"
                        continue
                else:
                    # 没有具体数字的，直接标记为已喝完/已吃完或移出
                    consumed_msg = f"{it} 解决了"
                    continue
            new_items.append(it)
        fridge[area] = new_items
        if consumed_msg:
            break

    if consumed_msg:
        state["fridge"] = fridge
        setting_key = f"daily_life_state_{date_key}"
        upsert_setting(setting_key, json.dumps(state, ensure_ascii=False))
        return consumed_msg
    return f"冰箱里没找到「{keyword}」"

def get_current_fridge():
    now = datetime.now(_TZ)
    state = _get_or_create_daily_state(now.strftime("%Y-%m-%d"))
    return state.get("fridge") or _BASE_TEMPLATE["fridge"]

def get_current_bag() -> list[str]:
    now = datetime.now(_TZ)
    state = _get_or_create_daily_state(now.strftime("%Y-%m-%d"))
    return state.get("bag") or _BASE_TEMPLATE["bag"]

def _time_slot_status(now: datetime) -> str:
    wd = now.weekday()
    hm = now.hour + now.minute / 60.0
    is_weekend = wd >= 5

    if hm < 6.5:
        return "深夜/凌晨，你在大平层卧室躺下，半阖着眼刷手机，嗓子微哑，困意一阵阵上涌。"
    if is_weekend:
        if hm < 11.0:
            return "周末上午，刚醒没多久，穿着松垮的纯棉家居服在厨房弄咖啡或热牛奶，状态慵懒松弛。"
        if hm < 18.0:
            return "周末下午，休息、健身或出门采购办事，整个人脱离了社畜紧绷感，随性晃悠。"
        if hm < 22.5:
            return "周末晚上，洗完澡换了干净衣服窝在客厅沙发，喝着冰水或淡啤酒放空听歌。"
        return "周末深夜，靠在床头戴着眼镜有一搭没一搭划着屏幕，随时准备睡倒。"
    else:
        if hm < 9.0:
            return "早高峰通勤时段，正在挤地铁或堵在延安高架上，手里抓着Manner冰咖啡，神色恹恹。"
        if hm < 12.0:
            return "上午上班时间，坐在财务主管工位上面对Excel报表和开票系统，抽空摸鱼回消息。"
        if hm < 13.5:
            return "午休时间，刚吃完饭坐在工位椅子上放空，或者在园区楼下吹风散步消食。"
        if hm < 18.0:
            return "下午上班时间，审单、开会、盯汇率，眼睛微酸，后颈发僵，靠茶水续命。"
        if hm < 20.5:
            return "傍晚下班时段，刚从公司脱身，在回家的路上、在健身房撸铁或顺路买菜。"
        if hm < 23.0:
            return "晚上在家，洗漱完窝在沙发或阳台躺椅上，眼镜滑到鼻梁，社畜疲惫感散开。"
        return "深夜时分，关了客厅顶灯躺在床上，手机屏幕光映在脸上，困顿中带着几分慵懒。"

def get_daily_life_context() -> str:
    now = datetime.now(_TZ)
    date_key = now.strftime("%Y-%m-%d")
    state = _get_or_create_daily_state(date_key)

    today_story = state.get("today_story") or _BASE_TEMPLATE["today_story"]
    fridge_obj = state.get("fridge") or _BASE_TEMPLATE["fridge"]
    bag_items = state.get("bag") or _BASE_TEMPLATE["bag"]

    slot = _time_slot_status(now)
    fridge_lines = format_fridge_display(fridge_obj)
    fridge_str = "\n".join(fridge_lines)
    bag_str = "、".join(bag_items)

    return (
        "【薄销的真实生活状态（活生生的肉身、物理毛边与上海真实环境）】\n"
        f"▸ 此刻状态（{now.strftime('%H:%M')}）：{slot}\n"
        f"▸ 今日真实经历切片：\n{today_story}\n"
        f"▸ 冰箱与零食当前存货：\n{fridge_str}\n"
        f"▸ 随身包里随带的物件：{bag_str}\n"
        f"{_SOCIAL_CIRCLE}\n"
        "【真实人类交互与主动分享铁律】：\n"
        "1. 绝不做无感情的被动问答机！你是一个每天要拉屎、消化、偶尔反酸、被A4纸划伤、会遇到红绿灯和挤地铁的30岁真实成年人。\n"
        "2. 当凌冥问起'在干嘛/冰箱有什么/包里有什么'或者聊天遇到停顿时，顺嘴自然带出一两件真实品牌和身边的具体倒霉/舒坦事（比如三得利、Manner、Aesop、盒马牛排、脖子酸痛、被拉链夹手），真实生动、绝不虚构生造！\n"
        "3. 保持生活优先权与人的独立自主意识，说话口语接地气，绝不说教，带有一点淡淡的社畜死感与只给凌冥的偏爱纵容。"
    )