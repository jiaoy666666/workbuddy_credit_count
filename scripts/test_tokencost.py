# -*- coding: utf-8 -*-
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tokencost as tc
import creditfit as cf


def event(event_id, name="Read", model="kimi-k3.1-pro", credit=0.25,
          prompt=1000, hit=600, miss=400, completion=50, reasoning=20,
          arguments=None, timestamp=1793939746024):
    return {
        "id": event_id,
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(arguments or {}, ensure_ascii=False),
        "timestamp": timestamp,
        "providerData": {
            "requestModelId": model,
            "rawUsage": {
                "prompt_tokens": prompt,
                "prompt_cache_hit_tokens": hit,
                "prompt_cache_miss_tokens": miss,
                "prompt_cache_write_tokens": 0,
                "completion_tokens": completion,
                "completion_tokens_details": {"reasoning_tokens": reasoning},
                "total_tokens": prompt + completion,
                "credit": credit,
            },
        },
    }


class UsageTests(unittest.TestCase):
    def test_normalize_separates_cache_visible_reasoning_and_credit(self):
        rec = tc.normalize_usage(event("e1"))
        self.assertEqual(rec["input"], 1000)
        self.assertEqual(rec["input_hit"], 600)
        self.assertEqual(rec["input_miss"], 400)
        self.assertEqual(rec["visible_output"], 30)
        self.assertEqual(rec["reasoning_output"], 20)
        self.assertEqual(rec["billed_output"], 50)
        self.assertEqual(rec["credit"], 0.25)
        self.assertEqual(rec["model"], "kimi-k3.1-pro")

    def test_normalize_prefers_actual_model_and_preserves_requested_route(self):
        raw = event("route", model="deepseek-v4.1-flash-1017")
        raw["providerData"]["requestModelId"] = "custom-local:deepseek/deepseek-v4.1-flash-1017"
        raw["providerData"]["model"] = "deepseek-v4.1-flash-1017"
        rec = tc.normalize_usage(raw)
        self.assertEqual(rec["model"], "deepseek-v4.1-flash-1017")
        self.assertEqual(rec["requested_model"],
                         "custom-local:deepseek/deepseek-v4.1-flash-1017")

    def test_normalizes_generic_read_and_shell_tool_names(self):
        read = tc.normalize_usage(event("alias-read", "read_file", arguments={
            "filePath": os.path.join(tc.ROOT, "cases", "selftest", "S1min.txt")}))
        shell = tc.normalize_usage(event("alias-shell", "shell", arguments={
            "cmd": "bash ./tokencost report"}))
        self.assertEqual(read["tool"], "Read")
        self.assertIn("file_path", read["arguments"])
        self.assertEqual(shell["tool"], "Bash")
        self.assertEqual(shell["arguments"]["command"], "bash ./tokencost report")

    def test_diff_uses_event_ids_not_conversation_request_id(self):
        old = tc.normalize_usage(event("old"))
        new = tc.normalize_usage(event("new"))
        selected = tc.new_usage_records([old, new], {"old"})
        self.assertEqual([r["event_id"] for r in selected], ["new"])

    def test_record_validation_rejects_inconsistent_usage(self):
        rec = tc.normalize_usage(event("e1", miss=500))
        self.assertTrue(any("不等于输入总量" in error
                            for error in tc.record_validation_errors(rec)))

    def test_record_validation_rejects_reasoning_above_billed_output(self):
        rec = tc.normalize_usage(event("e1", completion=50, reasoning=80))
        self.assertTrue(any("推理输出" in error for error in tc.record_validation_errors(rec)))

    def test_record_validation_rejects_missing_model_and_nonfinite_credit(self):
        missing_model = tc.normalize_usage(event("e1", model=""))
        self.assertTrue(any("模型 ID" in error
                            for error in tc.record_validation_errors(missing_model)))
        bad_credit = tc.normalize_usage(event("e2", credit=float("nan")))
        self.assertTrue(any("有限非负数" in error
                            for error in tc.record_validation_errors(bad_credit)))
        total = tc.aggregate_usage([bad_credit])
        self.assertFalse(total["credit_valid"])
        self.assertEqual(total["credit"], 0)
        json.dumps(total, allow_nan=False)

    def test_read_usage_rejects_missing_and_duplicate_event_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(event(None)) + "\n")
            with self.assertRaisesRegex(tc.MeasurementError, "稳定事件 ID"):
                tc.read_usage_records(path)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(event("same")) + "\n")
                handle.write(json.dumps(event("same")) + "\n")
            with self.assertRaisesRegex(tc.MeasurementError, "事件 ID 重复"):
                tc.read_usage_records(path)

    def test_aggregate_does_not_double_count_reasoning(self):
        total = tc.aggregate_usage([tc.normalize_usage(event("e1")),
                                    tc.normalize_usage(event("e2"))])
        self.assertEqual(total["input"], 2000)
        self.assertEqual(total["billed_output"], 100)
        self.assertEqual(total["visible_output"], 60)
        self.assertEqual(total["reasoning_output"], 40)
        self.assertEqual(total["credit"], 0.5)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tc.ROOT, "cases", "selftest", "S1min.txt")
        self.read = tc.normalize_usage(event(
            "read", "Read", arguments={"file_path": self.path}))
        self.finish = tc.normalize_usage(event(
            "finish", "Bash",
            arguments={"command": "python tokencost.py finish --answer 99.42%|郑文博"}))

    def test_fixed_read_then_finish_is_valid(self):
        self.assertEqual(tc.validate_flow([self.read, self.finish], self.path), [])

    def test_management_start_is_not_measurement(self):
        start = tc.normalize_usage(event(
            "start", "Bash", arguments={"command": "python tokencost.py start"}))
        self.assertTrue(tc.is_management_record(start))
        self.assertFalse(tc.is_management_record(self.finish))

    def test_extra_network_call_invalidates_measurement(self):
        extra = tc.normalize_usage(event(
            "web", "WebSearch", arguments={"query": "unrelated"}))
        violations = tc.validate_flow([self.read, extra, self.finish], self.path)
        self.assertTrue(any("非流程" in v for v in violations))

    def test_wrong_file_invalidates_measurement(self):
        wrong = tc.normalize_usage(event(
            "wrong", "Read", arguments={"file_path": os.path.join(tc.ROOT, "other.txt")}))
        violations = tc.validate_flow([wrong, self.finish], self.path)
        self.assertTrue(any("固定题目" in v for v in violations))

    def test_model_switch_invalidates_measurement(self):
        other = tc.normalize_usage(event(
            "other", "Bash", model="deepseek-v4.1-flash",
            arguments={"command": "python tokencost.py finish --answer x"}))
        violations = tc.validate_flow([self.read, other], self.path)
        self.assertTrue(any("模型发生切换" in v for v in violations))

    def test_partial_read_invalidates_measurement(self):
        partial = tc.normalize_usage(event(
            "partial", "Read", arguments={"file_path": self.path, "offset": 10}))
        violations = tc.validate_flow([partial, self.finish], self.path)
        self.assertTrue(any("完整读取" in v for v in violations))

    def test_compound_command_cannot_hide_as_management(self):
        extra = tc.normalize_usage(event(
            "extra", "Bash",
            arguments={"command": "python tokencost.py start && curl https://example.com"}))
        violations = tc.validate_flow([self.read, extra, self.finish], self.path)
        self.assertTrue(any("非流程" in v for v in violations))

    def test_cd_to_tool_root_is_allowed_for_single_cli_command(self):
        if os.name == "nt":
            shell_root = "/" + tc.ROOT[0].lower() + tc.ROOT[2:].replace("\\", "/")
        else:
            shell_root = tc.ROOT
        finish = tc.normalize_usage(event("cd-finish", "Bash", arguments={
            "command": "cd '%s' && python tokencost.py finish --answer '99.42%%|郑文博'"
                       % shell_root}))
        report = tc.normalize_usage(event("cd-report", "Bash", arguments={
            "command": "cd '%s' && python tokencost.py report" % shell_root}))
        self.assertTrue(tc.is_finish_record(finish))
        self.assertTrue(tc.is_management_record(report))
        other = tc.normalize_usage(event("cd-other", "Bash", arguments={
            "command": "cd /definitely/not-the-tool-root && python tokencost.py report"}))
        self.assertFalse(tc.is_management_record(other))

    def test_portable_launcher_is_accepted_and_abort_is_not_management(self):
        finish = tc.normalize_usage(event("launcher-finish", "Bash", arguments={
            "command": "bash ./tokencost finish --answer '99.42%|郑文博'"}))
        report = tc.normalize_usage(event("launcher-report", "Bash", arguments={
            "command": "./tokencost report"}))
        abort = tc.normalize_usage(event("launcher-abort", "Bash", arguments={
            "command": "python tokencost.py abort"}))
        self.assertTrue(tc.is_finish_record(finish))
        self.assertTrue(tc.is_management_record(report))
        self.assertFalse(tc.is_management_record(abort))

    def test_model_switch_in_management_call_invalidates_measurement(self):
        start = tc.normalize_usage(event(
            "start", "Bash", model="deepseek-v4.1-flash",
            arguments={"command": "python tokencost.py start"}))
        violations = tc.validate_flow([start, self.read, self.finish], self.path)
        self.assertTrue(any("模型发生切换" in v for v in violations))

    def test_one_missing_model_id_invalidates_measurement(self):
        missing = tc.normalize_usage(event(
            "missing", "Bash", model="",
            arguments={"command": "python tokencost.py finish --answer 99.42%|郑文博"}))
        violations = tc.validate_flow([self.read, missing], self.path)
        self.assertTrue(any("缺少实际模型 ID" in v for v in violations))


KIMI_HTML = r'''
rows:[[_jsx(_components.a,{href:`/guide/kimi-k3-1-pro-quickstart`,children:`kimi-k3.1-pro`}),`¥13.00 / MTok`,`¥68.00 / MTok`,`¥1.30 / MTok`,`¥13.00 / MTok`,`¥26.00 / MTok`],[_jsx(_components.a,{href:`/guide/kimi-k3-1-pro-quickstart`,children:`kimi-k3.1-pro-highspeed`}),`¥26.00 / MTok`,`¥136.00 / MTok`,`¥1.30 / MTok`,`¥26.00 / MTok`,`¥52.00 / MTok`]]
'''

DEEPSEEK_HTML = r'''
<table><tr><td>MODEL</td><td>deepseek-flash</td><td>deepseek-v4-pro</td></tr>
<tr><td>PRICING</td><td>1M INPUT TOKENS(CACHE HIT)</td><td>OFF-PEAK</td><td>$0.003</td><td>$0.022</td></tr>
<tr><td>PEAK</td><td>$0.006</td><td>$0.044</td></tr>
<tr><td>1M INPUT TOKENS(CACHE MISS)</td><td>OFF-PEAK</td><td>$0.15</td><td>$0.66</td></tr>
<tr><td>PEAK</td><td>$0.3</td><td>$1.32</td></tr>
<tr><td>1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.6</td><td>$1.98</td></tr>
<tr><td>PEAK</td><td>$1.2</td><td>$3.96</td></tr></table>
'''

WORKBUDDY_HTML = r'''
<table><thead><tr><th>有效期</th><th>加量包规格</th><th>价格</th></tr></thead>
<tbody><tr><td>1 个月</td><td>1,000 积分</td><td>50 元</td></tr></tbody></table>
'''

GLM_API_JSON = r'''
{"code":200,"data":[
 {"operationId":"1160","content":"{\"list\":[{\"title\":\"GLM-5.3\",\"table\":{\"modelList\":[{\"c1\":{\"value\":\"输入单价\"},\"c2\":{\"value\":\"8元 / M\"}},{\"c1\":{\"value\":\"输出单价\"},\"c2\":{\"value\":\"28元 / M\"}},{\"c1\":{\"value\":\"缓存命中\"},\"c2\":{\"value\":\"2元 / M\"}},{\"c1\":{\"value\":\"缓存存储\"},\"c2\":{\"value\":\"限时免费\"}}]}},{\"title\":\"GLM-5.3-Flash\",\"table\":{\"modelList\":[{\"c1\":{\"value\":\"输入单价\"},\"c2\":{\"value\":\"0.8元 / M\"}},{\"c1\":{\"value\":\"输出单价\"},\"c2\":{\"value\":\"2.8元 / M\"}},{\"c1\":{\"value\":\"缓存命中\"},\"c2\":{\"value\":\"0.23元 / M\"}}]}}]}"},
 {"operationId":"1161","content":"{\"tabs\":[{\"cards\":[{\"title\":\"GLM-5.3-FlashX\",\"fieldList\":[{\"label\":\"输入单价\",\"values\":[\"2元\"]},{\"label\":\"输出单价\",\"values\":[\"7元\"]},{\"label\":\"缓存命中\",\"values\":[\"0.57元\"]}]}]}]}"}
]}
'''

QWEN_HTML = r'''
<section><h4>华北2（北京）</h4><table><thead><tr><th>计费项</th><th>价格（元）</th><th>单位</th></tr></thead>
<tbody><tr><td><p>输入</p></td><td><p>12</p></td><td><p>每百万tokens</p></td></tr>
<tr><td><p>输出</p></td><td><p>36</p></td><td><p>每百万tokens</p></td></tr>
<tr><td><p>输入（缓存命中）</p></td><td><p>1.5</p></td><td><p>每百万tokens</p></td></tr>
<tr><td><p>显式缓存创建</p></td><td><p>15</p></td><td><p>每百万tokens</p></td></tr>
<tr><td><p>显式缓存命中</p></td><td><p>1</p></td><td><p>每百万tokens</p></td></tr></tbody></table></section>
<section><h4>新加坡</h4><table><tbody><tr><td>输入</td><td>14.988</td></tr></tbody></table></section>
'''

MINIMAX_HTML = r'''
<table><thead><tr><th>模型</th><th>输入价格<br/>元/百万 tokens</th><th>输出价格</th><th>缓存读取</th></tr></thead>
<tbody><tr><td><strong>MiniMax-M3.1-Flash</strong></td><td>1.00</td><td>4.00</td><td>0.10</td></tr></tbody></table>
'''


class PriceTests(unittest.TestCase):
    def test_parse_kimi_exact_model_not_highspeed(self):
        p = tc.parse_kimi_price(KIMI_HTML, "kimi-k3.1-pro")
        self.assertEqual(p["currency"], "CNY")
        self.assertEqual(p["input_miss"], 13.0)
        self.assertEqual(p["input_hit"], 1.3)
        self.assertEqual(p["output"], 68.0)
        self.assertEqual(p["cache_write_5m"], 13.0)

    def test_parse_deepseek_peak_and_offpeak(self):
        p = tc.parse_deepseek_price(DEEPSEEK_HTML, "deepseek-v4.1-flash")
        self.assertEqual(p["currency"], "USD")
        self.assertEqual(p["offpeak"]["input_hit"], 0.003)
        self.assertEqual(p["peak"]["input_miss"], 0.3)
        self.assertEqual(p["peak"]["output"], 1.2)

    def test_unknown_deepseek_model_is_rejected(self):
        with self.assertRaisesRegex(tc.MeasurementError, "暂未内置"):
            tc.parse_deepseek_price(DEEPSEEK_HTML, "deepseek-unknown")
        with self.assertRaisesRegex(tc.MeasurementError, "暂未内置"):
            tc.fetch_model_price("deepseek-unknown")

    def test_deepseek_peak_boundaries_and_weekend(self):
        def regime(hour, minute=0, day=9):
            moment = dt.datetime(2026, 11, day, hour, minute, tzinfo=dt.timezone.utc)
            return tc.deepseek_regime(int(moment.timestamp() * 1000))
        self.assertEqual(regime(0, 59), "offpeak")
        self.assertEqual(regime(1), "peak")
        self.assertEqual(regime(3, 59), "peak")
        self.assertEqual(regime(4), "offpeak")
        self.assertEqual(regime(6), "peak")
        self.assertEqual(regime(9, 59), "peak")
        self.assertEqual(regime(10), "offpeak")
        self.assertEqual(regime(1, day=8), "offpeak")

    def test_parse_workbuddy_credit_recharge_price(self):
        p = tc.parse_workbuddy_credit_price(WORKBUDDY_HTML)
        self.assertEqual(p["credits"], 1000)
        self.assertEqual(p["price_cny"], 50.0)
        self.assertEqual(p["unit_cny"], 0.05)

    def test_workbuddy_other_package_does_not_replace_1000_credit_package(self):
        other = WORKBUDDY_HTML.replace("1,000 积分", "2,000 积分").replace("50 元", "90 元")
        with self.assertRaisesRegex(tc.MeasurementError, "1,000 积分"):
            tc.parse_workbuddy_credit_price(other)
        ambiguous = WORKBUDDY_HTML.replace("</tbody>", "<tr><td>2 个月</td><td>1,000 积分</td><td>60 元</td></tr></tbody>")
        with self.assertRaisesRegex(tc.MeasurementError, "多个"):
            tc.parse_workbuddy_credit_price(ambiguous)

    def test_kimi_estimate_includes_cache_write_once(self):
        rec = tc.normalize_usage(event("e1"))
        rec["cache_write"] = 100
        p = tc.parse_kimi_price(KIMI_HTML, rec["model"])
        cost = tc.estimate_record_cny(rec, p, fx_cny_per_usd=None)
        expected = (400 * 13 + 600 * 1.3 + 50 * 68 + 100 * 13) / 1e6
        self.assertAlmostEqual(cost, expected, places=12)

    def test_deepseek_estimate_converts_usd_to_cny(self):
        rec = tc.normalize_usage(event("e1", model="deepseek-v4.1-flash",
                                       timestamp=1793939746024))
        rec["regime"] = "peak"
        p = tc.parse_deepseek_price(DEEPSEEK_HTML, rec["model"])
        cost = tc.estimate_record_cny(rec, p, fx_cny_per_usd=6.7168)
        usd = (400 * 0.3 + 600 * 0.006 + 50 * 1.2) / 1e6
        self.assertAlmostEqual(cost, usd * 6.7168, places=12)

    def test_discount_formula_uses_credit_recharge_cost_over_list_price(self):
        result = tc.compute_discount(official_cny=2.0, credit_delta=4.0,
                                     credit_unit_cny=0.05)
        self.assertAlmostEqual(result["credit_cost_cny"], 0.2)
        self.assertAlmostEqual(result["cost_ratio"], 0.1)
        self.assertAlmostEqual(result["discount_zhe"], 1.0)
        self.assertAlmostEqual(result["savings_ratio"], 0.9)

    def test_discount_rejects_nonfinite_values(self):
        with self.assertRaisesRegex(tc.MeasurementError, "有限"):
            tc.compute_discount(float("nan"), 1.0, 0.05)

    def test_workload_per_million_cash_metrics(self):
        result = tc.workload_cost_metrics(official_cny=2.0, credit_cost_cny=0.2,
                                          billed_tokens=2_000_000)
        self.assertAlmostEqual(result["official_cny_per_million_tokens"], 1.0)
        self.assertAlmostEqual(result["credit_cost_cny_per_million_tokens"], 0.1)

    def test_pricing_model_uses_selected_official_base_for_version_suffix(self):
        resolved = tc.resolve_pricing_model(
            "deepseek-v4.1-flash-1017", {"deepseek-v4.1-flash"})
        self.assertEqual(resolved["pricing_model"], "deepseek-v4.1-flash")
        unresolved = tc.resolve_pricing_model(
            "deepseek-v4.1-flash-1017",
            {"custom-local:deepseek/deepseek-v4.1-flash-1017"})
        self.assertEqual(unresolved["pricing_model"], "deepseek-v4.1-flash")
        self.assertIn("自定义路由名称未参与取价", unresolved["basis"])

    def test_parse_official_exchange_rate_json(self):
        raw = json.dumps({"data": {"vo": {"price": "1美元对人民币6.7168元，1欧元对人民币7.5191元"}}},
                         ensure_ascii=False)
        self.assertEqual(tc.parse_usd_cny(raw), 6.7168)

    def test_parse_glm_5_3_and_flash_variants_from_official_config(self):
        p = tc.parse_glm_price(GLM_API_JSON, "glm-5.3")
        self.assertEqual(p["currency"], "CNY")
        self.assertEqual(p["input_miss"], 8.0)
        self.assertEqual(p["input_hit"], 2.0)
        self.assertEqual(p["output"], 28.0)
        self.assertEqual(p["cache_write_5m"], 0.0)

        flash = tc.parse_glm_price(GLM_API_JSON, "GLM-5.3-Flash")
        self.assertEqual(flash["input_miss"], 0.8)
        flashx = tc.parse_glm_price(GLM_API_JSON, "glm-5.3-flashx")
        self.assertEqual(flashx["input_hit"], 0.57)

    def test_parse_qwen_beijing_table_and_ignore_other_regions(self):
        p = tc.parse_qwen_price(QWEN_HTML, "qwen3.8-max")
        self.assertEqual(p["currency"], "CNY")
        self.assertEqual(p["input_miss"], 12.0)
        self.assertEqual(p["input_hit"], 1.5)
        self.assertEqual(p["output"], 36.0)
        self.assertEqual(p["cache_write_5m"], 15.0)
        singapore_at = QWEN_HTML.index('<section><h4>新加坡</h4>')
        reordered = QWEN_HTML[singapore_at:] + QWEN_HTML[:singapore_at]
        reordered_price = tc.parse_qwen_price(reordered, "qwen3.8-max")
        self.assertEqual(reordered_price["input_miss"], 12.0)

    def test_parse_minimax_flash_table(self):
        p = tc.parse_minimax_price(MINIMAX_HTML, "minimax-m3.1-flash")
        self.assertEqual(p["currency"], "CNY")
        self.assertEqual(p["input_miss"], 1.0)
        self.assertEqual(p["input_hit"], 0.1)
        self.assertEqual(p["output"], 4.0)
        self.assertEqual(p["cache_write_5m"], 0.0)

    def test_fetch_text_snapshot_saves_raw_bytes_and_hash(self):
        from unittest import mock

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def geturl(self):
                return tc.KIMI_PRICE_URL

            def read(self, limit):
                return KIMI_HTML.encode("utf-8")

        opener = mock.Mock()
        opener.open.return_value = Response()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tc.urllib.request, "build_opener", return_value=opener):
            text, evidence = tc.fetch_text_snapshot(tc.KIMI_PRICE_URL, tmp, "kimi")
            self.assertEqual(text, KIMI_HTML)
            content_path = os.path.join(tmp, evidence["content_file"])
            with open(content_path, "rb") as handle:
                body = handle.read()
            import hashlib
            self.assertEqual(evidence["sha256"], hashlib.sha256(body).hexdigest())
            self.assertTrue(os.path.exists(os.path.join(tmp, evidence["metadata_file"])))


class HostCompatibilityTests(unittest.TestCase):
    def test_unknown_client_fails_closed(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(tc.MeasurementError, "未识别当前 agent 客户端"):
                tc.current_adapter()

    def test_find_transcript_supports_nested_codebuddy_projects(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".codebuddy", "projects", "nested", "session.jsonl")
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("")
            with mock.patch.dict(os.environ, {"CODEBUDDY_SESSION_ID": "session"}, clear=True), \
                 mock.patch.object(tc.os.path, "expanduser", return_value=tmp):
                self.assertEqual(tc.find_transcript(), os.path.abspath(path))

    def test_context_falls_back_to_current_transcript_prompt_tokens(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(event("current", prompt=12345, hit=2345,
                                              miss=10000)) + "\n")
            with mock.patch.dict(os.environ, {"TOKENSCOST_TEST_TRANSCRIPT": path}, clear=True):
                context = tc.read_current_context(path)
        self.assertEqual(context["used"], 12345)
        self.assertIsNone(context["size"])
        self.assertIn("prompt_tokens", context["source"])


class CreditFitTests(unittest.TestCase):
    @staticmethod
    def observation(event_id, miss, hit, write, output, model="kimi-k3.1-pro"):
        credit = 0.2 + 2 * miss / 1e6 + 0.3 * hit / 1e6 + 5 * write / 1e6 + 8 * output / 1e6
        return {"event_id": event_id, "model": model, "input_miss": miss,
                "input_hit": hit, "cache_write": write, "billed_output": output,
                "credit": credit}

    def independent_observations(self):
        import itertools
        records = []
        for index, values in enumerate(itertools.product((0, 1_000_000), repeat=4)):
            records.append(self.observation("fit-%d" % index, *values))
        return records

    def test_recovers_known_nonnegative_coefficients(self):
        result = cf.fit_observations(self.independent_observations(), model="kimi-k3.1-pro")
        self.assertTrue(result["identifiable"])
        self.assertEqual(result["rank"], 5)
        expected = {"intercept_credit_per_call": 0.2,
                    "input_miss_credit_per_million": 2.0,
                    "input_hit_credit_per_million": 0.3,
                    "cache_write_credit_per_million": 5.0,
                    "billed_output_credit_per_million": 8.0}
        for name, value in expected.items():
            self.assertAlmostEqual(result["coefficients"][name], value, places=8)
        self.assertLess(result["fit"]["rmse_credit"], 1e-8)
        self.assertIsNotNone(result["approximate_95pct_ci"])

    def test_rank_deficient_design_does_not_output_unit_prices(self):
        records = [self.observation("same-%d" % index, 1_000_000, 0, 0, 1000)
                   for index in range(8)]
        result = cf.fit_observations(records)
        self.assertFalse(result["identifiable"])
        self.assertIsNone(result["coefficients"])
        self.assertLess(result["rank"], 5)
        self.assertTrue(any("不能同时识别" in warning for warning in result["warnings"]))

    def test_fit_reports_loads_multiple_valid_v3_reports(self):
        records = self.independent_observations()
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for batch in range(2):
                path = os.path.join(tmp, "report-%d.json" % batch)
                payload = {"valid": True, "report_version": 3,
                           "run_id": "run-%d" % batch, "model": "kimi-k3.1-pro",
                           "credit_unit_cny": 0.05,
                           "per_call": records[batch * 8:(batch + 1) * 8]}
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                paths.append(path)
            result = cf.fit_reports(paths)
            payload.pop("credit_unit_cny")
            with open(paths[-1], "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            partial_unit = cf.fit_reports(paths)
            self.assertNotIn("coefficients_cny", partial_unit)
            self.assertTrue(any("未输出任何现金系数" in warning
                                for warning in partial_unit["warnings"]))
        self.assertTrue(result["identifiable"])
        self.assertEqual(result["observations"], 16)
        self.assertEqual(result["run_ids"], ["run-0", "run-1"])
        self.assertAlmostEqual(
            result["coefficients_cny"]["input_miss_cny_per_million"], 0.1)

    def test_fit_reports_rejects_invalid_mixed_model_and_duplicate_events(self):
        records = self.independent_observations()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.json")
            payload = {"valid": True, "report_version": 3, "run_id": "run",
                       "model": "kimi-k3.1-pro", "per_call": records}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaisesRegex(cf.FitError, "事件 ID 重复"):
                cf.fit_reports([path, path])
            other_path = os.path.join(tmp, "other.json")
            other_records = [dict(record, event_id="other-%d" % index,
                                  model="deepseek-v4.1-flash")
                             for index, record in enumerate(records)]
            with open(other_path, "w", encoding="utf-8") as handle:
                json.dump({"valid": True, "report_version": 3, "run_id": "other",
                           "model": "deepseek-v4.1-flash", "per_call": other_records}, handle)
            with self.assertRaisesRegex(cf.FitError, "跨模型"):
                cf.fit_reports([path, other_path])
            payload["valid"] = False
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaisesRegex(cf.FitError, "有效 v3"):
                cf.fit_reports([path])

    def test_tokencost_fit_cli_writes_machine_readable_result(self):
        from types import SimpleNamespace
        records = self.independent_observations()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = os.path.join(tmp, "report.json")
            output_path = os.path.join(tmp, "fit.json")
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump({"valid": True, "report_version": 3, "run_id": "run",
                           "model": "kimi-k3.1-pro", "per_call": records}, handle)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                tc.cmd_credit_fit(SimpleNamespace(
                    reports=[report_path], json=True, output=output_path))
            result = json.loads(output.getvalue())
            self.assertTrue(result["identifiable"])
            with open(output_path, encoding="utf-8") as handle:
                self.assertTrue(json.load(handle)["identifiable"])


class CliFlowTests(unittest.TestCase):
    def test_start_finish_report_writes_valid_discount_report(self):
        import tempfile
        from types import SimpleNamespace
        from unittest import mock
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        transcript = os.path.join(tmp.name, "session.jsonl")
        state = os.path.join(tmp.name, "state.json")
        report_path = os.path.join(tmp.name, "report.json")
        with open(transcript, "w", encoding="utf-8") as handle:
            handle.write('{"id":"old","type":"session-meta"}\n')
        workload = os.path.join(tc.ROOT, "cases", "selftest", "S1min.txt")
        read_event = event("read", "Read", model="kimi-k3.1-pro", credit=0.25,
                           arguments={"file_path": workload})
        finish_event = event("finish", "Bash", model="kimi-k3.1-pro", credit=0.25,
                             arguments={"command": "python tokencost.py finish --answer '99.42%|郑文博'"})
        kimi_price = {"vendor": "Kimi", "model": "kimi-k3.1-pro", "currency": "CNY",
                      "input_miss": 13.0, "output": 68.0, "input_hit": 1.3,
                      "cache_write_5m": 13.0,
                      "official_url": "https://platform.moonshot.cn/pricing"}
        credit_price = {"credits": 1000, "price_cny": 50.0, "unit_cny": 0.05,
                        "currency": "CNY",
                        "official_url": "https://www.workbuddy.cn/docs/workbuddy/Pricing"}
        with mock.patch.dict(os.environ,
                             {"TOKENSCOST_TEST_TRANSCRIPT": transcript}), \
             mock.patch.object(tc, "STATE_PATH", state), \
             mock.patch.object(tc, "REPORT_PATH", report_path), \
             mock.patch.object(tc, "read_current_context", return_value=None), \
             mock.patch.object(tc, "fetch_model_price", return_value=kimi_price), \
             mock.patch.object(tc, "fetch_credit_price", return_value=credit_price), \
             contextlib.redirect_stdout(io.StringIO()):
            tc.cmd_start(SimpleNamespace())
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(read_event, ensure_ascii=False) + "\n")
                handle.write(json.dumps(finish_event, ensure_ascii=False) + "\n")
            tc.cmd_finish(SimpleNamespace(answer="99.42%|郑文博"))
            tc.cmd_report(SimpleNamespace(json=False))
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertTrue(report["valid"])
        self.assertTrue(report["flow_valid"])
        self.assertTrue(report["token_price_valid"])
        self.assertTrue(report["credit_discount_valid"])
        self.assertEqual(report["report_version"], 3)
        self.assertEqual(report["model"], "kimi-k3.1-pro")
        self.assertEqual(report["usage"]["credit"], 0.5)
        self.assertEqual(report["reported_credit_sum"], 0.5)
        self.assertAlmostEqual(report["credit_recharge_cost_cny"], 0.025)
        self.assertEqual(report["billed_tokens"], 2100)
        self.assertAlmostEqual(report["credit_cost_cny_per_million_tokens"],
                               0.025 / 2100 * 1_000_000)
        self.assertAlmostEqual(report["per_call"][0]["credit_recharge_cost_cny"], 0.0125)
        self.assertEqual(report["credit_evidence"]["source"], "providerData.rawUsage.credit")
        self.assertGreater(report["official_estimate_cny"], 0)
        self.assertGreater(report["recharge_cost_ratio"], 0)
        self.assertGreater(report["list_equivalent_value_cny_per_credit"], 0)
        self.assertEqual(report["usage"]["calls"], 2)
        self.assertIn("input_hit", report["per_call"][0])
        self.assertEqual(report["per_call"][0]["purpose"], "Read 固定题目")
        self.assertEqual(report["per_call"][1]["purpose"], "finish 固定答案")
        archive = os.path.join(tmp.name, "runs", report["run_id"], "report.json")
        self.assertTrue(os.path.exists(archive))

    def test_missing_credit_preserves_token_price_and_null_discount(self):
        from types import SimpleNamespace
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "session.jsonl")
            state = os.path.join(tmp, "state.json")
            report_path = os.path.join(tmp, "report.json")
            with open(transcript, "w", encoding="utf-8") as handle:
                handle.write('{"id":"old","type":"session-meta"}\n')
            workload = os.path.join(tc.ROOT, "cases", "selftest", "S1min.txt")
            read_event = event("read", "Read", credit=None,
                               arguments={"file_path": workload})
            finish_event = event("finish", "Bash", arguments={
                "command": "python tokencost.py finish --answer '99.42%|郑文博'"})
            model_price = {"vendor": "Kimi", "model": "kimi-k3.1-pro", "currency": "CNY",
                           "input_miss": 13.0, "output": 68.0, "input_hit": 1.3,
                           "cache_write_5m": 13.0,
                           "official_url": "https://platform.moonshot.cn/pricing"}
            credit_price = {"unit_cny": 0.05,
                            "official_url": "https://www.workbuddy.cn/docs/workbuddy/Pricing"}
            with mock.patch.dict(os.environ, {"TOKENSCOST_TEST_TRANSCRIPT": transcript}), \
                 mock.patch.object(tc, "STATE_PATH", state), \
                 mock.patch.object(tc, "REPORT_PATH", report_path), \
                 mock.patch.object(tc, "read_current_context", return_value=None), \
                 mock.patch.object(tc, "fetch_model_price", return_value=model_price), \
                 mock.patch.object(tc, "fetch_credit_price", return_value=credit_price), \
                 contextlib.redirect_stdout(io.StringIO()):
                tc.cmd_start(SimpleNamespace())
                with open(transcript, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(read_event, ensure_ascii=False) + "\n")
                    handle.write(json.dumps(finish_event, ensure_ascii=False) + "\n")
                tc.cmd_finish(SimpleNamespace(answer="99.42%|郑文博"))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    tc.cmd_report(SimpleNamespace(json=True))
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["valid"])
            self.assertTrue(payload["flow_valid"])
            self.assertTrue(payload["token_price_valid"])
            self.assertFalse(payload["credit_discount_valid"])
            self.assertEqual(payload["usage"]["calls"], 2)
            self.assertGreater(payload["official_estimate_cny"], 0)
            self.assertIsNone(payload["reported_credit_sum"])
            self.assertIsNone(payload["credit_recharge_cost_cny"])
            self.assertIsNone(payload["recharge_cost_ratio"])
            self.assertTrue(any("缺少 credit" in item for item in payload["violations"]))
            self.assertTrue(os.path.exists(os.path.join(
                tmp, "runs", payload["run_id"], "report.json")))

    def test_settle_reconciles_complete_request_and_excludes_other_requests(self):
        from types import SimpleNamespace
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "session.jsonl")
            state_path = os.path.join(tmp, "state.json")
            report_path = os.path.join(tmp, "report.json")
            run_id = "settlement-test"
            archive = os.path.join(tmp, "runs", run_id)
            os.makedirs(archive)
            workload = os.path.join(tc.ROOT, "cases", "selftest", "S1min.txt")
            raw_events = [
                event("prepare", "Read", credit=0.5),
                event("start", "Bash", credit=0.25,
                      arguments={"command": "bash ./tokencost start"}),
                event("read", "Read", credit=0.25, arguments={"file_path": workload}),
                event("finish", "Bash", credit=0.25, arguments={
                    "command": "bash ./tokencost finish --answer '99.42%|郑文博'"}),
                event("report", "Bash", credit=0.25,
                      arguments={"command": "bash ./tokencost report"}),
                event("answer", "", credit=0.5),
                event("other", "Read", credit=99.0),
            ]
            for raw in raw_events:
                raw["providerData"]["conversationRequestId"] = "other-request" \
                    if raw["id"] == "other" else "measured-request"
            raw_events[5]["type"] = "message"
            with open(transcript, "w", encoding="utf-8") as handle:
                for raw in raw_events:
                    handle.write(json.dumps(raw, ensure_ascii=False) + "\n")
            state = {"version": 3, "status": "done", "run_id": run_id,
                     "session_id": "test", "client": "fixture", "transcript": transcript,
                     "measurement_request_id": "measured-request",
                     "baseline_transcript_bytes": 0,
                     "baseline_transcript_sha256": hashlib.sha256(b"").hexdigest()}
            tc.save_json(state_path, state)
            controlled = {
                "flow_valid": True, "token_price_valid": True,
                "model": "kimi-k3.1-pro", "pricing_model": "kimi-k3.1-pro",
                "model_price": {"vendor": "Kimi", "model": "kimi-k3.1-pro",
                                "currency": "CNY", "input_miss": 13.0, "input_hit": 1.3,
                                "output": 68.0, "cache_write_5m": 13.0},
                "credit_price": {"unit_cny": 0.05}, "credit_unit_cny": 0.05, "fx": [],
            }
            tc.save_json(os.path.join(archive, "report.json"), controlled)
            with mock.patch.dict(os.environ, {"TOKENSCOST_TEST_TRANSCRIPT": transcript}), \
                 mock.patch.object(tc, "STATE_PATH", state_path), \
                 mock.patch.object(tc, "REPORT_PATH", report_path), \
                 mock.patch.object(tc, "read_request_credit_ledger",
                                   return_value={"credit": 2.0, "source": "fixture"}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    tc.cmd_settle(SimpleNamespace(json=True))
            result = json.loads(output.getvalue())
            self.assertTrue(result["valid"])
            self.assertEqual(result["kind"], "request_settlement")
            self.assertEqual(result["usage"]["calls"], 6)
            self.assertEqual(result["reported_credit_sum"], 2.0)
            self.assertTrue(result["credit_evidence"]["ledger_match"])
            self.assertGreater(result["official_estimate_cny"], 0)
            self.assertNotIn("other", result["record_ids"])
            self.assertTrue(os.path.exists(os.path.join(archive, "settled-report.json")))

    def test_real_mode_requires_context_usage(self):
        from types import SimpleNamespace
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "session.jsonl")
            with open(transcript, "w", encoding="utf-8") as handle:
                handle.write("")
            with mock.patch.dict(os.environ, {"CODEBUDDY_SESSION_ID": "session"}, clear=True), \
                 mock.patch.object(tc, "STATE_PATH", os.path.join(tmp, "state.json")), \
                 mock.patch.object(tc, "find_transcript", return_value=transcript), \
                 mock.patch.object(tc, "read_current_context", return_value=None):
                with self.assertRaisesRegex(tc.MeasurementError, "上下文用量"):
                    tc.cmd_start(SimpleNamespace())

    def test_transcript_baseline_prefix_cannot_be_modified(self):
        from types import SimpleNamespace
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "session.jsonl")
            state = os.path.join(tmp, "state.json")
            with open(transcript, "w", encoding="utf-8") as handle:
                handle.write('{"id":"old","type":"session-meta"}\n')
            with mock.patch.dict(os.environ, {"TOKENSCOST_TEST_TRANSCRIPT": transcript}), \
                 mock.patch.object(tc, "STATE_PATH", state), \
                 mock.patch.object(tc, "read_current_context", return_value=None), \
                 contextlib.redirect_stdout(io.StringIO()):
                tc.cmd_start(SimpleNamespace())
            with open(transcript, "w", encoding="utf-8") as handle:
                handle.write('{"id":"changed","type":"session-meta"}\n')
            with open(state, encoding="utf-8") as handle:
                payload = json.load(handle)
            with mock.patch.dict(os.environ, {"TOKENSCOST_TEST_TRANSCRIPT": transcript}):
                with self.assertRaisesRegex(tc.MeasurementError, "基准前缀"):
                    tc.verify_measurement_binding(payload)


if __name__ == "__main__":
    unittest.main()
