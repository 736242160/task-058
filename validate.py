#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate.py — 按字段间规则校验数据文件的单文件工具（仅依赖 Python 标准库）

用法:
    python3 validate.py 数据文件 规则文件

数据文件: CSV 格式，第一行为表头；字段可被双引号包裹，引号内的逗号、
          换行、以及 "" 转义的双引号均可正确处理。

规则文件: 每行一条规则，格式为「规则名: 条件」，# 开头为注释，空行忽略。
          冒号中英文均可。条件支持以下形式：

    字段 not_empty                  字段非空
    字段 between 下界 and 上界       数值范围（闭区间）
    字段 date 格式                   日期格式，如 YYYY-MM-DD、YYYY/MM/DD、
                                    YYYYMMDD、YYYY-MM-DD HH:MM:SS，或直接写
                                    strptime 格式如 %Y-%m-%d（严格校验）
    字段A == 字段B                   字段间一致，运算符支持 == != > >= < <=
                                    （两边都是数值按数值比较，否则按字符串比较；
                                     右值若不是字段名则按字面量处理）
    if 规则名 then 条件              嵌套校验：仅当引用的规则对该行通过时才检查
    if not 规则名 then 条件          仅当引用的规则对该行不通过时才检查
                                    （被引用的规则必须先定义）

输出: 校验报告。每条规则报告不满足的数据行号（从 1 起，不含表头；引号内
      换行的记录仍算 1 行）、涉及字段、期望与实际的差异。某条规则失败或
      定义错误不影响其他规则执行。数据文件存在未闭合引号时报告其起始行。

退出码: 0 全部通过；1 存在不满足的记录或规则定义错误；2 数据文件无法解析。
"""

import argparse
import csv
import io
import re
import sys
from datetime import datetime

DATE_FORMATS = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY/MM/DD": "%Y/%m/%d",
    "YYYYMMDD": "%Y%m%d",
    "YYYY-MM-DD HH:MM:SS": "%Y-%m-%d %H:%M:%S",
    "YYYY/MM/DD HH:MM:SS": "%Y/%m/%d %H:%M:%S",
    "MM/DD/YYYY": "%m/%d/%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
}

OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


class RuleDefError(Exception):
    """规则定义错误。"""


# ---------------------------------------------------------------- 引号检查

def find_unclosed_quote(text):
    """扫描文本，若存在未闭合的引号，返回其起始行号（1 起），否则返回 None。"""
    in_quotes = False
    at_field_start = True
    start_line = None
    line = 1
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':   # "" 转义
                    i += 2
                    continue
                in_quotes = False
                at_field_start = False
            elif ch == '\n':
                line += 1
        else:
            if ch == '"':
                if at_field_start:
                    in_quotes = True
                    start_line = line
                else:
                    at_field_start = False
            elif ch == ',':
                at_field_start = True
            elif ch == '\n':
                line += 1
                at_field_start = True
            elif ch != '\r':
                at_field_start = False
        i += 1
    return start_line if in_quotes else None


# ---------------------------------------------------------------- 条件解析

def parse_condition(text):
    """把条件文本解析为元组形式的语法树，失败抛出 RuleDefError。"""
    text = text.strip()
    if not text:
        raise RuleDefError("条件为空")

    m = re.match(r"^if\s+(not\s+)?(.+?)\s+then\s+(.+)$", text, re.S)
    if m:
        return ("if", bool(m.group(1)), m.group(2).strip(),
                parse_condition(m.group(3)))

    m = re.match(r"^(.+?)\s+not_empty$", text, re.S)
    if m:
        return ("not_empty", m.group(1).strip())

    m = re.match(r"^(.+?)\s+between\s+(\S+)\s+and\s+(\S+)$", text, re.S)
    if m:
        field, lo_s, hi_s = m.group(1).strip(), m.group(2), m.group(3)
        try:
            lo, hi = float(lo_s), float(hi_s)
        except ValueError:
            raise RuleDefError(f"between 的边界必须是数值: {lo_s} / {hi_s}")
        if lo > hi:
            raise RuleDefError(f"between 下界 {lo_s} 大于上界 {hi_s}")
        return ("between", field, lo, hi, lo_s, hi_s)

    m = re.match(r"^(.+?)\s+date\s+(.+)$", text, re.S)
    if m:
        field, fmt = m.group(1).strip(), m.group(2).strip()
        strp = DATE_FORMATS.get(fmt, fmt)
        try:
            datetime(2000, 1, 2, 3, 4, 5).strftime(strp)
        except (ValueError, TypeError):
            raise RuleDefError(f"无法识别的日期格式: {fmt}")
        return ("date", field, fmt, strp)

    m = re.match(r"^(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+)$", text, re.S)
    if m:
        return ("cmp", m.group(1).strip(), m.group(2), m.group(3).strip())

    raise RuleDefError(f"无法解析的条件: {text}")


def validate_condition(cond, header_set, defined_rules):
    """校验条件引用的字段与规则是否存在。"""
    kind = cond[0]
    if kind == "if":
        ref = cond[2]
        if ref not in defined_rules:
            raise RuleDefError(f"引用了未定义或定义在后的规则「{ref}」")
        validate_condition(cond[3], header_set, defined_rules)
    elif kind in ("not_empty", "between", "date"):
        if cond[1] not in header_set:
            raise RuleDefError(f"未知字段「{cond[1]}」")
    elif kind == "cmp":
        if cond[1] not in header_set:
            raise RuleDefError(f"未知字段「{cond[1]}」")
        # 右值若不是字段名则按字面量处理，无需校验


# ---------------------------------------------------------------- 条件求值

def _to_num(value):
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _unquote(text):
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def check(cond, record, passed):
    """对单条记录求值。通过返回 None；不通过返回 (涉及字段, 期望, 实际)。"""
    kind = cond[0]

    if kind == "if":
        _, neg, ref, sub = cond
        premise = passed(ref)
        if neg:
            premise = not premise
        if not premise:
            return None                      # 前提不成立，跳过本规则
        return check(sub, record, passed)

    if kind == "not_empty":
        field = cond[1]
        value = record.get(field, "")
        if value is None or str(value).strip() == "":
            return ([field], f"{field} 非空", f"{field}={value!r}")
        return None

    if kind == "between":
        _, field, lo, hi, lo_s, hi_s = cond
        value = record.get(field, "")
        num = _to_num(value)
        if num is None:
            return ([field], f"{field} 为数值且在 [{lo_s}, {hi_s}] 内",
                    f"{field}={value!r}（非数值）")
        if not lo <= num <= hi:
            return ([field], f"{lo_s} <= {field} <= {hi_s}",
                    f"{field}={value!r}")
        return None

    if kind == "date":
        _, field, fmt, strp = cond
        value = str(record.get(field, "")).strip()
        ok = False
        if value:
            try:
                ok = datetime.strptime(value, strp).strftime(strp) == value
            except ValueError:
                ok = False
        if not ok:
            return ([field], f"{field} 符合日期格式 {fmt}",
                    f"{field}={value!r}")
        return None

    if kind == "cmp":
        _, left, op, right = cond
        lv = str(record.get(left, ""))
        if right in record:                  # 字段间比较
            rv = str(record.get(right, ""))
            fields = [left, right]
            expected = f"{left} {op} {right}"
            actual = f"{left}={lv!r}, {right}={rv!r}"
        else:                                # 与字面量比较
            rv = _unquote(right)
            fields = [left]
            expected = f"{left} {op} {right}"
            actual = f"{left}={lv!r}"
        ln, rn = _to_num(lv), _to_num(rv)
        if ln is not None and rn is not None:
            x, y = ln, rn
        else:
            x, y = lv.strip(), rv.strip()
        try:
            if not OPS[op](x, y):
                return (fields, expected, actual)
        except TypeError:
            return (fields, expected, actual)
        return None

    raise RuleDefError(f"未知条件类型: {kind}")


# ---------------------------------------------------------------- 数据加载

def load_data(path):
    """返回 (表头, 记录列表, 行宽警告)。文件无法解析时抛出 ValueError。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()

    bad_line = find_unclosed_quote(text)
    if bad_line is not None:
        raise ValueError(f"存在未闭合的引号，起始行: 第 {bad_line} 行")

    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not rows[0]:
        raise ValueError("数据文件为空或缺少表头")

    header = rows[0]
    if any(h.strip() == "" for h in header):
        raise ValueError("表头存在空字段名")
    if len(set(header)) != len(header):
        raise ValueError("表头存在重复字段名")

    records, warnings = [], []
    for row in rows[1:]:
        if not row:                          # 空行跳过
            continue
        row_no = len(records) + 1
        if len(row) != len(header):
            warnings.append((row_no, len(row), len(header)))
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            else:
                row = row[:len(header)]
        records.append(dict(zip(header, row)))
    return header, records, warnings


def load_rules(path, header_set):
    """返回 (有效规则列表, 定义错误列表)。规则为 (名称, 原文, 语法树)。"""
    rules, errors = [], []
    defined = set()
    with open(path, encoding="utf-8-sig") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([^:：]+)[:：](.+)$", line)
            if not m:
                errors.append((lineno, line, "缺少「规则名: 条件」格式"))
                continue
            name, text = m.group(1).strip(), m.group(2).strip()
            if name in defined:
                errors.append((lineno, line, f"规则名「{name}」重复"))
                continue
            try:
                cond = parse_condition(text)
                validate_condition(cond, header_set, defined)
            except RuleDefError as e:
                errors.append((lineno, line, str(e)))
                continue
            rules.append((name, text, cond))
            defined.add(name)
    return rules, errors


# ---------------------------------------------------------------- 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="validate.py",
        description="按字段间规则校验数据文件（纯标准库单文件工具）",
        epilog="规则语法详见源文件头部注释。",
    )
    ap.add_argument("data", help="数据文件（CSV，首行表头）")
    ap.add_argument("rules", help="规则定义文件")
    args = ap.parse_args(argv)

    try:
        header, records, warnings = load_data(args.data)
    except OSError as e:
        print(f"错误: 无法读取数据文件: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"错误: 数据文件无法解析: {e}", file=sys.stderr)
        return 2

    try:
        rules, rule_errors = load_rules(args.rules, set(header))
    except OSError as e:
        print(f"错误: 无法读取规则文件: {e}", file=sys.stderr)
        return 2

    # 逐条规则独立求值，互不影响
    results = []          # (名称, 原文, [ (行号, 涉及字段, 期望, 实际) ])
    passed_rows = {}      # 规则名 -> [各行是否通过]
    for name, text, cond in rules:
        failures, bools = [], []
        for idx, rec in enumerate(records):
            r = check(cond, rec, lambda ref, i=idx: passed_rows[ref][i])
            bools.append(r is None)
            if r is not None:
                fields, expected, actual = r
                failures.append((idx + 1, fields, expected, actual))
        passed_rows[name] = bools
        results.append((name, text, failures))

    # ---------------- 输出报告 ----------------
    print("=" * 25 + " 数据校验报告 " + "=" * 25)
    print(f"数据文件: {args.data}（字段 {len(header)} 个，数据 {len(records)} 行）")
    print(f"规则文件: {args.rules}（有效规则 {len(rules)} 条）")
    print("说明: 行号为数据行号，从 1 起、不含表头；引号内换行的记录仍计 1 行")

    if warnings:
        print("\n结构警告（字段数与表头不一致，已按表头截断/补空）:")
        for row_no, got, want in warnings:
            print(f"    第 {row_no} 行: {got} 个字段，期望 {want} 个")

    if rule_errors:
        print("\n规则定义错误（这些规则未执行，不影响其他规则）:")
        for lineno, line, msg in rule_errors:
            print(f"    规则文件第 {lineno} 行: {line}  --  {msg}")

    print()
    for name, text, failures in results:
        if not failures:
            print(f"[通过] {name}: {text}")
        else:
            print(f"[失败] {name}: {text}（{len(failures)} 行不满足）")
            for row, fields, expected, actual in failures:
                print(f"    第 {row} 行 | 涉及字段: {', '.join(fields)}"
                      f" | 期望: {expected} | 实际: {actual}")

    failed_rules = sum(1 for _, _, f in results if f)
    total_failures = sum(len(f) for _, _, f in results)
    print("-" * 64)
    print(f"汇总: 规则 {len(rules)} 条，通过 {len(rules) - failed_rules} 条，"
          f"失败 {failed_rules} 条；不满足的记录共 {total_failures} 处")
    if rule_errors:
        print(f"另有 {len(rule_errors)} 条规则定义错误")
    return 1 if (failed_rules or rule_errors) else 0


if __name__ == "__main__":
    sys.exit(main())
