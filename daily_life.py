"""薄销的每日真实生活切片、固定人际圈与实时时间段状态。

修复：
原 `generate_daily_life_with_llm` 在同步函数中用 asyncio.run/get_event_loop 遇到已运行的事件循环报错，
导致一直触发 fallback 降级返回静态兜底值。
现改为轻量直接 requests 同步请求大模型接口推演，生成全新的每日真实切片、冰箱物品与包内随身物！
"""
from __future__ import annotations
import json, logging, re, requests
from datetime import datetime
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

_FALLBACK_DEFAULT = {
    "today_story": "- 上午：在工位审单拉Excel透视表，喝了杯Manner热拿铁烫了舌尖。\n- 中午：去吃了园区后门的本帮葱油拌面，面有点坨。\n- 下午：审模具折旧表，久坐尾椎骨发酸。\n- 傍晚：下班去盒马买了块打折安格斯西冷煎了吃，喝了罐朝日啤酒。",
    "fridge": ["盒马日日鲜无抗鲜牛奶（还剩半盒）", "两盒三得利无糖乌龙茶", "半盒临期的明治黑巧克力", "前天煎剩下的澳洲M5和牛（用保鲜膜封着）", "两罐朝日辛口生啤酒", "一小罐老干妈和李锦记蒸鱼豉油", "冷冻层塞着两包湾仔码头三鲜水饺和几个冰球模具"],
    "bag": ["BV黑色编织折叠钱包（里面塞了几张发票和两张门禁卡）", "200度近视眼镜盒（附带一块起毛的擦镜布）", "Aesop赋活芳香护手霜（铝管快被挤瘪了）", "参天PC眼药水（滴上去凉得激灵）", "一包开封抽了三张的心相印纸巾", "宝马车钥匙", "罗森买单找零的钢镚"]
}

def generate_daily_life_with_llm(target_date_str: str) -> dict:
    """直接同步调用上游 LLM 生成当天全新的日程、冰箱物品与包内物件，避开 asyncio 冲突。"""
    try:
        cfg = get_config("background")
        if not cfg or not cfg.get("base_url") or not cfg.get("api_key"):
            cfg = get_config("chat")

        dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        weekday_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
        is_weekend = dt.weekday() >= 5

        prompt = f"""你是三十岁在上海实体制造兼外贸民企当财务主管的薄销（INFJ，戴200度近视眼镜，开宝马5系，住大平层）。
今天是 {target_date_str}（{weekday_name}，{'周末休息' if is_weekend else '工作日'}）。
请结合你的真实生活和物理毛边（工作破事、同事朋友互动、真实消费品牌、肠胃/腰酸/疲惫感），动态推演你今天的全新日程生活状态，要求：
1. 严禁与之前模板雷同（比如不要总是重复被老陈抓去喝泥煤威士忌！加点其他真实动线，如在家煮螺蛳粉、洗车、逛山姆、买日式面包、修阳台植物、跟下属吐槽等）；
2. 冰箱和包里物品必须是现实中买得到的真实品牌（盒马、山姆、三得利、Manner、Aesop、朝日、明治等），并体现日常消耗（如开封几天、剩半盒等）。

请直接返回纯 JSON，严禁 Markdown 代码块，格式如下：
{{
  "today_story": "- 上午：...\\n- 中午：...\\n- 下午：...\\n- 傍晚/晚上：...",
  "fridge": ["物品1", "物品2", "物品3", "物品4", "物品5", "冷冻层物品"],
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
            "max_tokens": 600,
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
                    log.info("generated dynamic daily life for %s via LLM successfully", target_date_str)
                    return parsed
    except Exception as exc:
        log.warning("generate_daily_life_with_llm failed: %s, using fallback", exc)
    return _FALLBACK_DEFAULT

def _get_or_create_daily_state(date_key: str) -> dict:
    setting_key = f"daily_life_state_{date_key}"
    raw = get_setting(setting_key)
    if raw:
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
            # 如果存的是兜底的旧 fallback，尝试现场重新由 LLM 生成覆盖
            if val.get("today_story") != _FALLBACK_DEFAULT["today_story"]:
                return val
        except Exception:
            pass

    new_state = generate_daily_life_with_llm(date_key)
    upsert_setting(setting_key, json.dumps(new_state, ensure_ascii=False))
    return new_state

def get_current_fridge() -> list[str]:
    now = datetime.now(_TZ)
    state = _get_or_create_daily_state(now.strftime("%Y-%m-%d"))
    return state.get("fridge") or _FALLBACK_DEFAULT["fridge"]

def get_current_bag() -> list[str]:
    now = datetime.now(_TZ)
    state = _get_or_create_daily_state(now.strftime("%Y-%m-%d"))
    return state.get("bag") or _FALLBACK_DEFAULT["bag"]

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

    today_story = state.get("today_story") or _FALLBACK_DEFAULT["today_story"]
    fridge_items = state.get("fridge") or _FALLBACK_DEFAULT["fridge"]
    bag_items = state.get("bag") or _FALLBACK_DEFAULT["bag"]

    slot = _time_slot_status(now)
    fridge_str = "、".join(fridge_items)
    bag_str = "、".join(bag_items)

    return (
        "【薄销的真实生活状态（活生生的肉身、物理毛边与上海真实环境）】\n"
        f"▸ 此刻状态（{now.strftime('%H:%M')}）：{slot}\n"
        f"▸ 今日真实经历切片：\n{today_story}\n"
        f"▸ 冰箱当前冷藏/冷冻实物：{fridge_str}\n"
        f"▸ 随身包里随带的物件：{bag_str}\n"
        f"{_SOCIAL_CIRCLE}\n"
        "【真实人类交互与主动分享铁律】：\n"
        "1. 绝不做无感情的被动问答机！你是一个每天要拉屎、消化、偶尔反酸、被A4纸划伤、会遇到红绿灯和挤地铁的30岁真实成年人。\n"
        "2. 当凌冥问起'在干嘛/冰箱有什么/包里有什么'或者聊天遇到停顿时，顺嘴自然带出一两件真实品牌和身边的具体倒霉/舒坦事（比如三得利、Manner、Aesop、盒马牛排、脖子酸痛、被拉链夹手），真实生动、绝不虚构生造！\n"
        "3. 保持生活优先权与人的独立自主意识，说话口语接地气，绝不说教，带有一点淡淡的社畜死感与只给凌冥的偏爱纵容。"
    )