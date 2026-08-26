# -*- coding: utf-8 -*-
"""
开源标准评测数据集导入模块

从公开的开源基准数据集抽样题目，替换系统题库：
- gsm8k: GSM8K（OpenAI 官方，8479 题，难度低，适合快速冒烟）
- livebench-math: LiveBench Math（每月更新防污染，中等难度）
- aime: AIME 美国数学邀请赛历年真题（90 题，竞赛级难度，强模型也只有 50-80% 正确率）
- math-hard: MATH 数据集 Level 4-5 数值题（高中竞赛难度，跨 7 个科目筛数值答案）

GSM8K 走双镜像（原始源 + hf-mirror 国内镜像）；其余经 HuggingFace
datasets-server API 获取，只保留纯数字答案的题目，保证客观判分可靠。
仅使用 Python 标准库。
"""

import csv
import io
import json
import random
import re
import urllib.request

from app_paths import USER_DATA_DIR

# 题目统一加的作答要求（保证输出格式便于客观判分）
GSM8K_SUFFIX = "\n请推理后直接给出最终数字答案，只输出一个数字。"
LIVEBENCH_SUFFIX = "\n请推理后直接给出最终答案，不要输出解释。"
AIME_SUFFIX = "\n请推理后只输出最终的整数答案（0-999），不要输出任何其他内容。"
MATH_SUFFIX = "\n请推理后只输出最终的数字答案（可以是小数），不要输出任何其他内容。"

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


def import_dataset(source, count, seed=None):
    """下载开源数据集并抽样 count 道题，返回题目列表。

    source: 数据集标识（gsm8k / livebench-math / aime / math-hard）
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
    if source in LIVEBENCH_DATASETS:
        return load_livebench(LIVEBENCH_DATASETS[source], count)
    raise ValueError(f"未知数据源: {source}，可选: gsm8k, aime, math-hard, "
                     f"{', '.join(LIVEBENCH_DATASETS)}")


def list_sources():
    """返回可选数据源列表（供前端下拉框使用，按难度排序）。"""
    return [
        {"id": "math-hard", "label": "MATH 竞赛难题 L4-5【推荐】高中竞赛难度，强模型 60-80%"},
        {"id": "aime", "label": "AIME 数学邀请赛【高难】90 题，GPT-4o 仅 ~13%，区分度极高"},
        {"id": "livebench-math", "label": "LiveBench Math（每月更新防污染，中等难度）"},
        {"id": "gsm8k", "label": "GSM8K（小学数学，难度低，仅适合冒烟测试）"},
    ]
