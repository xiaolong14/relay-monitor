# -*- coding: utf-8 -*-
"""
开源标准评测数据集导入模块

从公开的开源基准数据集抽样题目，替换系统题库：
- gsm8k: GSM8K（OpenAI 官方，8479 题，难度低，适合快速冒烟）
- livebench-math: LiveBench Math（每月更新防污染，中等难度）
- aime: AIME 美国数学邀请赛历年真题（90 题，竞赛级难度，强模型也只有 50-80% 正确率）
- math-hard: MATH 数据集 Level 4-5 数值题（高中竞赛难度，跨 7 个科目筛数值答案）
- humaneval-plus: HumanEval+ 编程题（EvalPlus 官方强化版，164 题，80 倍测试输入）
- mbpp-plus: MBPP+ 编程题（EvalPlus 官方强化版，378 题，35 倍测试输入）

编程题采用 EvalPlus diff-testing 判分：模型代码在本机沙箱执行，与参考实现
在大规模强化输入上逐一比对输出，靠背题/记答案无法通过，可有效识别编码
能力的「换药/降智」（见 codegrade.py）。

GSM8K 走双镜像（原始源 + hf-mirror 国内镜像）；数学题经 HuggingFace
datasets-server API 获取；编程题从 EvalPlus 官方 GitHub Release 下载。
仅使用 Python 标准库。
"""

import concurrent.futures as futures
import csv
import gzip
import io
import json
import random
import re
import urllib.request

from app_paths import USER_DATA_DIR
import codegrade

# 题目统一加的作答要求（保证输出格式便于客观判分）
GSM8K_SUFFIX = "\n请推理后直接给出最终数字答案，只输出一个数字。"
LIVEBENCH_SUFFIX = "\n请推理后直接给出最终答案，不要输出解释。"
AIME_SUFFIX = "\n请推理后只输出最终的整数答案（0-999），不要输出任何其他内容。"
MATH_SUFFIX = "\n请推理后只输出最终的数字答案（可以是小数），不要输出任何其他内容。"
CODE_SUFFIX = ("\n请实现上述函数，只输出完整可独立运行的 Python 代码"
               "（含必要的 import 与完整函数定义），用 ```python 代码块包裹，"
               "不要输出任何解释。")

# EvalPlus 官方 GitHub Release 下载地址（与 evalplus 库源码中的地址一致）
EVALPLUS_URLS = {
    "humaneval-plus": ("https://github.com/evalplus/humanevalplus_release"
                       "/releases/download/v0.1.10/HumanEvalPlus.jsonl.gz"),
    "mbpp-plus": ("https://github.com/evalplus/mbppplus_release"
                  "/releases/download/v0.2.0/MbppPlus.jsonl.gz"),
}

# 每道编程题携带的强化测试输入上限（在保证判分严格的同时控制判分耗时与库体积）
CODE_MAX_PLUS_INPUTS = 60
# 单题测试输入序列化后的体积上限（超出则丢弃最大的输入）
CODE_MAX_INPUT_BYTES = 150 * 1024

# 纯数字答案（可含小数/负号）——这类答案可用词边界精确匹配
NUMERIC_ANSWER = re.compile(r"^-?\d+(\.\d+)?$")

# GSM8K 的下载地址（原始源 + 国内镜像，按顺序尝试）
GSM8K_URLS = [
    "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl",
    "https://hf-mirror.com/datasets/openai/gsm8k/resolve/main/gsm8k/test.jsonl",
]

# LiveBench 各类别数据集（经 datasets-server API 按页拉取）
LIVEBENCH_DATASETS = {
    "livebench-math": "livebench/math",
}

# MATH 数据集的 7 个科目配置（只用 test split，避免训练集污染）
MATH_CONFIGS = [
    "algebra", "counting_and_probability", "geometry",
    "intermediate_algebra", "number_theory", "prealgebra", "precalculus",
]

_HEADERS = {"User-Agent": "Mozilla/5.0 (relay-quality-monitor)"}


def _fetch_text(urls):
    """依次尝试多个下载地址，返回文本内容；全部失败抛出最后一个异常。"""
    last_err = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:  # 换下一个镜像重试
            last_err = e
    raise RuntimeError(f"所有下载地址均失败: {last_err}")


def _fetch_json(url):
    """请求 JSON API 并返回解析结果。"""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_gsm8k(count):
    """解析 GSM8K jsonl：question 为题干，answer 末尾 '#### 数字' 为标准答案。"""
    text = _fetch_text(GSM8K_URLS)
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        raw_ans = obj.get("answer", "")
        # GSM8K 答案格式："...推理...\n#### 72"，取 #### 后的数字（去掉千分位逗号）
        if "####" not in raw_ans:
            continue
        ans = raw_ans.split("####")[-1].strip().replace(",", "")
        items.append({"q": obj["question"].strip() + GSM8K_SUFFIX, "answer": ans})
    return random.sample(items, min(count, len(items)))


def load_livebench(dataset_id, count):
    """经 datasets-server API 拉取 LiveBench 题目（只保留纯数字答案）。"""
    items = []
    offset, page = 0, 100
    while len(items) < count:
        url = (f"https://datasets-server.huggingface.co/rows?dataset={dataset_id}"
               f"&config=default&split=test&offset={offset}&length={page}")
        data = _fetch_json(url)
        rows = data.get("rows", [])
        if not rows:
            break
        for r in rows:
            row = r.get("row", {})
            turns = row.get("turns") or []
            gt = str(row.get("ground_truth", "")).strip()
            # 只取单轮提问 + 纯数字答案的题，确保判分客观无歧义
            if turns and NUMERIC_ANSWER.match(gt):
                items.append({"q": str(turns[0]).strip() + LIVEBENCH_SUFFIX, "answer": gt})
        offset += page
    return random.sample(items, min(count, len(items)))


def load_aime(count):
    """AIME 历年真题（AI-MO/aimo-validation-aime，90 题，答案为 0-999 整数）。

    竞赛级难度：GPT-4o 正确率约 13%，顶级推理模型 50-80%，区分度极高。
    """
    items = []
    offset, page = 0, 100
    while True:
        url = ("https://datasets-server.huggingface.co/rows?dataset=AI-MO/aimo-validation-aime"
               f"&config=default&split=train&offset={offset}&length={page}")
        data = _fetch_json(url)
        rows = data.get("rows", [])
        if not rows:
            break
        for r in rows:
            row = r.get("row", {})
            problem = str(row.get("problem") or "").strip()
            ans = str(row.get("answer") or "").strip()
            if problem and NUMERIC_ANSWER.match(ans):
                items.append({"q": problem + AIME_SUFFIX, "answer": ans})
        offset += page
        if offset >= 90:  # 数据集共 90 题
            break
    return random.sample(items, min(count, len(items)))


def _extract_boxed(text):
    """从 MATH 数据集的 solution 中提取最后一个 \\boxed{...} 的内容。

    处理花括号嵌套（如 \\boxed{\\frac{1}{2}}）；未找到返回 None。
    """
    idx = text.rfind("\\boxed{")
    if idx < 0:
        return None
    i = idx + len("\\boxed{")
    depth, out = 1, []
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        out.append(c)
        i += 1
    return "".join(out).strip() if depth == 0 else None


def load_math_hard(count):
    """MATH 数据集 Level 4-5 数值题（高中竞赛难度）。

    优先读本地题池缓存（data/math_hard_pool.json，首次全量筛选约数分钟，
    之后秒级抽样）；无缓存时遍历 7 个科目的 test split，筛选 Level 4/5
    且 boxed 答案为纯数字的题目，收集到足够数量后写缓存并抽样。
    """
    cache = USER_DATA_DIR / "data" / "math_hard_pool.json"
    if cache.exists():
        try:
            items = json.loads(cache.read_text(encoding="utf-8"))
            if items:
                return random.sample(items, min(count, len(items)))
        except Exception:
            pass  # 缓存损坏则重新收集

    items = []
    # 早停阈值：抽样的 2 倍且不少于 120 题（兼顾多样性与下载耗时）
    enough = max(count * 2, 120)
    for cfg in MATH_CONFIGS:
        if len(items) >= enough:
            break
        offset, page = 0, 100
        while True:
            url = ("https://datasets-server.huggingface.co/rows?dataset=EleutherAI/hendrycks_math"
                   f"&config={cfg}&split=test&offset={offset}&length={page}")
            try:
                data = _fetch_json(url)
            except Exception:
                break  # 单科目失败跳过，其余继续
            rows = data.get("rows", [])
            if not rows:
                break
            for r in rows:
                row = r.get("row", {})
                level = str(row.get("level") or "")
                if "Level 4" not in level and "Level 5" not in level:
                    continue
                problem = str(row.get("problem") or "").strip()
                ans = _extract_boxed(str(row.get("solution") or ""))
                if problem and ans and NUMERIC_ANSWER.match(ans):
                    items.append({"q": problem + MATH_SUFFIX, "answer": ans})
            offset += page
            if len(rows) < page:
                break
    if not items:
        raise RuntimeError("MATH 难题筛选结果为空")
    # 写入题池缓存（供下次秒级抽样）
    try:
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 缓存写失败不影响本次结果
    return random.sample(items, min(count, len(items)))


def _fetch_gzip_jsonl(url):
    """下载 .jsonl.gz 并解析为对象列表。"""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = gzip.decompress(resp.read()).decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _mbpp_deserialize_inputs(task_id, inputs):
    """MBPP+ 输入反序列化（与 evalplus 官方逻辑一致）。

    官方 jsonl 中 tuple/set/complex 等被序列化为 list/str，特定题号需要
    还原，否则参考实现与模型代码拿到的输入类型错误。
    """
    tid = int(str(task_id).split("/")[-1])
    if tid in [2, 116, 132, 143, 222, 261, 273, 394, 399, 421, 424, 429,
               470, 560, 579, 596, 616, 630, 726, 740, 744, 809]:
        return [[tuple(lst) for lst in inp] for inp in inputs]
    if tid in [63, 64, 70, 94, 120, 237, 272, 299, 400, 409, 417, 438,
               473, 614, 780]:
        return [[[tuple(lst) for lst in lst_lst] for lst_lst in inp]
                for inp in inputs]
    if tid in [75, 413, 444, 753]:
        return [[[tuple(lst) for lst in inp[0]]] + [inp[1]] for inp in inputs]
    if tid in (106, 750):
        return [[inp[0]] + [tuple(inp[1])] for inp in inputs]
    if tid == 115:
        return [[[set(item) if isinstance(item, list) and len(item) else {}
                  for item in inp[0]]] for inp in inputs]
    if tid == 124:
        return [(float(inp[0]), complex(inp[1])) for inp in inputs]
    if tid in [250, 405, 446, 617, 720, 763, 808]:
        return [[tuple(inp[0])] + inp[1:] for inp in inputs]
    if tid in [259, 401, 445]:
        modified = [[[tuple(lst) for lst in lst_lst] for lst_lst in inp]
                    for inp in inputs]
        return [[tuple(lst) for lst in inp] for inp in modified]
    if tid == 278:
        return [[[tuple(item) if isinstance(item, list) else item
                  for item in inp[0]]] for inp in inputs]
    if tid == 307:
        return [[tuple(inp[0])] + [inp[1], inp[2]] for inp in inputs]
    if tid == 722:
        return [[{key: tuple(value) for key, value in inp[0].items()}]
                + inp[1:] for inp in inputs]
    if tid == 252:
        return [[complex(inp[0])] for inp in inputs]
    if tid in [580, 615, 791]:
        def to_tuple(x):
            if isinstance(x, list):
                return tuple(to_tuple(i) for i in x)
            return x
        return [[to_tuple(v) for v in inp] for inp in inputs]
    return inputs


def _sample_code_inputs(task_id, base_input, plus_input):
    """为编程题挑选测试输入：全部基础输入 + 固定种子抽样的强化输入。

    种子按题号固定，保证不同中转站、不同日期评测使用完全相同的输入集。
    同时限制序列化体积，超出时丢弃最大的强化输入。
    输入保留原生 Python 类型（tuple/set/complex），以 repr 字符串存储。
    """
    base = list(base_input or [])
    plus = list(plus_input or [])
    rng = random.Random(f"evalplus-{task_id}")
    if len(plus) > CODE_MAX_PLUS_INPUTS:
        plus = rng.sample(plus, CODE_MAX_PLUS_INPUTS)
    inputs = base + plus
    # 体积控制：超大输入（复杂度陷阱题）优先保留基础输入，丢弃最大者
    while inputs and len(repr(inputs)) > CODE_MAX_INPUT_BYTES:
        sizes = [len(repr(i)) for i in inputs]
        idx = max(range(len(sizes)), key=lambda i: sizes[i])
        if idx < len(base):
            break  # 基础输入本身就超大：保留现状（判分时参考实现崩溃的输入会自动跳过）
        inputs.pop(idx)
    return inputs


def _build_code_question(task, source):
    """把 EvalPlus 原始题目转换为系统编程题格式。

    返回 {q, answer, qtype, test, entry_point}：
    - q 为发给模型的题面（函数签名/题面描述 + 实现要求）
    - test 为判分 payload 的 JSON 字符串（参考实现/输入/atol/入口名）
    - answer 固定为空串（编程题不用字符串匹配判分）
    """
    entry = str(task.get("entry_point") or "").strip()
    canonical = str(task.get("canonical_solution") or "")
    prompt = str(task.get("prompt") or "").strip()
    if not entry or not canonical.strip() or not prompt:
        return None
    if source == "humaneval-plus":
        # HumanEval+：prompt 含 import+签名+docstring，canonical 为缩进函数体
        canonical_full = prompt + canonical
        q = prompt + CODE_SUFFIX
    else:
        # MBPP+：prompt 为独立 docstring（题面描述+示例断言），canonical 为完整函数
        desc = prompt.strip()
        if desc.startswith('"""') and desc.endswith('"""'):
            desc = desc[3:-3].strip()
        canonical_full = canonical
        q = (f"{desc}\n\n请实现函数 `{entry}`（函数名与参数必须一致）。" + CODE_SUFFIX)
    base_input = task.get("base_input") or []
    plus_input = task.get("plus_input") or []
    if source == "mbpp-plus":
        base_input = _mbpp_deserialize_inputs(task["task_id"], base_input)
        plus_input = _mbpp_deserialize_inputs(task["task_id"], plus_input)
    if not base_input:
        return None
    inputs = _sample_code_inputs(task["task_id"], base_input, plus_input)
    payload = {
        "task_id": task["task_id"],
        "canonical": canonical_full,
        # repr 字符串：保留 tuple/set/complex 等原生类型，判分端 eval 还原
        "inputs_repr": repr(inputs),
        "atol": float(task.get("atol") or 0),
        "entry_point": entry,
    }
    return {
        "q": q,
        "answer": "",
        "qtype": "code",
        "test": json.dumps(payload, ensure_ascii=False),
        "entry_point": entry,
    }


def _evalplus_pool(source):
    """构建并缓存指定数据源的编程题池（含判分 payload，已通过自校验）。

    首次构建：下载官方 jsonl.gz → 逐题构建判分 payload → 并行用参考实现
    自校验（剔除输入损坏/参考实现不可运行的题）→ 缓存到 data/ 目录。
    之后直接从缓存秒级读取。
    """
    cache = USER_DATA_DIR / "data" / f"evalplus_pool_{source}.json"
    if cache.exists():
        try:
            pool = json.loads(cache.read_text(encoding="utf-8"))
            if pool:
                return pool
        except Exception:
            pass  # 缓存损坏则重建

    tasks = _fetch_gzip_jsonl(EVALPLUS_URLS[source])
    pool = []
    for task in tasks:
        q = _build_code_question(task, source)
        if q:
            pool.append(q)
    if not pool:
        raise RuntimeError(f"{source} 题目构建结果为空")

    # 自校验：用参考实现自身跑判分，剔除不可判的题（并行加速）
    validated = []
    with futures.ThreadPoolExecutor(max_workers=8) as pool_exec:
        results = list(pool_exec.map(
            lambda item: codegrade.validate_payload(json.loads(item["test"])),
            pool))
    for item, ok in zip(pool, results):
        if ok:
            validated.append(item)
    dropped = len(pool) - len(validated)
    if not validated:
        raise RuntimeError(f"{source} 全部题目未通过判分自校验")
    print(f"[datasets] {source} 题池构建完成: {len(validated)} 题可用"
          + (f"，剔除 {dropped} 题（输入/参考实现异常）" if dropped else ""))

    try:
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(validated, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 缓存写失败不影响本次结果
    return validated


def load_evalplus(source, count):
    """从 EvalPlus 编程题池抽样 count 道题（HumanEval+ / MBPP+）。"""
    pool = _evalplus_pool(source)
    return random.sample(pool, min(count, len(pool)))


def import_dataset(source, count, seed=None):
    """下载开源数据集并抽样 count 道题，返回题目列表。

    source: 数据集标识（gsm8k / livebench-math / aime / math-hard /
            humaneval-plus / mbpp-plus）
    count: 抽样题目数量
    seed: 随机种子（不传则每次抽样不同，便于定期换题）
    """
    if seed is not None:
        random.seed(seed)
    if source == "gsm8k":
        return load_gsm8k(count)
    if source == "aime":
        return load_aime(count)
    if source == "math-hard":
        return load_math_hard(count)
    if source in EVALPLUS_URLS:
        return load_evalplus(source, count)
    if source in LIVEBENCH_DATASETS:
        return load_livebench(LIVEBENCH_DATASETS[source], count)
    raise ValueError(f"未知数据源: {source}，可选: gsm8k, aime, math-hard, "
                     f"{', '.join(EVALPLUS_URLS)}, "
                     f"{', '.join(LIVEBENCH_DATASETS)}")


def list_sources():
    """返回可选数据源列表（供前端下拉框使用，按难度排序）。"""
    return [
        {"id": "humaneval-plus", "label": "HumanEval+ 编程题【荐·测换药】164 题，强化测试真实执行代码"},
        {"id": "mbpp-plus", "label": "MBPP+ 编程题【荐·测换药】378 题，强化测试真实执行代码"},
        {"id": "math-hard", "label": "MATH 竞赛难题 L4-5 高中竞赛难度，强模型 60-80%"},
        {"id": "aime", "label": "AIME 数学邀请赛【高难】90 题，GPT-4o 仅 ~13%"},
        {"id": "livebench-math", "label": "LiveBench Math（每月更新防污染，中等难度）"},
        {"id": "gsm8k", "label": "GSM8K（小学数学，难度低，仅适合冒烟测试）"},
    ]
