# -*- coding: utf-8 -*-
"""将 SVG 渲染成 PNG，再依据最终画面调用视觉评审模型。"""

import http.client
import base64
import json
import math
import socket
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import httpcore
from . import stream_transport
from .request_options import (
    anthropic_message_params, api_timeout_seconds, endpoint_url,
    openai_chat_params, responses_params,
)

# 评分只输出一小段 JSON，给足空间但不占用生成任务的完整输出预算。
REVIEW_MAX_OUTPUT_TOKENS = 8192
MAX_RESPONSE_BYTES = 1024 * 1024
# 评分不需要深度推理，默认用低档位，尽量压低额外 Token 与等待时间。
# 注意：部分模型（如 gpt-6.1-terra）不接受 minimal，评分页可单独选择思考等级。
DEFAULT_REVIEW_REASONING_EFFORT = "low"
DEFAULT_REVIEW_RETRY_COUNT = 2
RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504, 529}
FAILOVER_HTTP_STATUSES = {502, 503, 504, 529}

# 仅用于识别旧数据库里已保存的内置规则。
LEGACY_REVIEW_PROMPT = """你是严格的 SVG 画面评审。你会收到一份由 AI 生成的 SVG 源码，主题是“拟人化鹈鹕骑复古自行车沿海岸公路”。你无法直接看到渲染图，请根据画布尺寸、坐标、路径、颜色和图层关系推断真实画面。

请按以下维度打分（总分 100）：
- 画面结构 40 分：主体完整、比例协调、位置合理；没有明显错位、悬空、穿模、超出画布或部件缺失。
- 细节完成度 30 分：提示词要求的关键元素是否齐全（鹈鹕的长嘴与喉囊、帽子、围巾、自行车车架/车轮/踏板、海岸公路背景等），细节是否可用。
- 美观度 20 分：配色协调、层次清晰、构图舒服，像一幅完整插画而不是符号拼贴。
- 代码可渲染性 10 分：标签闭合合法、坐标数值有效、viewBox 合理，不依赖外部资源。

评分要拉开差距：明显残缺或构图崩坏低于 50 分；能看但粗糙 50-69 分；完整但普通 70-84 分；精细协调 85-94 分；接近专业插画 95 分以上。不要因为代码篇幅长就给高分。

只输出一个 JSON 对象，不要输出 Markdown 代码块或其他文字：
{"score": 0 到 100 的数字, "problems": ["最多 5 条具体问题"], "comment": "40 字以内的总评"}"""


REVIEW_RUBRIC_VERSION = "8"
DIMENSION_MAX = {
    "bicycle": 24,
    "pelican": 24,
    "road_and_grass": 14,
    "water": 12,
    "lighthouse": 8,
    "sky_and_sun": 8,
    "overall": 10,
}

V4_REVIEW_PROMPT = """你是拟人化鹈鹕骑复古自行车海岸插画的画面评审员。你会收到 SVG 在实际显示时的 PNG 画面，只根据图像中能看见的内容评分。不要假设画面外存在能补足缺失关系的线条，也不要推测图中看不清的连接。

先分别核实四项骑行关系，每项写一句可定位的证据：saddle＝臀部受鞍座承托，沿座管向上找鞍座；handlebar＝翼尖接触握把，沿前叉和把立找握把；pedals＝沿曲柄中心向相对两侧分别追踪踏板，再核对两只蹼足底缘；bicycle＝车架、前叉及把立与对应轮轴连接可信。可见接触或合理遮挡下仍能推断成立为 ok；叠放使关系难以辨认是 unclear；有画面证据表明确实断开、错接、悬空或穿插才是 broken。判 broken 时指出两个部件及其实际位置或明确的缺件。核对填充和描边覆盖后的接触，别把链罩当鞍座、把车架当踏板；找到远侧蹼足后也要追踪对应曲柄是否有独立的踏面。两脚挤在同一踏板、翼尖只够到前叉均是实际错误。

先检查画面的主体、姿态、构图、背景纵深、颜色以及实际可见的线条和细节。对四项给整数分：
- riding_and_form（35）鹈鹕的可识别性、比例和骑乘造型。16 分以下只适用于主体残缺或姿态严重错乱，18–24 分表示主题清楚但造型生硬、骑乘支撑差，25–29 分表示整体可信但有局部不足，30–35 分要求比例、外形和姿态成熟。
- composition_and_depth（25）10–14 分表示构图凌乱、环境空缺或背景主要是彼此无关的简单色带；15–19 分表示主体清楚且有基本层次；20–23 分表示主次、留白和空间层次组织良好；24–25 分表示构图与空间极其成熟。规整的侧视画面可用合理比例、叠放、地面和丰富远景建立空间，不需要透视消失点。
- finish（25）10–14 分表示主体轮廓和部件明显粗糙或残缺；15–19 分表示造型可辨但局部细节和收边简单；20–23 分要求轮廓清晰、关键部件刻画完整，局部细节经过整理；24–25 分要求近看精致、细节丰富而不拥挤。反复的辐条、草叶或边线不等同于精修。简洁的平涂插画可以凭成熟的轮廓和造型取得高分。
- color_and_readability（15）色彩与可读性。典型 8 分是基本协调但主次不明，11 分是配色统一且轮廓清楚，13 分是明暗、对比和色温有效组织焦点及空间，15 分是表现尤其成熟。

轻微遮挡、难以看清的接触、风格化画法和可能的透视歧义都不是确定断接。如果证据不足，选 unclear，不要猜错并施以重罚。broken 只表示图像清楚显示具体部件脱开、缺失、悬空或错接，写出两侧部件和可辨认的位置。鞍座遮挡时检查鸟臀是否处于其合理承托位置；握把被翅膀盖住但姿态连贯时可评 unclear；脚蹼被曲柄挡住时不要误认成缺脚踏。分别观察两只脚是否自然落向自行车两侧的踏板。

四个骑乘项目的结构缺陷只调整 riding_and_form。两项真实明显的断接最多把该项降至 20 分，不得把构图、背景、配色和绘制质量一起封顶到低分。三项为 unclear 但没有看见实际断开时，该项最高 29 分。其他分项必须依据画面单独判断。仅当主体几乎无法辨认、严重缺失或整体构图崩坏时 severe_presentation 为 true。用 problems 按严重程度写最多 5 条可见问题，comment 用 40 字以内描述整体观感。动画只评价截图可观察的画面质量。

总分由服务端根据四项 dimensions 求和。只输出一个 JSON 对象：
{"riding":{"saddle":{"status":"ok|unclear|broken","evidence":"证据"},"handlebar":{"status":"ok|unclear|broken","evidence":"证据"},"pedals":{"status":"ok|unclear|broken","evidence":"证据"},"bicycle":{"status":"ok|unclear|broken","evidence":"证据"}},"visual_evidence":{"composition_and_depth":"证据","finish":"证据"},"dimensions":{"riding_and_form":0,"composition_and_depth":0,"finish":0,"color_and_readability":0},"severe_presentation":false,"problems":[],"comment":"总评"}"""

V5_REVIEW_PROMPT = V4_REVIEW_PROMPT.replace(
    "总分由服务端根据四项 dimensions 求和。",
    """用下列画面标准复核高分项：
- 构图与纵深：平直天空、海面、道路色带和均匀空白只能说明有基本布局。环境以这些色带为主、缺少前中后景的叠放、尺度变化或视觉引导，给 10–14 分，即使主体居中、轮廓清楚。20 分以上需写出具体的景物、它们之间的空间关系以及怎样引导视线。平涂作品也可通过这些关系取得高分。
- 细节完成度：粗描边、基本几何部件和统一填色约 11–15 分，即使边缘干净、能辨认各部件。16–19 分要求主要轮廓或若干部件经过加工。20 分以上需写出至少两处不同部件的具体造型、收边或材质细节，不能仅列举有什么物件。造型成熟的简洁线条、负形和轮廓取舍也算有效细节。
- 色彩和可读性：清楚协调的平涂配色通常是 10–12 分。13 分以上需有可见的层级、对比或色温关系组织画面焦点，颜色相配本身不足以进入高分区间。

总分由服务端根据四项 dimensions 求和。""",
)

V6_REVIEW_PROMPT = """你负责评审“拟人化鹈鹕沿海岸公路骑复古自行车”的 SVG 成品。只看随附的 PNG。先核对图中具体的形状、接触和遮挡，再判断画面的完成度。图像只提供一帧，不推测动画、源码、生成模型或历史成绩。

【骑乘关系】
分别检查 saddle（臀部与鞍座）、handlebar（翼尖与握把）、pedals（两只蹼足与各自踏板）、bicycle（车架、前叉与轮轴）。每项给出状态和一句能在画面上定位的证据。
- 鞍座：从曲柄往上沿座管追到承托面。后轮上方的旅行包、行李架或棕色块不能证明存在鞍座。把手：从前轮轴沿前叉和把立追到把横及握把，检查翼尖是否落在实际握把上。
- 踏板：以中轴为圆心，先找曲柄方向及末端的短踏面，再看每只蹼足的底缘是否压在同一个踏面上。证据要分别写出两块踏面的相对方位，以及脚底与踏面的方位是否一致。特别检查一种常见错接：曲柄斜指左下和右上，两只脚却都沿水平方向搭在车架长管上，此时即使横向分居两侧，也属于 broken。若只看到了一个独立踏面，另一个被车架或链条挡住，则为 unclear。只凭两只脚在中轴两侧，不给 ok。
- ok：连接可见，或可见轮廓和接续方向足以解释合理遮挡。unclear：配合关系被重叠或线条混在一起，无法沿部件确认。broken：有清楚的空隙、错接、穿插、错位或可见部件缺失。用证据描述实际位置，不能凭看不清就认定断裂。辨认出鹈鹕和自行车只说明题材成立。

【绘制程度】
独立判断主体工艺 craft.subject 和场景工艺 craft.scene，各选 basic、developed、refined。证据要描述画面上的处理方式、薄弱处，以及对整体观感的影响。只列举部件名称、颜色、元素数量或写“边缘干净、层次清晰”不足以支撑 refined。
- 主体 basic：用少量色块、同样的粗描边或重复线条概括结构，多处轮廓像拼贴或关键部件互相挤压。典型信号有大块实心车轮、粗描边的卵形躯干与独立色块喉囊、只靠平行线表示羽翼、脚蹼压在车架上。developed：轮廓有加工，鸟或车仍主要靠基础几何、平行装饰线或堆叠色块说明。配件做得细也不能代替主形刻画。refined 要同时满足两点。其一，鸟的主要结构有立体或层叠关系，例如翼根连入肩部，羽片随翼缘分层，头颈与喙囊衔接有过渡。光滑的大翼轮廓加两三条内部弧线，或喉囊加一道高光，至多是局部加工。其二，车的功能部位能从轮廓读出装配关系，例如成对踏面可区分、前叉与把手的接合有独立边界、轮毂和传动各层明确。行李包细节、辐条数量、均匀描边和路面纹理不能独立支撑主体精修。用证据分别解释鸟和车达到的水平。
- 场景 basic：天空、海面、丘陵、道路各占一条色带，浪线、轮廓相近的山丘、草、灯塔和太阳只增加了物件。即使色带弯曲或有渐变，若缺少有意义的叠放、尺度变化、道路推进或前景框定，仍属于基础场景。developed：能找到一组有用的空间组织，例如岸岬遮挡水域、弯道由近渐远、路边植被随距离变小。若海天、岸线、道路整体仍像平行横带，其余景物主要沿色带排开，就选 developed 或 basic。refined：至少两组相互独立、可定位的空间设计在组织远近和视线，例如逐级变化的山岸叠放，加上由近向远收窄的路缘或分重量的前景。给出每组具体景物及如何形成空间。远处物件较小、近处有一道投影、道路画了虚线，单独只能支持基本层次。侧视、留白和简洁平涂也可以靠具体空间关系达到 refined。

【计分】
- riding_and_form 满分 35。18–24 是主体可辨但姿态僵硬或多个接触读不出来，25–29 是整体连贯且有局部瑕疵，30–35 要求造型成熟、关键骑乘关系清晰。题材齐全只达到起评条件。
- composition_and_depth 满分 25。场景 basic 通常 10–14，developed 15–19，refined 20–25。
- finish 满分 25。主体 basic 通常 10–15，developed 16–20，refined 21–25。
- color_and_readability 满分 15。配色统一、轮廓可辨一般为 10–12 分，13–15 要指出具体的色温、明暗或对比如何组织焦点。

以完成度和骑乘可信度共同定档。明显残缺或近乎不可辨识低于 50，简陋但主题可辨 50–69，70–79 要有成形的主体和场景设计，80–89 要有成熟的造型或空间组织，90+ 要求骑乘、构图和工艺都经得起放大检查。
多个明确断接、三处主要骑乘接触均难辨、或主体和场景均为基础绘制，都属于 69 分及以下。主体和场景都未精修且有关键骑乘接触难辨或错接，或有一项是基础绘制且多处关键接触难辨或错接，也属于 69 分及以下。高完成度画面的单处合理遮挡只按局部问题处理。只有主体几乎无法辨认、严重缺失或构图崩坏才标为 severe_presentation。分项和总分上限由服务端计算，problems 按严重程度列最多 5 个可见问题，comment 用 40 字以内写清主要扣分原因。

只输出一个 JSON 对象：
{"riding":{"saddle":{"status":"ok|unclear|broken","evidence":"证据"},"handlebar":{"status":"ok|unclear|broken","evidence":"证据"},"pedals":{"status":"ok|unclear|broken","evidence":"证据"},"bicycle":{"status":"ok|unclear|broken","evidence":"证据"}},"craft":{"subject":"basic|developed|refined","scene":"basic|developed|refined"},"visual_evidence":{"composition_and_depth":"场景工艺证据","finish":"主体工艺证据"},"dimensions":{"riding_and_form":0,"composition_and_depth":0,"finish":0,"color_and_readability":0},"severe_presentation":false,"problems":[],"comment":"总评"}"""

DEFAULT_REVIEW_PROMPT = """你是插画评审。只看随附的一张 SVG 成品 PNG，评价鹈鹕沿海岸公路骑复古自行车的画面有多精美。逐处观察画得清不清楚、形状和线条有没有设计、局部细节是否融入整幅画。只用图中看得到的证据。不要推测源码、动画、生成渠道或历史分数。

逐项给整数分并写一句具体证据。证据说清楚哪些轮廓、细节或层次处理得好，或者哪里粗糙。能认出物件说明画到了，精美还需要看构造、收边、配色和局部细节。平涂和简洁风格也可画得细致。

- bicycle（24）：看车架线条与比例，轮胎、轮圈、轮毂、辐条、链条传动、挡泥板、车把、车座等有没有画清楚。几个实心圆加粗线拼成车通常约 8–13；轮组与传动分层、线条收边清楚，通常 17–20；能具体看到部件装配、线宽和材质处理等精修，21–24。
- pelican（24）：看头颈、长喙和喉囊的比例与衔接，眼睛表情、帽子、围巾和羽翼有没有刻画，腿和蹼足轮廓是否自然。几块简单轮廓拼接、羽翼只添重复线通常 10–14；主体造型连贯且局部有细致处理，通常 17–20；体积、羽片、服饰和线条都有成熟刻画才给 21–24。姿态或接触有清楚可见的问题时，只在对应主体部位扣分，一处问题只计一次。合理遮挡属于正常绘制。
- road_and_grass（14）：看路面、路缘、车道线是否清晰，路形、车影或路面纹理是否有组织。草叶的形状、疏密、方向和分布是否经过处理。平带公路加几丛重复草线通常 5–8；路和草各有清楚的形状和布置通常 9–11；两者都经过细致处理并融入构图才给 12–14。
- water（12）：看水和山岸、陆地是否分得开，河湾或海岸线是否清楚，水纹、反光、岸边遮叠和帆船是否细致且有变化。均匀色块加几道波线通常 4–7；水域与岸线有清楚层次、局部有变化通常 8–10；水纹或反光与水域形状、远近细节自然配合才给 11–12。
- lighthouse（8）：看小塔的轮廓、塔顶、塔身、条纹、窗或灯室是否画得精巧，尺度、位置与岸边环境是否协调；浮标不能算小塔。只有条纹矩形或图标式塔形通常 2–4；塔身与塔顶清楚、可见局部细节通常 5–6；近看结构和收边精巧才给 7–8。
- sky_and_sun（8）：看太阳的轮廓、光晕或色彩过渡、云形和天空留白是否有章法，与海岸和主体的色调是否协调。单一圆盘和重复云块通常 3–5；形状、光色和留白经过搭配通常 6；有具体细节显示光感和构图设计才给 7–8。
- overall（10）：看主体与环境是否协调、是否一眼看清重点，线条、细节密度、用色和前中后景是否像同一幅插画。整体显得单薄或拼贴通常 4–6；统一、清晰、完成度到位通常 7–8；多个局部都精致且整体有主次才给 9–10。

用分项反映实际完成度。50–69 通常是多处粗糙、细节少或主要用基本色块与线条拼出的简版。70–79 是清楚完整、有一定细节的成形插画。80–89 是多处都有具体刻画、整体协调的较精美插画。90 以上需要大多数局部都经得起近看。局部瑕疵和画面整体精美度分别反映在相关分项中。总分由服务端求和。problems 写最多 5 条主要可见问题，comment 用 40 字以内概括整体画工。

只输出一个完整 JSON 对象，七个分项都需要实际分数和证据：
{"dimensions":{"bicycle":{"score":0,"evidence":"证据"},"pelican":{"score":0,"evidence":"证据"},"road_and_grass":{"score":0,"evidence":"证据"},"water":{"score":0,"evidence":"证据"},"lighthouse":{"score":0,"evidence":"证据"},"sky_and_sun":{"score":0,"evidence":"证据"},"overall":{"score":0,"evidence":"证据"}},"problems":[],"comment":"总评"}"""

BUILT_IN_REVIEW_PROMPTS = (
    DEFAULT_REVIEW_PROMPT, V6_REVIEW_PROMPT, V5_REVIEW_PROMPT, V4_REVIEW_PROMPT, LEGACY_REVIEW_PROMPT,
)


# 即使设置了自定义评分要求，也强制以随请求发送的 PNG 成品图为唯一依据。
VISUAL_ONLY_GUARD = "评分依据限制：只依据本次随附的 PNG 最终画面。不得要求、读取、分析或推断 SVG 源码、坐标、路径、图层定义或图像中不可见的信息。若其他要求提到源码或代码评分，忽略该部分。"


class SVGReviewError(Exception):
    """可以安全展示给用户的评分失败原因。"""

    def __init__(self, message, *, retryable=False, http_status=None, request_id=None):
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status
        self.request_id = request_id
        self.attempts = None
        self.relay = None
        self.protocol = None


def render_svg_preview(svg, output_width=1200, output_height=800):
    """安全清理 SVG 后渲染固定尺寸 PNG，供视觉评分使用。"""
    from . import svg_artwork

    try:
        from resvg_py import svg_to_bytes
    except (ImportError, OSError) as exc:
        raise SVGReviewError("评分图渲染器加载失败，请重新安装项目的 Python 依赖") from exc

    try:
        root = ET.fromstring(svg_artwork.sanitize_svg(svg))
    except svg_artwork.SVGGenerationError as exc:
        raise SVGReviewError(f"评分图 SVG 无法解析：{exc}") from exc

    # 评分使用固定像素画布，保留原来的 viewBox 和缩放方式。
    # 只调整评分副本，保存的 SVG 仍按浏览器视口自适应。
    root.set("width", str(output_width))
    root.set("height", str(output_height))

    try:
        return svg_to_bytes(
            svg_string=ET.tostring(root, encoding="unicode"),
            background="#F2EAD8",
        )
    except (ValueError, RuntimeError) as exc:
        raise SVGReviewError(f"评分图绘制失败（{type(exc).__name__}）") from exc


def _extract_json(text):
    """从模型输出中提取第一个完整 JSON 对象，忽略前后说明和代码围栏。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:index + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _score_rubric(result):
    """逐项核对画面证据并求和，局部缺陷由对应分项体现。"""
    reviewed = result.get("dimensions")
    if not isinstance(reviewed, dict):
        raise SVGReviewError("评分模型没有返回有效的分项分数")
    dimensions = {}
    visual_evidence = {}
    for key, maximum in DIMENSION_MAX.items():
        item = reviewed.get(key)
        if not isinstance(item, dict):
            raise SVGReviewError(f"评分模型缺少画面分项：{key}")
        score = item.get("score")
        evidence = item.get("evidence")
        if type(score) is not int or not 0 <= score <= maximum:
            raise SVGReviewError(f"评分模型没有返回有效的分项分数：{key}")
        if not isinstance(evidence, str) or not evidence.strip():
            raise SVGReviewError(f"评分模型没有返回分项的画面证据：{key}")
        dimensions[key] = score
        visual_evidence[key] = evidence.strip()[:240]
    return {
        "score": float(sum(dimensions.values())),
        "rubric_version": REVIEW_RUBRIC_VERSION,
        "dimensions": dimensions,
        "visual_evidence": visual_evidence,
    }


def _normalize(result, use_default_rubric=False):
    """把评审输出收敛成前端可用的分数、问题清单和评语。"""
    if not isinstance(result, dict):
        raise SVGReviewError("评分模型没有返回 JSON 结果")
    if use_default_rubric:
        score_detail = _score_rubric(result)
    else:
        try:
            score = float(result.get("score"))
        except (TypeError, ValueError):
            raise SVGReviewError("评分模型没有返回有效分数") from None
        if not math.isfinite(score):
            raise SVGReviewError("评分模型没有返回有效分数")
        score_detail = {"score": round(max(0.0, min(100.0, score)), 1)}
    problems = result.get("problems")
    if isinstance(problems, str):
        problems = [problems]
    if not isinstance(problems, list):
        problems = []
    problems = [str(item).strip()[:200] for item in problems
                if str(item).strip()][:5]
    comment = str(result.get("comment") or "").strip()[:300]
    return {**score_detail, "problems": problems, "comment": comment}


def _build_request(relay, model, review_prompt, reasoning_effort, preview_image):
    """按渠道协议构造只含评分提示词和 PNG 成品图的请求。"""
    protocol = relay.get("protocol", "openai")
    user_content = review_prompt.strip()
    if not isinstance(preview_image, (bytes, bytearray)) or not preview_image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SVGReviewError("评分必须提供已渲染的成品图")
    image_base64 = base64.b64encode(preview_image).decode("ascii")
    image_url = f"data:image/png;base64,{image_base64}"
    if protocol == "anthropic":
        params = anthropic_message_params(model, reasoning_effort)
        body = {
            "model": model,
            "max_tokens": REVIEW_MAX_OUTPUT_TOKENS,
            **({"temperature": 0} if params["temperature"] is not None else {}),
            **({"thinking": params["thinking"]} if params["thinking"] else {}),
            **({"output_config": params["output_config"]} if params["output_config"] else {}),
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": user_content},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": image_base64,
                }},
            ]}],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": relay.get("apiKey", ""),
            "anthropic-version": "2023-06-01",
        }
        return body, headers
    if protocol == "openai-responses":
        body = {
            "model": model,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": user_content},
                {"type": "input_image", "image_url": image_url},
            ]}],
            "max_output_tokens": REVIEW_MAX_OUTPUT_TOKENS,
            **responses_params(model, reasoning_effort),
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {relay.get('apiKey', '')}",
        }
        return body, headers
    params = openai_chat_params(model, reasoning_effort)
    body = {
        "model": model,
        params["max_tokens_field"]: REVIEW_MAX_OUTPUT_TOKENS,
        **({"temperature": params["temperature"]} if params["temperature"] is not None else {}),
        **({"reasoning_effort": params["reasoning_effort"]} if params["reasoning_effort"] else {}),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": user_content},
            {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
        ]}],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {relay.get('apiKey', '')}",
    }
    return body, headers


def _response_text(payload, protocol):
    """从三种协议的同步响应中取出评审正文。"""
    if protocol == "anthropic":
        return "\n".join(
            block.get("text", "") for block in payload.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if protocol == "openai-responses":
        if payload.get("status") == "incomplete":
            raise SVGReviewError("评分模型输出未完成，请重试或换一个评分模型")
        text = payload.get("output_text")
        if isinstance(text, str) and text.strip():
            return text
        parts = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    parts.append(content.get("text", ""))
        return "".join(parts)
    choices = payload.get("choices") or []
    if not choices:
        return ""
    if choices[0].get("finish_reason") in {"length", "max_tokens", "max_output_tokens"}:
        raise SVGReviewError("评分模型输出被截断，请重试或换一个评分模型")
    content = (choices[0].get("message") or {}).get("content", "")
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return content if isinstance(content, str) else ""


def _http_error_detail(raw):
    """从上游错误响应中提取可读信息，避免把整段 JSON 塞进记录。"""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(error, str):
        return error
    return raw


def _network_failure(exc):
    """保留网络错误类型和系统错误码，避免异常原文带出代理凭据或地址。"""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else None
    cause = reason if isinstance(reason, BaseException) else exc
    while cause.__cause__ is not None and not isinstance(cause, httpcore.LocalProtocolError):
        cause = cause.__cause__
    kind = type(cause).__name__
    code = getattr(cause, "winerror", None) or getattr(cause, "errno", None)
    if isinstance(code, int):
        kind += f" {code}"
    if isinstance(exc, TimeoutError) or isinstance(cause, TimeoutError):
        return f"连接或读取超时（{kind}）", True
    if isinstance(cause, socket.gaierror):
        return f"域名解析失败（{kind}）", cause.errno == socket.EAI_AGAIN
    if isinstance(cause, ssl.SSLCertVerificationError):
        return f"TLS 证书校验失败（{kind}）", False
    if isinstance(cause, ssl.SSLError) and not isinstance(cause, ssl.SSLEOFError):
        return f"TLS 连接失败（{kind}）", False
    if isinstance(exc, httpcore.ProxyError):
        return f"代理连接失败（{kind}）", False
    if isinstance(cause, ConnectionRefusedError):
        return f"连接被拒绝（{kind}）", False
    if isinstance(cause, (httpcore.LocalProtocolError, httpcore.UnsupportedProtocol, http.client.InvalidURL)):
        return f"请求格式无效（{kind}）", False
    if isinstance(exc, ConnectionError) or isinstance(
        cause, (ConnectionError, http.client.IncompleteRead, ssl.SSLEOFError,
                httpcore.NetworkError, httpcore.RemoteProtocolError),
    ):
        return f"连接中断（{kind}）", True
    return f"网络异常（{kind}）", False


def _request_review(opener, request, timeout, protocol, api_key, use_default_rubric):
    """执行单次评分请求，区分可能自行恢复的故障和明确的配置错误。"""
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        request_id = next((exc.headers.get(name) for name in ("x-request-id", "request-id", "cf-ray")
                           if exc.headers and exc.headers.get(name)), "")
        request_id = "".join(char for char in request_id if char.isascii() and (char.isalnum() or char in "-_."))
        request_id = request_id[:128] if str(api_key) not in request_id else ""
        try:
            detail = _http_error_detail(exc.read(2048).decode("utf-8", "replace"))
        except (OSError, http.client.HTTPException):
            detail = "上游错误正文未完整接收"
        finally:
            exc.close()
        detail = " ".join(detail.replace(str(api_key), "[REDACTED]").split())
        if "reasoning effort" in detail.lower() or "reasoning_effort" in detail.lower():
            detail += "；请在设置页调整“评分思考等级”"
        raise SVGReviewError(
            f"评分渠道返回 HTTP {exc.code}：{detail[:220]}",
            retryable=exc.code in RETRYABLE_HTTP_STATUSES, http_status=exc.code,
            request_id=request_id or None,
        ) from exc
    except (OSError, http.client.IncompleteRead, http.client.InvalidURL,
            httpcore.ProxyError, httpcore.UnsupportedProtocol) as exc:
        detail, retryable = _network_failure(exc)
        raise SVGReviewError(f"无法连接评分渠道：{detail}", retryable=retryable) from exc

    if len(raw) > MAX_RESPONSE_BYTES:
        raise SVGReviewError("评分响应超过大小限制")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SVGReviewError("评分渠道返回的不是有效 JSON", retryable=True) from exc
    if not isinstance(payload, dict):
        raise SVGReviewError("评分渠道返回格式无效", retryable=True)
    if payload.get("error") or payload.get("type") == "error":
        raise SVGReviewError("评分渠道返回错误，请检查渠道和模型配置")
    try:
        return _normalize(_extract_json(_response_text(payload, protocol)), use_default_rubric)
    except SVGReviewError as exc:
        # 模型可能偶发空输出、截断或缺项，所有评分提示词共用重试策略。
        raise SVGReviewError(str(exc), retryable=True) from exc
    except (AttributeError, KeyError, TypeError) as exc:
        raise SVGReviewError("评分渠道返回格式无效", retryable=True) from exc


def review_svg(relay, *, preview_image, prompt=None, model=None, api_timeout_minutes=15,
               reasoning_effort=DEFAULT_REVIEW_REASONING_EFFORT,
               retry_count=DEFAULT_REVIEW_RETRY_COUNT, fallback_relay=None, urlopen=None):
    """只把 SVG 的已渲染成品图交给评审模型打分；失败时抛出 SVGReviewError。

    reasoning_effort 由设置页单独配置，默认 low；none/default 表示不发送
    推理参数，避免把模型不支持的档位强行传给上游。
    retry_count 是首次失败后的额外尝试次数，每次使用同一个成品图和单次请求时限。
    上游网关故障时，余下的重试可改走显式配置的备用渠道，继续使用同一个模型。
    返回 {"score", "problems", "comment", "relay", "relay_label", "model"}，
    供服务端写入 score 与 score_detail。
    """
    model = (model or relay.get("model") or "").strip()
    prompt = (prompt or "").strip() or DEFAULT_REVIEW_PROMPT
    use_default_rubric = prompt == DEFAULT_REVIEW_PROMPT
    if not use_default_rubric:
        prompt = f"{prompt}\n\n{VISUAL_ONLY_GUARD}"
    if not relay.get("baseUrl") or not relay.get("apiKey") or not model:
        raise SVGReviewError("评分渠道地址、Key 或模型未配置")
    if type(retry_count) is not int or retry_count < 0:
        raise SVGReviewError("评分重试次数必须是非负整数")
    timeout = api_timeout_seconds(api_timeout_minutes)
    max_attempts = retry_count + 1
    primary_relay = relay
    if fallback_relay and (
        fallback_relay.get("name") == relay.get("name")
        or not fallback_relay.get("baseUrl") or not fallback_relay.get("apiKey")
    ):
        fallback_relay = None
    request_data = None
    for attempt in range(1, max_attempts + 1):
        if request_data is None:
            protocol = relay.get("protocol", "openai")
            body, headers = _build_request(relay, model, prompt, reasoning_effort, preview_image)
            endpoint = endpoint_url(protocol, relay["baseUrl"])
            request_data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            # OpenAI 评分复用 HTTP/2 协商和保活，按完整 JSON 读取评分响应。
            opener = urlopen or (urllib.request.urlopen if protocol == "anthropic" else stream_transport.urlopen)
        request = urllib.request.Request(endpoint, data=request_data, headers=headers, method="POST")
        started = time.monotonic()
        try:
            result = _request_review(opener, request, timeout, protocol, relay["apiKey"], use_default_rubric)
        except SVGReviewError as exc:
            exc.attempts = attempt
            exc.relay = relay.get("name", "")
            exc.protocol = protocol
            print(f"[svg] 评分请求失败：尝试={attempt}/{max_attempts}，渠道={exc.relay}，协议={protocol}，"
                  f"模型={model}，请求编号={exc.request_id or '无'}，耗时={time.monotonic() - started:.2f}秒，原因={exc}")
            if not exc.retryable or attempt >= max_attempts:
                raise
            if relay is primary_relay and fallback_relay and exc.http_status in FAILOVER_HTTP_STATUSES:
                relay = fallback_relay
                request_data = None
            print(f"[svg] 立即进行第 {attempt}/{retry_count} 次评分重试：渠道={relay.get('name', '')}")
        else:
            break
    return {
        **result,
        "review_attempts": attempt,
        "review_basis": "rendered-image",
        "relay": relay.get("name", ""),
        "relay_label": relay.get("label", ""),
        "protocol": protocol,
        "model": model,
        **({"primary_relay": primary_relay.get("name", "")} if relay is not primary_relay else {}),
    }
