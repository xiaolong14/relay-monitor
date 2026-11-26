# -*- coding: utf-8 -*-
"""
编程题本地判分模块（EvalPlus diff-testing 方式）。

判分原理（与 EvalPlus 官方一致）：
- 每道编程题携带 payload：参考实现 canonical_solution、大规模测试输入
  （base_input + plus_input）、浮点容差 atol、入口函数名 entry_point
- 判分时：在同一脚本内分别 exec 参考实现与模型代码，对所有输入逐一运行，
  用 atol 感知的方式对比输出；全部一致才算通过
- 强化输入（plus_input，几十至上百组）专门覆盖边界与复杂度陷阱，
  靠背题/记答案无法通过，能有效识别「换药」后的编码能力降级

安全说明：模型生成的代码在本机以子进程方式真实执行（隔离模式 python -I，
30 秒超时）。这是 HumanEval/EvalPlus 等编程基准的标准做法，评测对象
为主流大模型 API，风险可控；如对安全极度敏感请勿使用编程题库。

仅使用 Python 标准库。
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# 单题判分超时（秒）：需容忍强化大输入下的正常慢速实现
GRADE_TIMEOUT = 30

# 判分脚本可用的公共 import（参考实现与模型代码共享的全局环境）
PRELUDE = """import math, re, itertools, collections, functools, string, heapq, bisect, operator
from typing import List, Tuple, Dict, Set, Optional, Any, Union
from collections import defaultdict, Counter, deque, OrderedDict
from functools import lru_cache, reduce
"""

# 提取模型输出中的 python 代码块
_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# atol 感知的递归对比逻辑（注入判分脚本）
_COMPARE_SRC = """
def _close(a, b, atol):
    # 浮点用 atol 容差比较
    if isinstance(a, float) or isinstance(b, float):
        if isinstance(a, bool) or isinstance(b, bool):
            return a == b
        try:
            fa, fb = float(a), float(b)
        except Exception:
            return False
        if math.isnan(fa) or math.isnan(fb):
            return math.isnan(fa) and math.isnan(fb)
        return abs(fa - fb) <= atol
    # 序列类型必须同型（tuple != list，与官方判分一致），元素递归比较
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        if type(a) is not type(b):
            return False
        if len(a) != len(b):
            return False
        return all(_close(x, y, atol) for x, y in zip(a, b))
    if isinstance(a, dict) or isinstance(b, dict):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_close(a[k], b[k], atol) for k in a)
    return a == b
"""


def extract_code(output):
    """从模型输出中提取可执行 Python 代码。

    优先取 ```python 代码块；无代码块但含函数定义时退回整段输出。
    提取不到返回 None。
    """
    text = (output or "").strip()
    if not text:
        return None
    blocks = _CODE_BLOCK.findall(text)
    if blocks:
        # 取第一个含 def 的代码块；都没有则用第一个块
        for b in blocks:
            if "def " in b:
                return b.strip()
        return blocks[0].strip()
    if "def " in text:
        return text
    return None


def build_harness(candidate_code, payload):
    """构建判分脚本：参考实现 + 模型代码 + 全输入对比。

    payload 字段：canonical（完整可 exec 的参考实现源码）、inputs_repr
    （参数列表的 repr 字符串，保留 tuple/set/complex 原生类型）、atol、
    entry_point。inputs_repr 由本地数据集构建，非模型输出，eval 还原安全。
    """
    inputs_repr = payload.get("inputs_repr") or repr(payload.get("inputs") or [])
    canonical = payload["canonical"]
    return (
        "import json\n"
        + PRELUDE
        + "\n"
        + _COMPARE_SRC
        + "\n_canonical_src = " + repr(canonical) + "\n"
        + "_candidate_src = " + repr(candidate_code) + "\n"
        + "_inputs = eval(" + repr(inputs_repr) + ")\n"
        + "_entry = " + repr(payload["entry_point"]) + "\n"
        + "_atol = " + repr(float(payload.get("atol") or 0)) + "\n"
        + "_prelude = " + repr(PRELUDE) + "\n"
        + """
_ns_c, _ns_m = {}, {}
# 预注入公共 import：模型常直接引用 prompt 中的 typing 名称而不自行 import
exec(_prelude, _ns_c)
exec(_prelude, _ns_m)
try:
    exec(_canonical_src, _ns_c)
    _canon = _ns_c[_entry]
except Exception:
    # 参考实现自身无法运行：该题不可判分，按失败处理并由题池自校验过滤
    print('BROKEN')
    raise SystemExit(0)
try:
    exec(_candidate_src, _ns_m)
except Exception:
    print('FAIL')
    raise SystemExit(0)
_cand = _ns_m.get(_entry)
if _cand is None:
    print('FAIL')
    raise SystemExit(0)
_ok = True
for _inp in _inputs:
    try:
        _exp = _canon(*_inp)
    except Exception:
        # 参考实现对该输入崩溃：输入数据异常，跳过该输入
        continue
    try:
        _got = _cand(*_inp)
    except Exception:
        _ok = False
        break
    if not _close(_got, _exp, _atol):
        _ok = False
        break
print('PASS' if _ok else 'FAIL')
"""
    )


def run_script(script, timeout=GRADE_TIMEOUT):
    """以隔离子进程执行判分脚本，返回 'PASS' / 'FAIL' / 'BROKEN' / 'TIMEOUT'。"""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(script)
            tmp = f.name
        proc = subprocess.run(
            [sys.executable, "-I", tmp],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "").strip().splitlines()
        return out[-1].strip() if out else "FAIL"
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except Exception:
        return "FAIL"
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except Exception:
                pass


def grade(output, payload):
    """对模型输出判分：提取代码并执行 diff-testing，通过返回 True。"""
    code = extract_code(output)
    if code is None:
        return False
    return run_script(build_harness(code, payload)) == "PASS"


def validate_payload(payload):
    """题池自校验：用参考实现自身作为候选代码跑一遍判分。

    通过说明 payload 完整可判（输入可反序列化、参考实现可运行、
    对比逻辑自洽）；不通过的题目会从题池中剔除。
    """
    return run_script(build_harness(payload["canonical"], payload)) == "PASS"
