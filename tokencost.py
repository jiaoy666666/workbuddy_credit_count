#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tokencost：测量当前 WorkBuddy agent 的一次受控自测。

固定流程：start -> Read 固定题目 -> finish --answer ... -> report
数据来源：当前会话 transcript 中每次模型调用的 rawUsage。
价格来源：模型厂商官方定价页、WorkBuddy 官方定价页；美元报价经中国外汇交易中心换算。
"""
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import html.parser
import json
import math
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import client_adapters

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(ROOT, "cases", "workload.json")
DATA_DIR = os.environ.get("TOKENSCOST_DATA_DIR") or os.path.join(ROOT, "data")
STATE_PATH = os.path.join(DATA_DIR, "measurement-state.json")
REPORT_PATH = os.path.join(DATA_DIR, "latest-report.json")
MAX_BASELINE_CONTEXT = 60000
MIN_FREE_CONTEXT = 30000
REPORT_VERSION = 3

KIMI_MODELS = {"kimi-k3.1-pro", "kimi-k3.1-pro-highspeed", "kimi-k3"}
DEEPSEEK_MODEL_KIND = {
    "deepseek-v4.1-flash": "flash",
    "deepseek-flash": "flash",
    "deepseek-v4-pro": "pro",
}
QWEN_MODEL_PAGES = {"qwen3.8-max": "qwen3-8-max", "qwen3.8-flash": "qwen3-8-flash"}
MINIMAX_MODELS = {"minimax-m3.1-flash"}
GLM_MODELS = {"glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"}
SUPPORTED_PRICE_MODELS = (KIMI_MODELS | set(DEEPSEEK_MODEL_KIND) | GLM_MODELS
                          | set(QWEN_MODEL_PAGES) | MINIMAX_MODELS)
TOOL_ALIASES = {
    "read": "Read", "read_file": "Read", "file_read": "Read",
    "bash": "Bash", "shell": "Bash", "powershell": "PowerShell",
}

KIMI_PRICE_URL = "https://platform.moonshot.cn/pricing"
DEEPSEEK_PRICE_URL = "https://api-docs.deepseek.com/quick_start/pricing"
GLM_PRICE_URL = "https://open.bigmodel.cn/api/biz/operation/query?ids=1160,1161"
QWEN_PRICE_URL = "https://help.aliyun.com/zh/model-studio/{model_page}"
MINIMAX_PRICE_URL = "https://platform.minimax.cn/docs/guides/pricing-paygo"
WORKBUDDY_PRICE_URL = "https://www.workbuddy.cn/docs/workbuddy/Pricing"
ALLOWED_PRICE_HOSTS = {
    "platform.moonshot.cn", "platform.kimi.com", "api-docs.deepseek.com",
    "open.bigmodel.cn", "help.aliyun.com", "platform.minimax.cn",
    "www.workbuddy.cn", "www.chinamoney.com.cn",
}


class MeasurementError(Exception):
    pass


class PriceHTTPError(MeasurementError):
    def __init__(self, status, url):
        super().__init__("官方价格页 HTTP %s：%s" % (status, url))
        self.status = status
        self.url = url


class WhitelistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_PRICE_HOSTS:
            raise MeasurementError("官方页面跳转到了非白名单地址：%s" % newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class TableParser(html.parser.HTMLParser):
    def __init__(self, region=None):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None
        self.region = region
        self.current_region = None
        self.heading = None
        self.table_matches = region is None

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4"):
            self.heading = []
        elif tag == "table":
            self.table_matches = self.region is None or self.current_region == self.region
        elif tag == "tr" and self.table_matches:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4") and self.heading is not None:
            self.current_region = " ".join("".join(self.heading).split())
            self.heading = None
        elif tag == "table":
            self.table_matches = self.region is None
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def html_rows(text, region=None):
    parser = TableParser(region=region)
    parser.feed(text)
    return parser.rows


def number(value):
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", value.replace(",", ""))
    if not match:
        raise MeasurementError("官方价格页出现无法识别的数值：%s" % value)
    return float(match.group(1))


def parse_kimi_price(text, model_id):
    pattern = (r"children:`" + re.escape(model_id) + r"`}\)," +
               r"\s*`¥([0-9.]+) / MTok`,\s*`¥([0-9.]+) / MTok`," +
               r"\s*`¥([0-9.]+) / MTok`,\s*`¥([0-9.]+) / MTok`," +
               r"\s*`¥([0-9.]+) / MTok`")
    match = re.search(pattern, text)
    if not match:
        raise MeasurementError("Kimi 官方定价页未找到模型 %s；工具不会套用其他型号价格" % model_id)
    values = [float(v) for v in match.groups()]
    return {
        "vendor": "Moonshot AI / Kimi", "model": model_id, "currency": "CNY",
        "input_miss": values[0], "output": values[1], "input_hit": values[2],
        "cache_write_5m": values[3], "cache_write_1h": values[4],
        "official_url": KIMI_PRICE_URL,
    }


def parse_deepseek_price(text, model_id):
    normalized = model_id.lower()
    model_kind = DEEPSEEK_MODEL_KIND.get(normalized)
    if not model_kind:
        raise MeasurementError("暂未内置 DeepSeek 模型 %s 的官方价格" % model_id)
    rows = html_rows(text)
    is_pro = model_kind == "pro"
    section = None
    result = {"vendor": "DeepSeek", "model": normalized, "currency": "USD",
              "offpeak": {}, "peak": {}, "official_url": DEEPSEEK_PRICE_URL}
    for cells in rows:
        joined = " ".join(cells).upper()
        if "CACHE HIT" in joined:
            section = "input_hit"
        elif "CACHE MISS" in joined:
            section = "input_miss"
        elif "OUTPUT TOKENS" in joined:
            section = "output"
        regime = "peak" if cells and cells[0].upper() == "PEAK" else (
            "offpeak" if "OFF-PEAK" in joined else None)
        if section and regime:
            if len(cells) < (3 if regime == "peak" else 4):
                continue
            value = cells[-1] if is_pro else cells[-2]
            result[regime][section] = number(value)
    for regime in ("offpeak", "peak"):
        for key in ("input_hit", "input_miss", "output"):
            if key not in result[regime]:
                raise MeasurementError("DeepSeek 官方定价页缺少 %s/%s" % (regime, key))
    return result


def _iter_glm_cards(payload):
    try:
        outer = json.loads(payload)
    except Exception as exc:
        raise MeasurementError("智谱官方价格配置不是合法 JSON") from exc
    if outer.get("code") != 200:
        raise MeasurementError("智谱官方价格配置返回错误：%s" % outer.get("msg", "未知"))
    for item in outer.get("data") or []:
        try:
            content = json.loads(item.get("content") or "{}")
        except Exception:
            continue
        for card in content.get("list") or []:
            yield card
        for tab in content.get("tabs") or []:
            for card in tab.get("cards") or []:
                yield card


def _glm_fields(card):
    fields = {}
    for item in card.get("fieldList") or []:
        values = item.get("values") or []
        if item.get("label") and values:
            fields[str(item["label"])] = str(values[0])
    table = card.get("table") or {}
    for row in table.get("modelList") or []:
        label = ""
        value = ""
        for cell in row.values():
            if isinstance(cell, dict):
                text = str(cell.get("value") or "").strip()
                if not label:
                    label = text
                else:
                    value = text
        if label and value:
            fields[label] = value
    return fields


def parse_glm_price(text, model_id):
    title_map = {
        "glm-5.3": "GLM-5.3",
        "glm-5.3-flash": "GLM-5.3-Flash",
        "glm-5.3-flashx": "GLM-5.3-FlashX",
    }
    wanted = title_map.get(model_id.lower())
    if not wanted:
        raise MeasurementError("暂未内置智谱模型 %s 的官方价格" % model_id)
    for card in _iter_glm_cards(text):
        if str(card.get("title") or "").lower() != wanted.lower():
            continue
        fields = _glm_fields(card)
        try:
            input_price = number(fields["输入单价"])
            output_price = number(fields["输出单价"])
            cache_hit = number(fields["缓存命中"])
        except KeyError as exc:
            raise MeasurementError("智谱官方价格页缺少 %s 的 %s" % (model_id, exc.args[0])) from exc
        return {
            "vendor": "智谱 AI / BigModel", "model": model_id.lower(), "currency": "CNY",
            "input_miss": input_price, "output": output_price, "input_hit": cache_hit,
            "cache_write_5m": 0.0,
            "official_url": "https://open.bigmodel.cn/pricing",
            "data_url": GLM_PRICE_URL,
        }
    raise MeasurementError("智谱官方价格配置未找到模型 %s" % model_id)


def parse_qwen_price(text, model_id):
    normalized = model_id.lower()
    if normalized not in QWEN_MODEL_PAGES:
        raise MeasurementError("暂未内置 Qwen 模型 %s 的官方价格" % model_id)
    decoded = text.replace('\\"', '"')
    rows = html_rows(decoded, region="华北2（北京）")
    values = {}
    for cells in rows:
        if len(cells) < 2:
            continue
        key = cells[0].replace(" ", "")
        if key in ("输入", "输出", "输入（缓存命中）", "显式缓存创建", "显式缓存命中") and key not in values:
            values[key] = number(cells[1])
    required = ("输入", "输出", "输入（缓存命中）", "显式缓存创建")
    missing = [key for key in required if key not in values]
    if missing:
        raise MeasurementError("阿里云官方价格页缺少 %s 的字段：%s" % (model_id, "、".join(missing)))
    return {
        "vendor": "阿里云百炼 / Qwen", "model": model_id.lower(), "currency": "CNY",
        "input_miss": values["输入"], "output": values["输出"],
        "input_hit": values["输入（缓存命中）"],
        "cache_write_5m": values["显式缓存创建"],
        "official_url": QWEN_PRICE_URL.format(model_page=QWEN_MODEL_PAGES[normalized]),
    }


def parse_minimax_price(text, model_id):
    wanted = model_id.lower()
    for cells in html_rows(text):
        if not cells or cells[0].strip().lower() != wanted:
            continue
        if len(cells) < 4:
            break
        return {
            "vendor": "MiniMax", "model": wanted, "currency": "CNY",
            "input_miss": number(cells[1]), "output": number(cells[2]),
            "input_hit": number(cells[3]), "cache_write_5m": 0.0,
            "official_url": MINIMAX_PRICE_URL,
        }
    raise MeasurementError("MiniMax 官方价格页未找到模型 %s" % model_id)


def parse_workbuddy_credit_price(text):
    matches = []
    for cells in html_rows(text):
        joined = " ".join(cells)
        if "积分" in joined and "元" in joined and "个月" in joined:
            credits = None
            price = None
            for cell in cells:
                if "积分" in cell:
                    credits = number(cell)
                elif "元" in cell:
                    price = number(cell)
            if credits == 1000 and price:
                matches.append({"credits": int(credits), "price_cny": price,
                                "unit_cny": price / credits, "currency": "CNY",
                                "official_url": WORKBUDDY_PRICE_URL,
                                "basis": "个人版 1,000 积分加量包补充成本，不是订阅积分分摊价或现金赎回价"})
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise MeasurementError("WorkBuddy 官方定价页存在多个 1,000 积分加量包，无法唯一选价")
    raise MeasurementError("WorkBuddy 官方定价页未找到唯一的个人版 1,000 积分加量包价格")


def parse_usd_cny(text):
    try:
        payload = json.loads(text)
        price = payload["data"]["vo"]["price"]
    except Exception as exc:
        raise MeasurementError("中国外汇交易中心汇率数据格式异常") from exc
    match = re.search(r"1美元对人民币([0-9.]+)元", price)
    if not match:
        raise MeasurementError("中国外汇交易中心汇率数据缺少美元中间价")
    return float(match.group(1))


def _usage_int(value, field):
    try:
        number_value = float(value)
    except (TypeError, ValueError) as exc:
        raise MeasurementError("调用记录字段 %s 不是整数：%r" % (field, value)) from exc
    if not math.isfinite(number_value) or not number_value.is_integer():
        raise MeasurementError("调用记录字段 %s 不是有限整数：%r" % (field, value))
    return int(number_value)


def _first_value(*values):
    return next((value for value in values if value is not None), None)


def normalize_usage(event):
    provider = event.get("providerData") or {}
    usage = provider.get("rawUsage") or {}
    if not usage:
        return None
    prompt_details = usage.get("prompt_tokens_details") or {}
    output_details = usage.get("completion_tokens_details") or {}
    input_tokens = _usage_int(_first_value(
        usage.get("prompt_tokens"), usage.get("input_tokens"), 0), "prompt_tokens")
    hit = _usage_int(_first_value(
        usage.get("prompt_cache_hit_tokens"), prompt_details.get("cached_tokens"), 0),
        "prompt_cache_hit_tokens")
    miss_value = _first_value(usage.get("prompt_cache_miss_tokens"), input_tokens - hit)
    miss = _usage_int(miss_value, "prompt_cache_miss_tokens")
    write = max(
        _usage_int(_first_value(usage.get("prompt_cache_write_tokens"), 0),
                   "prompt_cache_write_tokens"),
        _usage_int(_first_value(usage.get("cache_creation_input_tokens"), 0),
                   "cache_creation_input_tokens"))
    billed_output = _usage_int(_first_value(
        usage.get("completion_tokens"), usage.get("output_tokens"), 0), "completion_tokens")
    reasoning = _usage_int(_first_value(
        output_details.get("reasoning_tokens"), usage.get("completion_thinking_tokens"),
        usage.get("reasoning_tokens"), 0), "reasoning_tokens")
    credit_value = usage.get("credit")
    try:
        credit = float(credit_value) if credit_value is not None else None
    except (TypeError, ValueError) as exc:
        raise MeasurementError("调用记录 credit 不是数值：%r" % credit_value) from exc
    timestamp_ms = _usage_int(event.get("timestamp") or 0, "timestamp")
    try:
        arguments = json.loads(event.get("arguments") or "{}")
    except Exception:
        arguments = {}
    raw_tool = str(event.get("name") or "")
    tool = TOOL_ALIASES.get(raw_tool.lower(), raw_tool)
    if tool in ("Bash", "PowerShell") and "command" not in arguments and "cmd" in arguments:
        arguments = dict(arguments, command=arguments["cmd"])
    if tool == "Read" and "file_path" not in arguments and "filePath" in arguments:
        arguments = dict(arguments, file_path=arguments["filePath"])
    return {
        "event_id": event.get("id"), "event_type": event.get("type"),
        "tool": tool, "arguments": arguments,
        "timestamp_ms": timestamp_ms,
        "request_id": provider.get("conversationRequestId") or "",
        "model": provider.get("model") or provider.get("requestModelId") or "",
        "requested_model": provider.get("requestModelId") or "",
        "input": input_tokens, "input_hit": hit, "input_miss": miss,
        "cache_write": write, "billed_output": billed_output,
        "reasoning_output": reasoning,
        "visible_output": billed_output - reasoning,
        "credit": credit,
    }


def record_validation_errors(record, require_credit=True):
    errors = []
    label = str(record.get("event_id") or "未知事件")
    if not str(record.get("model") or "").strip():
        errors.append("调用 %s 缺少实际模型 ID" % label)
    if record["timestamp_ms"] <= 0:
        errors.append("调用 %s 缺少有效时间戳" % label)
    for field in ("input", "input_hit", "input_miss", "cache_write", "billed_output",
                  "reasoning_output"):
        if record[field] < 0:
            errors.append("调用 %s 的 %s 不能为负数" % (label, field))
    if record["input_hit"] + record["input_miss"] != record["input"]:
        errors.append("调用 %s 的缓存命中与未命中之和不等于输入总量" % label)
    if record["reasoning_output"] > record["billed_output"]:
        errors.append("调用 %s 的推理输出大于计费输出" % label)
    credit = record.get("credit")
    if require_credit:
        if credit is None:
            errors.append("调用 %s 缺少 credit 字段" % label)
        elif not math.isfinite(credit) or credit < 0:
            errors.append("调用 %s 的 credit 必须是有限非负数" % label)
    return errors


def read_usage_records(path):
    records = []
    event_lines = {}
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MeasurementError("会话记录第 %d 行不是完整 JSON，不能测量" % line_number) from exc
            record = normalize_usage(event)
            if record:
                event_id = record["event_id"]
                if not isinstance(event_id, str) or not event_id.strip():
                    raise MeasurementError("会话记录第 %d 行缺少稳定事件 ID" % line_number)
                if event_id in event_lines:
                    raise MeasurementError("会话记录事件 ID 重复：%s（第 %d、%d 行）"
                                           % (event_id, event_lines[event_id], line_number))
                event_lines[event_id] = line_number
                records.append(record)
    return records


def new_usage_records(records, baseline_event_ids):
    return [r for r in records if r["event_id"] not in baseline_event_ids]


def _shell_directory(path):
    match = re.fullmatch(r"/([A-Za-z])(?:/(.*))?", path)
    if match:
        path = match.group(1).upper() + ":/" + (match.group(2) or "")
    return os.path.normcase(os.path.abspath(path))


def tokencost_invocation(record):
    if record["tool"] not in ("Bash", "PowerShell"):
        return None
    command = str(record["arguments"].get("command") or "")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&":
        if _shell_directory(tokens[1]) != _shell_directory(ROOT):
            return None
        tokens = tokens[3:]
    if not tokens:
        return None
    script = tokens[0].strip('"')
    if script in ("tokencost", "tokencost.cmd") or script.endswith(("/tokencost", "\\tokencost",
                                                                      "/tokencost.cmd", "\\tokencost.cmd")):
        arguments = tokens[1:]
    elif tokens[0] in ("bash", "sh") and len(tokens) >= 2:
        script = tokens[1].strip('"')
        if not (script == "tokencost" or script.endswith(("/tokencost", "\\tokencost"))):
            return None
        arguments = tokens[2:]
    else:
        if len(tokens) < 2:
            return None
        launcher = tokens[0].strip('"').lower().replace("/", "\\").rsplit("\\", 1)[-1]
        if launcher not in ("python", "python.exe", "python3", "python3.exe", "py", "py.exe"):
            return None
        script = tokens[1].strip('"')
        if not (script == "tokencost.py" or script.endswith("/tokencost.py")
                or script.endswith("\\tokencost.py")):
            return None
        arguments = tokens[2:]
    if not arguments:
        return None
    command_name = arguments[0]
    if command_name == "start" and len(arguments) == 1:
        return command_name
    if command_name == "settle" and arguments[1:] in ([], ["--json"]):
        return command_name
    if command_name == "report" and arguments[1:] in ([], ["--json"]):
        return command_name
    if command_name == "finish" and len(arguments) == 3 and arguments[1] == "--answer":
        return command_name
    return None


def is_management_record(record):
    return tokencost_invocation(record) in ("start", "report", "settle")


def is_finish_record(record):
    return tokencost_invocation(record) == "finish"


def call_purpose(record):
    if record["tool"] == "Read":
        return "Read 固定题目" if record["arguments"].get("file_path") else "Read 文件"
    if is_finish_record(record):
        return "finish 固定答案"
    if record.get("event_type") == "message":
        return "assistant 回答"
    invocation = tokencost_invocation(record)
    if invocation:
        return invocation + " 管理调用"
    return record["tool"] or "未知调用"


def validate_flow(records, workload_path):
    violations = []
    measured = [r for r in records if not is_management_record(r)]
    reads = [r for r in measured if r["tool"] == "Read"]
    finishes = [r for r in measured if is_finish_record(r)]
    extras = [r for r in measured if r not in reads and r not in finishes]
    if extras:
        violations.append("检测到非流程操作：%s" % ", ".join(
            "%s/%s" % (r["tool"], r["event_id"]) for r in extras))
    if len(reads) != 1 or len(finishes) != 1 or len(measured) != 2:
        violations.append("固定流程必须恰好包含一次 Read 和一次 finish，实际为 %d/%d"
                          % (len(reads), len(finishes)))
    if reads:
        arguments = reads[0]["arguments"]
        path_keys = [key for key in ("file_path", "path") if key in arguments]
        actual = str(arguments.get(path_keys[0]) or "") if len(path_keys) == 1 else ""
        if os.path.normcase(os.path.abspath(actual)) != os.path.normcase(
                os.path.abspath(workload_path)):
            violations.append("Read 未读取固定题目：%s" % (actual or "空路径"))
        if len(path_keys) != 1 or set(arguments) - set(path_keys):
            violations.append("Read 必须单次完整读取，不允许 offset、limit 或其他分页参数")
    if reads and finishes:
        read_index = measured.index(reads[0])
        finish_index = measured.index(finishes[0])
        if read_index > finish_index:
            violations.append("操作顺序错误：finish 必须在 Read 之后")
    models = {r["model"] for r in records if r["model"]}
    if len(models) > 1:
        violations.append("测量期间模型发生切换：%s" % ", ".join(sorted(models)))
    requested_models = {r.get("requested_model") for r in records if r.get("requested_model")}
    if len(requested_models) > 1:
        violations.append("测量期间请求路由发生切换：%s" % ", ".join(sorted(requested_models)))
    for record in records:
        violations.extend(record_validation_errors(record, require_credit=False))
    return violations


def aggregate_usage(records):
    fields = ("input", "input_hit", "input_miss", "cache_write", "billed_output",
              "reasoning_output", "visible_output")
    result = {key: sum(r[key] for r in records) for key in fields}
    result["calls"] = len(records)
    result["credit_complete"] = all(r["credit"] is not None for r in records)
    result["credit_valid"] = all(
        r["credit"] is not None and math.isfinite(r["credit"]) and r["credit"] >= 0
        for r in records)
    result["credit"] = math.fsum(
        r["credit"] for r in records
        if r["credit"] is not None and math.isfinite(r["credit"]))
    result["models"] = sorted({r["model"] for r in records if r["model"]})
    return result


def deepseek_regime(timestamp_ms):
    moment = dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.timezone.utc)
    if moment.weekday() < 5 and (1 <= moment.hour < 4 or 6 <= moment.hour < 10):
        return "peak"
    return "offpeak"


def estimate_record_cny(record, price, fx_cny_per_usd=None):
    if price["currency"] == "USD":
        if not fx_cny_per_usd:
            raise MeasurementError("美元刊例价缺少人民币换算汇率")
        rates = price[record.get("regime") or deepseek_regime(record["timestamp_ms"])]
        fx = decimal.Decimal(str(fx_cny_per_usd))
    else:
        rates = price
        fx = decimal.Decimal("1")
    write_rate = rates.get("cache_write_5m", price.get("cache_write_5m", 0.0))
    native = (
        decimal.Decimal(record["input_miss"]) * decimal.Decimal(str(rates["input_miss"]))
        + decimal.Decimal(record["input_hit"]) * decimal.Decimal(
            str(rates.get("input_hit", rates["input_miss"])))
        + decimal.Decimal(record["billed_output"]) * decimal.Decimal(str(rates["output"]))
        + decimal.Decimal(record["cache_write"]) * decimal.Decimal(str(write_rate))
    ) / decimal.Decimal(1_000_000)
    return float(native * fx)


def compute_discount(official_cny, credit_delta, credit_unit_cny):
    if not all(math.isfinite(value) for value in
               (official_cny, credit_delta, credit_unit_cny)):
        raise MeasurementError("价格与 credit 必须是有限数值")
    if official_cny <= 0:
        raise MeasurementError("官方预估价必须大于零")
    if credit_delta < 0:
        raise MeasurementError("credit 合计不能为负数")
    if credit_unit_cny < 0:
        raise MeasurementError("credit 单价不能为负数")
    official = decimal.Decimal(str(official_cny))
    credit_cost = decimal.Decimal(str(credit_delta)) * decimal.Decimal(str(credit_unit_cny))
    ratio = credit_cost / official
    return {"official_cny": official_cny, "credit_cost_cny": float(credit_cost),
            "cost_ratio": float(ratio), "discount_zhe": float(ratio * 10),
            "savings_ratio": float(1 - ratio)}


def workload_cost_metrics(official_cny, credit_cost_cny, billed_tokens):
    if billed_tokens <= 0:
        raise MeasurementError("计费 token 总量必须大于零")
    scale = decimal.Decimal(1_000_000) / decimal.Decimal(billed_tokens)
    credit_per_million = (float(decimal.Decimal(str(credit_cost_cny)) * scale)
                          if credit_cost_cny is not None else None)
    return {
        "billed_tokens": billed_tokens,
        "token_basis": "输入总量 + 计费输出；缓存写入不重复计入，仅适用于本次负载结构",
        "official_cny_per_million_tokens": float(
            decimal.Decimal(str(official_cny)) * scale),
        "credit_cost_cny_per_million_tokens": credit_per_million,
    }


def fetch_text_snapshot(url, evidence_dir=None, evidence_name="response"):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_PRICE_HOSTS:
        raise MeasurementError("禁止访问非官方白名单地址：%s" % url)
    request = urllib.request.Request(url, headers={
        "User-Agent": "tokencost/3.0 (+official-price-check)",
        "Referer": parsed.scheme + "://" + parsed.netloc + "/",
    })
    opener = urllib.request.build_opener(WhitelistRedirectHandler())
    try:
        with opener.open(request, timeout=40) as response:
            final_url = response.geturl()
            final = urllib.parse.urlparse(final_url)
            if final.scheme != "https" or final.hostname not in ALLOWED_PRICE_HOSTS:
                raise MeasurementError("官方页面跳转到了非白名单主机：%s" % final.hostname)
            body = response.read(20_000_001)
            if len(body) > 20_000_000:
                raise MeasurementError("官方价格页超过 20 MB，拒绝解析：%s" % url)
    except urllib.error.HTTPError as exc:
        raise PriceHTTPError(exc.code, url) from exc
    except urllib.error.URLError as exc:
        raise MeasurementError("无法联网读取官方价格页：%s" % url) from exc
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MeasurementError("官方价格页不是有效 UTF-8：%s" % url) from exc
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    evidence = {
        "source_url": url, "final_url": final_url, "fetched_at": fetched_at,
        "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
    }
    if evidence_dir:
        os.makedirs(evidence_dir, exist_ok=True)
        safe_name = re.sub(r"[^0-9A-Za-z_.-]+", "-", evidence_name).strip("-") or "response"
        content_name = safe_name + ".txt"
        content_path = os.path.join(evidence_dir, content_name)
        temporary = content_path + ".tmp"
        with open(temporary, "wb") as handle:
            handle.write(body)
        os.replace(temporary, content_path)
        evidence["content_file"] = content_name
        evidence["metadata_file"] = safe_name + ".json"
        save_json(os.path.join(evidence_dir, evidence["metadata_file"]), evidence)
    return text, evidence


def fetch_text(url):
    return fetch_text_snapshot(url)[0]


def _attach_price_evidence(price, evidence):
    price["evidence"] = evidence
    price["fetched_at"] = evidence["fetched_at"]
    return price


def resolve_pricing_model(actual_model, requested_models):
    actual = actual_model.lower()
    if actual in SUPPORTED_PRICE_MODELS:
        return {"pricing_model": actual, "basis": "实际模型 ID 有直接官方解析器"}
    requested = {value.lower() for value in requested_models if value}
    if len(requested) == 1:
        candidate = next(iter(requested))
        if (candidate in SUPPORTED_PRICE_MODELS
                and re.fullmatch(re.escape(candidate) + r"-[0-9A-Za-z][0-9A-Za-z.-]*", actual)):
            return {"pricing_model": candidate,
                    "basis": "实际模型是用户所选官方型号的版本后缀；按所选型号刊例价估算"}
    for candidate in sorted(SUPPORTED_PRICE_MODELS, key=len, reverse=True):
        if re.fullmatch(re.escape(candidate) + r"-[0-9A-Za-z][0-9A-Za-z.-]*", actual):
            return {"pricing_model": candidate,
                    "basis": "实际模型 ID 是官方型号的版本后缀；自定义路由名称未参与取价"}
    return {"pricing_model": actual, "basis": "无可验证的官方型号映射"}


def fetch_model_price(model_id, evidence_dir=None):
    normalized = model_id.lower()
    if normalized in KIMI_MODELS:
        url, parser, name = KIMI_PRICE_URL, parse_kimi_price, "model-" + normalized
    elif normalized in DEEPSEEK_MODEL_KIND:
        url, parser, name = DEEPSEEK_PRICE_URL, parse_deepseek_price, "model-" + normalized
    elif normalized in GLM_MODELS:
        url, parser, name = GLM_PRICE_URL, parse_glm_price, "model-" + normalized
    elif normalized in QWEN_MODEL_PAGES:
        url = QWEN_PRICE_URL.format(model_page=QWEN_MODEL_PAGES[normalized])
        parser, name = parse_qwen_price, "model-" + normalized
    elif normalized in MINIMAX_MODELS:
        url, parser, name = MINIMAX_PRICE_URL, parse_minimax_price, "model-" + normalized
    else:
        raise MeasurementError("暂未内置模型 %s 的官方定价解析器；不会猜测或套用别家价格" % model_id)
    text, evidence = fetch_text_snapshot(url, evidence_dir, name)
    return _attach_price_evidence(parser(text, normalized), evidence)


def fetch_credit_price(evidence_dir=None):
    text, evidence = fetch_text_snapshot(WORKBUDDY_PRICE_URL, evidence_dir, "workbuddy-credit")
    return _attach_price_evidence(parse_workbuddy_credit_price(text), evidence)


def fetch_usd_cny(on_date=None, evidence_dir=None):
    start_date = on_date or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    for days in range(10):
        day = start_date - dt.timedelta(days=days)
        url = ("https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/"
               "ccpr-notice%s.json" % day.isoformat())
        try:
            text, evidence = fetch_text_snapshot(
                url, evidence_dir, "usd-cny-" + day.isoformat())
            value = parse_usd_cny(text)
            return _attach_price_evidence(
                {"cny_per_usd": value, "date": day.isoformat(), "official_url": url}, evidence)
        except PriceHTTPError as exc:
            if exc.status != 404:
                raise
    raise MeasurementError("从 %s 向前 10 天没有可用的官方美元兑人民币中间价" % start_date)


def current_adapter():
    try:
        return client_adapters.detect_adapter()
    except client_adapters.AdapterError as exc:
        raise MeasurementError(str(exc)) from exc


def find_transcript(adapter=None):
    adapter = adapter or current_adapter()
    try:
        return adapter.find_transcript()
    except client_adapters.AdapterError as exc:
        raise MeasurementError(str(exc)) from exc


def read_current_context(transcript=None):
    adapter = current_adapter()
    session_id = adapter.session_id()
    if session_id:
        import sqlite3
        for name in adapter.context_dirs:
            path = os.path.join(os.path.expanduser("~"), name, "workbuddy.db")
            if not os.path.exists(path):
                continue
            try:
                con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
                row = con.execute("select used,size from session_usage where session_id=?",
                                  (session_id,)).fetchone()
                con.close()
                if row:
                    return {"used": int(row[0]), "size": int(row[1]),
                            "source": os.path.join(name, "workbuddy.db")}
            except Exception:
                continue
    if transcript and os.path.exists(transcript):
        records = read_usage_records(transcript)
        if records:
            latest = max(records, key=lambda record: record["timestamp_ms"])
            if latest["input"] > 0:
                return {"used": latest["input"], "size": None,
                        "source": "当前 transcript 最新 rawUsage.prompt_tokens；上下文总大小未知"}
    return None


def load_spec():
    with open(SPEC_PATH, encoding="utf-8") as handle:
        spec = json.load(handle)
    path = os.path.join(ROOT, spec["input_file"])
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    if digest != spec["sha256"]:
        raise MeasurementError("固定题目文件哈希不一致，拒绝继续")
    return spec, os.path.abspath(path)


def load_state(required=True):
    if not os.path.exists(STATE_PATH):
        if required:
            raise MeasurementError("尚未 start，不能跳过固定流程")
        return None
    with open(STATE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(temporary, path)


def verify_measurement_binding(state):
    adapter = current_adapter()
    if state.get("client") != adapter.name:
        raise MeasurementError("当前客户端与 start 绑定的客户端不一致")
    if adapter.session_id() != state.get("session_id"):
        raise MeasurementError("当前会话与 start 绑定的会话不一致")
    if adapter.name != "fixture" and find_transcript(adapter) != state.get("transcript"):
        raise MeasurementError("当前 transcript 与 start 绑定的记录不一致")
    expected_size = state.get("baseline_transcript_bytes")
    expected_hash = state.get("baseline_transcript_sha256")
    if expected_size is None or not expected_hash:
        raise MeasurementError("测量基准缺少 transcript 前缀校验，请在新会话重新测量")
    with open(state["transcript"], "rb") as handle:
        prefix = handle.read(expected_size)
    if len(prefix) != expected_size or hashlib.sha256(prefix).hexdigest() != expected_hash:
        raise MeasurementError("transcript 基准前缀发生变化，不能继续使用该测量")


def read_request_credit_ledger(state, request_id):
    adapter = current_adapter()
    for name in adapter.context_dirs:
        path = os.path.join(os.path.expanduser("~"), name, "workbuddy.db")
        if not os.path.exists(path):
            continue
        import sqlite3
        try:
            con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
            row = con.execute("select credit_json from session_usage where session_id=?",
                              (state["session_id"],)).fetchone()
            con.close()
        except Exception:
            continue
        if not row or not row[0]:
            continue
        try:
            payload = json.loads(row[0])
            value = float(payload[request_id])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if math.isfinite(value) and value >= 0:
            return {"credit": value, "source": os.path.join(name, "workbuddy.db")}
    return None


def run_directory(state):
    run_id = state.get("run_id")
    if not run_id or not re.fullmatch(r"[0-9A-Za-z-]+", run_id):
        raise MeasurementError("测量状态缺少有效 run_id")
    return os.path.join(os.path.dirname(REPORT_PATH), "runs", run_id)


def cmd_start(args):
    existing = load_state(required=False)
    if existing and existing.get("status") in ("started", "finished"):
        raise MeasurementError("已有一次未完成的测量；不要重新开始或覆盖基准")
    spec, workload = load_spec()
    adapter = current_adapter()
    transcript = find_transcript(adapter)
    baseline = read_usage_records(transcript)
    with open(transcript, "rb") as handle:
        baseline_bytes = handle.read()
    context = read_current_context(transcript)
    if context is None and not os.environ.get("TOKENSCOST_TEST_TRANSCRIPT"):
        raise MeasurementError("无法读取当前会话上下文用量，不能确认测量基准是否受控")
    if context:
        if context["used"] > MAX_BASELINE_CONTEXT:
            raise MeasurementError("当前会话已有 %s token，超过 %s；请用新会话避免历史上下文影响"
                                   % (context["used"], MAX_BASELINE_CONTEXT))
        if context.get("size") is not None and context["size"] - context["used"] < MIN_FREE_CONTEXT:
            raise MeasurementError("当前会话剩余上下文不足 %s token" % MIN_FREE_CONTEXT)
    state = {
        "version": REPORT_VERSION, "status": "started",
        "run_id": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S-")
                  + uuid.uuid4().hex[:10],
        "session_id": adapter.session_id(),
        "client": adapter.name,
        "measurement_request_id": next(
            (record.get("request_id") for record in reversed(baseline)
             if tokencost_invocation(record) == "start" and record.get("request_id")), ""),
        "transcript": transcript, "baseline_event_ids": [r["event_id"] for r in baseline],
        "baseline_transcript_bytes": len(baseline_bytes),
        "baseline_transcript_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "baseline_context": context, "started_at": int(time.time()),
        "workload": workload, "workload_sha256": spec["sha256"], "answer": None,
    }
    save_json(STATE_PATH, state)
    print("基准已建立，已有 %d 条调用记录被排除。" % len(baseline))
    print("接下来只允许两步，不要联网、不要读其他文件、不要在聊天中解释：")
    print("1) 使用 Read 完整读取：%s" % workload)
    print("2) 根据文中【终稿附注】直接执行：")
    print("   bash ./tokencost finish --answer '<合格率>|<主检人>'")
    print("然后执行 bash ./tokencost report。流程或答案失败不要重试；仅官方取价失败可重试 report。")


def cmd_finish(args):
    state = load_state()
    if state.get("status") != "started":
        raise MeasurementError("finish 只能在 start 后执行一次，不能重试或补跑")
    answer = args.answer.strip()
    if not answer or len(answer) > 100:
        raise MeasurementError("答案为空或过长")
    state["answer"] = answer
    state["status"] = "finished"
    state["finished_at"] = int(time.time())
    save_json(STATE_PATH, state)
    print("答案已固定，不能修改。下一步仅执行：bash ./tokencost report")


def persist_report(state, report):
    save_json(REPORT_PATH, report)
    save_json(os.path.join(run_directory(state), "report.json"), report)


def invalid_report(state, records, violations, json_output=False):
    measured = [r for r in records if not is_management_record(r)]
    management = [r for r in records if is_management_record(r)]
    serialized_measured = []
    for record in measured:
        item = dict(record)
        item["purpose"] = call_purpose(record)
        if item["credit"] is not None and not math.isfinite(item["credit"]):
            item["credit"] = repr(item["credit"])
        serialized_measured.append(item)
    report = {
        "report_version": REPORT_VERSION, "run_id": state["run_id"], "valid": False,
        "flow_valid": False, "token_price_valid": False, "credit_discount_valid": False,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "violations": violations,
        "usage": aggregate_usage(measured) if measured else None,
        "management_usage": aggregate_usage(management) if management else None,
        "record_ids": [r["event_id"] for r in measured],
        "management_record_ids": [r["event_id"] for r in management],
        "per_call": serialized_measured,
        "scope": "usage 仅包含受控调用；management_usage 单独列出，不计算价格或折扣",
    }
    persist_report(state, report)
    state["status"] = "invalid"
    state["violations"] = violations
    save_json(STATE_PATH, state)
    if not json_output:
        print("测量无效，未计算价格或折扣率：")
        for item in violations:
            print("  - " + item)
        if measured:
            usage = report["usage"]
            credit_text = ("%.4f" % usage["credit"]
                           if usage["credit_complete"] and usage["credit_valid"] else "不可用")
            print("原始记录仍保留：输入 %s，计费输出 %s，credit %s"
                  % (usage["input"], usage["billed_output"], credit_text))
        print("不要通过重试挑选结果；需要新测量时请在新会话重新 start。")
    return report


def cmd_report(args):
    state = load_state()
    if state.get("status") != "finished":
        raise MeasurementError("report 只能在 finish 后执行；取价失败时可重试，无效或已完成报告不可重跑")
    verify_measurement_binding(state)
    records = new_usage_records(read_usage_records(state["transcript"]),
                                set(state["baseline_event_ids"]))
    violations = validate_flow(records, state["workload"])
    spec, _ = load_spec()
    answer = state.get("answer") or ""
    if answer.count("|") != 1 or not all(x in answer for x in spec["expected_contains"]):
        violations.append("答案不符合固定格式或检索结果不正确：%s" % answer)
    if violations:
        report = invalid_report(state, records, violations, json_output=args.json)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        raise SystemExit(3)

    measured = [r for r in records if not is_management_record(r)]
    management = [r for r in records if is_management_record(r)]
    usage = aggregate_usage(measured)
    credit_available = usage["credit_complete"] and usage["credit_valid"]
    credit_issues = []
    if not credit_available:
        for record in measured:
            credit_issues.extend(record_validation_errors(record, require_credit=True))
    model = usage["models"][0]
    pricing_resolution = resolve_pricing_model(
        model, {record.get("requested_model") for record in measured})
    evidence_dir = os.path.join(run_directory(state), "price-evidence")
    model_price = fetch_model_price(pricing_resolution["pricing_model"], evidence_dir=evidence_dir)
    model_price["actual_model"] = model
    model_price["pricing_basis"] = pricing_resolution["basis"]
    credit_price = fetch_credit_price(evidence_dir=evidence_dir)
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    model_price.setdefault("fetched_at", generated_at)
    credit_price.setdefault("fetched_at", generated_at)
    fx_by_date = {}
    if model_price["currency"] == "USD":
        for record in measured:
            call_date = dt.datetime.fromtimestamp(
                record["timestamp_ms"] / 1000, tz=dt.timezone.utc).astimezone(
                    dt.timezone(dt.timedelta(hours=8))).date()
            if call_date not in fx_by_date:
                fx_by_date[call_date] = fetch_usd_cny(
                    on_date=call_date, evidence_dir=evidence_dir)
    per_call = []
    for record in measured:
        regime = (deepseek_regime(record["timestamp_ms"])
                  if model_price["currency"] == "USD" else "flat")
        call_date = dt.datetime.fromtimestamp(
            record["timestamp_ms"] / 1000, tz=dt.timezone.utc).astimezone(
                dt.timezone(dt.timedelta(hours=8))).date()
        fx = fx_by_date.get(call_date)
        official = estimate_record_cny(
            record, model_price, fx["cny_per_usd"] if fx else None)
        item = dict(record)
        item["purpose"] = call_purpose(record)
        record_credit_valid = (record["credit"] is not None
                               and math.isfinite(record["credit"]) and record["credit"] >= 0)
        call_credit_cost = (record["credit"] * credit_price["unit_cny"]
                            if record_credit_valid else None)
        if item["credit"] is not None and not math.isfinite(item["credit"]):
            item["credit"] = repr(item["credit"])
        call_metrics = workload_cost_metrics(
            official, call_credit_cost, record["input"] + record["billed_output"])
        item.update({"regime": regime, "official_cny": official,
                     "credit_recharge_cost_cny": call_credit_cost,
                     "official_cny_per_million_tokens":
                         call_metrics["official_cny_per_million_tokens"],
                     "credit_cost_cny_per_million_tokens":
                         call_metrics["credit_cost_cny_per_million_tokens"],
                     "fx_date": fx["date"] if fx else None})
        per_call.append(item)
    official_cny = sum(item["official_cny"] for item in per_call)
    discount = (compute_discount(official_cny, usage["credit"], credit_price["unit_cny"])
                if credit_available else None)
    cost_metrics = workload_cost_metrics(
        official_cny, discount["credit_cost_cny"] if discount else None,
        usage["input"] + usage["billed_output"])
    list_equivalent = (official_cny / usage["credit"]
                       if credit_available and usage["credit"] > 0 else None)
    fx_values = list(fx_by_date.values())
    report = {
        "report_version": REPORT_VERSION, "run_id": state["run_id"],
        "valid": credit_available,
        "flow_valid": True, "token_price_valid": True,
        "credit_discount_valid": credit_available,
        "violations": credit_issues,
        "generated_at": generated_at, "model": model,
        "pricing_model": pricing_resolution["pricing_model"],
        "usage": usage,
        "official_estimate_cny": official_cny,
        "billed_tokens": cost_metrics["billed_tokens"],
        "token_basis": cost_metrics["token_basis"],
        "official_cny_per_million_tokens": cost_metrics["official_cny_per_million_tokens"],
        "credit_cost_cny_per_million_tokens":
            cost_metrics["credit_cost_cny_per_million_tokens"],
        "reported_credit_sum": usage["credit"] if credit_available else None,
        "credit_evidence": {"source": "providerData.rawUsage.credit",
                            "complete": credit_available,
                            "record_ids": [r["event_id"] for r in measured]},
        "credit_unit_cny": credit_price["unit_cny"],
        "credit_recharge_cost_cny": discount["credit_cost_cny"] if discount else None,
        "recharge_cost_ratio": discount["cost_ratio"] if discount else None,
        "discount_zhe": discount["discount_zhe"] if discount else None,
        "savings_ratio": discount["savings_ratio"] if discount else None,
        "list_equivalent_value_cny_per_credit": list_equivalent,
        "model_price": model_price, "credit_price": credit_price,
        "fx": fx_values[0] if len(fx_values) == 1 else fx_values,
        "per_call": per_call, "baseline_context": state.get("baseline_context"),
        "record_ids": [r["event_id"] for r in measured],
        "management_record_ids": [r["event_id"] for r in management],
        "scope": "仅当前会话的受控新增调用；管理调用单列；credit 为逐调用上报合计，不是账户余额差",
        "interpretation": "加量包补充成本与厂商刊例估算价的比较；每百万 token 现金成本仅适用于本次输入/输出/缓存结构，不代表统一模型单价或 WorkBuddy 实际采购价",
    }
    persist_report(state, report)
    state["status"] = "done" if credit_available else "partial"
    state["violations"] = credit_issues
    save_json(STATE_PATH, state)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return
    if credit_available:
        print("测量有效：%s ｜ %d 次受控调用" % (model, usage["calls"]))
    else:
        print("流程与 Token 价格有效：%s ｜ credit 折扣不可用" % model)
    print("\nToken 分项")
    print("  普通输入（缓存未命中） %s" % format(usage["input_miss"], ","))
    print("  缓存命中输入           %s" % format(usage["input_hit"], ","))
    print("  缓存写入               %s" % format(usage["cache_write"], ","))
    print("  可见输出               %s" % format(usage["visible_output"], ","))
    print("  推理输出               %s" % format(usage["reasoning_output"], ","))
    print("  计费输出（含推理）     %s" % format(usage["billed_output"], ","))
    print("  输入+计费输出合计      %s" % format(usage["input"] + usage["billed_output"], ","))
    print("\n受控调用明细（start/report 均为管理调用，不计入）")
    for index, item in enumerate(per_call, 1):
        credit_text = ("%.4f" % item["credit"]
                       if isinstance(item["credit"], (int, float))
                       and math.isfinite(item["credit"]) else "不可用")
        print("  %d) %s：输入 %s，计费输出 %s，credit %s，刊例 ¥%.6f"
              % (index, item["purpose"], format(item["input"], ","),
                 format(item["billed_output"], ","), credit_text, item["official_cny"]))
    context = state.get("baseline_context")
    if context:
        size_text = format(context["size"], ",") if context.get("size") is not None else "未知"
        print("\nstart 时上下文基准：%s / %s token"
              % (format(context["used"], ","), size_text))
        print("  既有系统/历史上下文会进入后续实际计费，不从 token 或 credit 中扣除")
    print("\n厂商官方刊例价")
    print("  来源：%s" % model_price["official_url"])
    print("  抓取：%s" % model_price["fetched_at"])
    print("  预估价：¥%.6f" % official_cny)
    print("  本次负载刊例均价：¥%.6f / 百万 token"
          % cost_metrics["official_cny_per_million_tokens"])
    if pricing_resolution["pricing_model"] != model:
        print("  取价型号：%s（%s）"
              % (pricing_resolution["pricing_model"], pricing_resolution["basis"]))
    for fx in fx_values:
        print("  汇率：1 USD = %.4f CNY（%s，中国外汇交易中心）"
              % (fx["cny_per_usd"], fx["date"]))
    print("\n本地逐调用 credit 上报合计（非账户余额差）")
    print("  官方加量包单价：¥%.4f/credit（%s）"
          % (credit_price["unit_cny"], credit_price["official_url"]))
    if credit_available:
        print("  消耗：%.4f credit" % usage["credit"])
        print("  加量包补充成本：¥%.6f" % discount["credit_cost_cny"])
        print("  本次负载等效现金均价：¥%.6f / 百万 token"
              % cost_metrics["credit_cost_cny_per_million_tokens"])
        print("  口径：%s" % cost_metrics["token_basis"])
        print("\n补充成本比（不是 WorkBuddy 官方折扣）")
        print("  加量包补充成本 ÷ 官方刊例预估价 = %.2f%%" % (discount["cost_ratio"] * 100))
        if discount["cost_ratio"] <= 1:
            print("  按此口径相当于 %.2f 折；差额比例 %.2f%%"
                  % (discount["discount_zhe"], discount["savings_ratio"] * 100))
        else:
            print("  加量包补充成本高于厂商刊例预估价")
        if list_equivalent is not None:
            print("  刊例等效价值：¥%.6f/credit（不是现金面值）" % list_equivalent)
    else:
        print("  credit 数据不完整，credit 价值和折扣均为空；未按 0 处理")
        for issue in credit_issues:
            print("  - " + issue)
    print("\n口径：%s" % report["scope"])
    print("档案：%s" % os.path.join(run_directory(state), "report.json"))
    if credit_available and state.get("measurement_request_id"):
        print("如需与 WorkBuddy 完整请求显示值对账，请在下一轮用户消息中执行：bash ./tokencost settle")
        print("settle 将纳入本轮最终回答并读取已结算账本；当前 report 不包含未来回答")


def cmd_settle(args):
    state = load_state()
    if state.get("status") not in ("done", "partial"):
        raise MeasurementError("settle 只能在 report 后、下一轮用户请求中执行")
    verify_measurement_binding(state)
    request_id = state.get("measurement_request_id")
    if not request_id:
        raise MeasurementError("start 未记录 measurement_request_id，不能结算完整请求")
    with open(os.path.join(run_directory(state), "report.json"), encoding="utf-8") as handle:
        controlled = json.load(handle)
    if not controlled.get("flow_valid") or not controlled.get("token_price_valid"):
        raise MeasurementError("原受控报告无效，不能结算")
    records = [record for record in read_usage_records(state["transcript"])
               if record.get("request_id") == request_id]
    if not records:
        raise MeasurementError("当前 transcript 中找不到本次请求的完整调用")
    models = {record["model"] for record in records}
    if models != {controlled["model"]}:
        raise MeasurementError("完整请求内实际模型不一致，禁止合并结算")
    if not all(record["credit"] is not None and math.isfinite(record["credit"])
               and record["credit"] >= 0 for record in records):
        raise MeasurementError("完整请求仍有调用缺少合法 credit，不能结算")
    transcript_credit = math.fsum(record["credit"] for record in records)
    ledger = read_request_credit_ledger(state, request_id)
    if ledger and abs(ledger["credit"] - transcript_credit) > 0.011:
        raise MeasurementError("账本 credit %.4f 与 transcript 合计 %.4f 不一致"
                               % (ledger["credit"], transcript_credit))
    price = controlled["model_price"]
    fx_value = controlled.get("fx")
    fx_values = fx_value if isinstance(fx_value, list) else ([fx_value] if fx_value else [])
    per_call = []
    for record in records:
        call_date = dt.datetime.fromtimestamp(
            record["timestamp_ms"] / 1000, tz=dt.timezone.utc).astimezone(
                dt.timezone(dt.timedelta(hours=8))).date()
        fx = None
        if price["currency"] == "USD":
            eligible = [value for value in fx_values if value["date"] <= call_date.isoformat()]
            if not eligible:
                raise MeasurementError("完整请求调用缺少对应日期的汇率证据")
            fx = max(eligible, key=lambda value: value["date"])
        regime = deepseek_regime(record["timestamp_ms"]) if price["currency"] == "USD" else "flat"
        official = estimate_record_cny(record, price, fx["cny_per_usd"] if fx else None)
        credit_cost = record["credit"] * controlled["credit_unit_cny"]
        metrics = workload_cost_metrics(
            official, credit_cost, record["input"] + record["billed_output"])
        item = dict(record)
        item.update({
            "purpose": call_purpose(record), "regime": regime, "official_cny": official,
            "credit_recharge_cost_cny": credit_cost,
            "official_cny_per_million_tokens": metrics["official_cny_per_million_tokens"],
            "credit_cost_cny_per_million_tokens": metrics["credit_cost_cny_per_million_tokens"],
            "fx_date": fx["date"] if fx else None,
        })
        per_call.append(item)
    usage = aggregate_usage(records)
    official_cny = math.fsum(item["official_cny"] for item in per_call)
    discount = compute_discount(official_cny, transcript_credit, controlled["credit_unit_cny"])
    metrics = workload_cost_metrics(
        official_cny, discount["credit_cost_cny"], usage["input"] + usage["billed_output"])
    report = {
        "report_version": REPORT_VERSION, "run_id": state["run_id"], "kind": "request_settlement",
        "valid": True, "flow_valid": True, "token_price_valid": True,
        "credit_discount_valid": True, "violations": [],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": controlled["model"], "pricing_model": controlled["pricing_model"],
        "usage": usage, "official_estimate_cny": official_cny,
        "billed_tokens": metrics["billed_tokens"], "token_basis": metrics["token_basis"],
        "official_cny_per_million_tokens": metrics["official_cny_per_million_tokens"],
        "credit_cost_cny_per_million_tokens": metrics["credit_cost_cny_per_million_tokens"],
        "reported_credit_sum": transcript_credit,
        "credit_evidence": {
            "source": "同一 conversationRequestId 的完整 transcript",
            "complete": True, "request_id": request_id,
            "ledger_credit": ledger["credit"] if ledger else None,
            "ledger_source": ledger["source"] if ledger else None,
            "ledger_match": ledger is not None,
        },
        "credit_unit_cny": controlled["credit_unit_cny"],
        "credit_recharge_cost_cny": discount["credit_cost_cny"],
        "recharge_cost_ratio": discount["cost_ratio"],
        "discount_zhe": discount["discount_zhe"], "savings_ratio": discount["savings_ratio"],
        "list_equivalent_value_cny_per_credit": official_cny / transcript_credit,
        "model_price": controlled["model_price"], "credit_price": controlled["credit_price"],
        "fx": controlled["fx"], "per_call": per_call,
        "record_ids": [record["event_id"] for record in records],
        "controlled_report": "report.json 保留 Read+finish 口径，本文件为完整请求结算",
        "scope": "同一用户请求的全部模型调用，含协议读取、start、report 和最终回答；不含后续 settle 请求",
        "interpretation": "实际 credit 已包含 WorkBuddy 当前模型计费规则；成本比不是平台宣称的模型折扣",
    }
    save_json(REPORT_PATH, report)
    save_json(os.path.join(run_directory(state), "settled-report.json"), report)
    state["status"] = "settled"
    save_json(STATE_PATH, state)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return
    print("完整请求结算有效：%s ｜ %d 次模型调用" % (controlled["model"], usage["calls"]))
    print("Token：输入 %s，缓存命中 %s，缓存未命中 %s，计费输出 %s"
          % (format(usage["input"], ","), format(usage["input_hit"], ","),
             format(usage["input_miss"], ","), format(usage["billed_output"], ",")))
    print("官方预估价：¥%.6f" % official_cny)
    print("实际 credit 合计：%.4f；充值价值：¥%.6f"
          % (transcript_credit, discount["credit_cost_cny"]))
    if ledger:
        print("账本对账：%.4f credit，一致（%s）" % (ledger["credit"], ledger["source"]))
    else:
        print("账本对账：当前不可用；仅使用完整逐调用 credit")
    print("充值价值 ÷ 官方预估价：%.2f%%" % (discount["cost_ratio"] * 100))
    if discount["cost_ratio"] <= 1:
        print("按加量包口径相当于 %.2f 折" % discount["discount_zhe"])
    else:
        print("加量包充值价值高于官方刊例预估价，没有折扣")
    print("范围：%s" % report["scope"])
    print("档案：%s" % os.path.join(run_directory(state), "settled-report.json"))


def cmd_credit_fit(args):
    import creditfit
    try:
        result = creditfit.fit_reports(args.reports)
    except creditfit.FitError as exc:
        raise MeasurementError(str(exc)) from exc
    if args.output:
        save_json(args.output, result)
    creditfit.print_result(result, json_output=args.json)
    if not result["identifiable"]:
        raise SystemExit(3)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError):
                pass
    parser = argparse.ArgumentParser(prog="tokencost", description="当前 agent 的受控 token/credit 自测")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="建立当前会话基准").set_defaults(func=cmd_start)
    finish = sub.add_parser("finish", help="固定答案，只允许一次")
    finish.add_argument("--answer", required=True)
    finish.set_defaults(func=cmd_finish)
    report = sub.add_parser("report", help="联网取价并计算加量包补充成本比")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)
    settle = sub.add_parser("settle", help="下一轮用户请求中对账完整请求的实际 credit")
    settle.add_argument("--json", action="store_true")
    settle.set_defaults(func=cmd_settle)
    fit = sub.add_parser("fit", help="离线拟合多份有效报告的 credit 消耗函数")
    fit.add_argument("reports", nargs="+", help="有效 v3+ report.json")
    fit.add_argument("--json", action="store_true")
    fit.add_argument("--output", help="可选的拟合结果保存路径")
    fit.set_defaults(func=cmd_credit_fit)
    args = parser.parse_args()
    try:
        args.func(args)
    except MeasurementError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"report_version": REPORT_VERSION, "valid": False,
                              "error": {"type": type(exc).__name__, "message": str(exc)}},
                             ensure_ascii=False, indent=2))
        else:
            print("错误：%s" % exc, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
