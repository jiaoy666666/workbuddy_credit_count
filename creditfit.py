#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import itertools
import json
import math
import statistics

TERMS = ("intercept", "input_miss", "input_hit", "cache_write", "billed_output")
COEFFICIENT_NAMES = {
    "intercept": "intercept_credit_per_call",
    "input_miss": "input_miss_credit_per_million",
    "input_hit": "input_hit_credit_per_million",
    "cache_write": "cache_write_credit_per_million",
    "billed_output": "billed_output_credit_per_million",
}
MAX_CONDITION_NUMBER = 1e8


class FitError(Exception):
    pass


def _solve_square(matrix, values):
    size = len(values)
    augmented = [list(matrix[row]) + [values[row]] for row in range(size)]
    scale = max((abs(value) for row in matrix for value in row), default=0.0)
    if scale == 0:
        return None
    tolerance = scale * max(size, 1) * 1e-12
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= tolerance:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for row in range(column + 1, size):
            factor = augmented[row][column] / divisor
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        remainder = sum(augmented[row][column] * solution[column]
                        for column in range(row + 1, size))
        solution[row] = (augmented[row][size] - remainder) / augmented[row][row]
    return solution


def _rank(matrix):
    work = [list(row) for row in matrix]
    rows = len(work)
    columns = len(work[0]) if rows else 0
    scale = max((abs(value) for row in work for value in row), default=0.0)
    if scale == 0:
        return 0
    tolerance = scale * max(rows, columns) * 1e-12
    rank = 0
    for column in range(columns):
        if rank >= rows:
            break
        pivot = max(range(rank, rows), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) <= tolerance:
            continue
        work[rank], work[pivot] = work[pivot], work[rank]
        divisor = work[rank][column]
        for row in range(rank + 1, rows):
            factor = work[row][column] / divisor
            for index in range(column, columns):
                work[row][index] -= factor * work[rank][index]
        rank += 1
    return rank


def _gram(matrix):
    columns = len(matrix[0])
    return [[sum(row[left] * row[right] for row in matrix)
             for right in range(columns)] for left in range(columns)]


def _gram_eigenvalues(matrix):
    eigen = _gram(matrix)
    size = len(eigen)
    for _ in range(100 * size * size):
        off_diagonal = [(abs(eigen[left][right]), left, right)
                        for left in range(size) for right in range(left + 1, size)]
        largest, left, right = max(off_diagonal, default=(0.0, 0, 0))
        diagonal_scale = max(abs(eigen[index][index]) for index in range(size))
        if largest <= diagonal_scale * 1e-14:
            break
        app, aqq, apq = eigen[left][left], eigen[right][right], eigen[left][right]
        tau = (aqq - app) / (2 * apq)
        tangent = (1 if tau >= 0 else -1) / (abs(tau) + math.sqrt(1 + tau * tau))
        cosine = 1 / math.sqrt(1 + tangent * tangent)
        sine = tangent * cosine
        for index in range(size):
            if index in (left, right):
                continue
            aip, aiq = eigen[index][left], eigen[index][right]
            eigen[index][left] = eigen[left][index] = cosine * aip - sine * aiq
            eigen[index][right] = eigen[right][index] = sine * aip + cosine * aiq
        eigen[left][left] = app - tangent * apq
        eigen[right][right] = aqq + tangent * apq
        eigen[left][right] = eigen[right][left] = 0.0
    return [eigen[index][index] for index in range(size)]


def _condition_number(matrix):
    eigenvalues = _gram_eigenvalues(matrix)
    largest = max(eigenvalues)
    smallest = min(eigenvalues)
    if largest <= 0 or smallest <= largest * 1e-12:
        return None
    return math.sqrt(largest / smallest)


def _features(record):
    return [1.0, record["input_miss"] / 1e6, record["input_hit"] / 1e6,
            record["cache_write"] / 1e6, record["billed_output"] / 1e6]


def _validate_record(record):
    for field in ("input_miss", "input_hit", "cache_write", "billed_output", "credit"):
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FitError("观测 %s 缺少数值字段 %s" % (record.get("event_id"), field))
        if not math.isfinite(value) or value < 0:
            raise FitError("观测 %s 的 %s 必须是有限非负数" % (record.get("event_id"), field))


def _max_feature_correlation(matrix):
    best = None
    for left, right in itertools.combinations(range(1, len(TERMS)), 2):
        xs = [row[left] for row in matrix]
        ys = [row[right] for row in matrix]
        mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
        covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        variance_x = sum((x - mean_x) ** 2 for x in xs)
        variance_y = sum((y - mean_y) ** 2 for y in ys)
        if variance_x == 0 or variance_y == 0:
            continue
        correlation = abs(covariance / math.sqrt(variance_x * variance_y))
        best = correlation if best is None else max(best, correlation)
    return best


def _nnls(matrix, values):
    gram = _gram(matrix)
    xty = [sum(row[column] * value for row, value in zip(matrix, values))
           for column in range(len(TERMS))]
    best_beta = None
    best_sse = None
    for mask in range(1 << len(TERMS)):
        indices = [index for index in range(len(TERMS)) if mask & (1 << index)]
        beta = [0.0] * len(TERMS)
        if indices:
            submatrix = [[gram[left][right] for right in indices] for left in indices]
            solution = _solve_square(submatrix, [xty[index] for index in indices])
            if solution is None or any(value < -1e-10 for value in solution):
                continue
            for index, value in zip(indices, solution):
                beta[index] = max(0.0, value)
        predictions = [sum(row[index] * beta[index] for index in range(len(TERMS)))
                       for row in matrix]
        sse = math.fsum((prediction - value) ** 2
                        for prediction, value in zip(predictions, values))
        if best_sse is None or sse < best_sse:
            best_beta, best_sse = beta, sse
    return best_beta, best_sse


def fit_observations(records, model=None):
    if not records:
        raise FitError("没有可用于拟合的调用记录")
    for record in records:
        _validate_record(record)
    matrix = [_features(record) for record in records]
    values = [float(record["credit"]) for record in records]
    rank = _rank(matrix)
    condition = _condition_number(matrix)
    correlation = _max_feature_correlation(matrix)
    beta, sse = _nnls(matrix, values)
    warnings = []
    if rank < len(TERMS):
        warnings.append("设计矩阵秩为 %d/%d，输入、缓存和输出系数不能同时识别"
                        % (rank, len(TERMS)))
    if rank == len(TERMS) and condition is None:
        warnings.append("设计矩阵数值奇异，不能稳定识别系数")
    if condition is not None and condition > MAX_CONDITION_NUMBER:
        warnings.append("条件数 %.3g 超过 %.3g，系数对微小误差高度敏感"
                        % (condition, MAX_CONDITION_NUMBER))
    if correlation is not None and correlation > 0.95:
        warnings.append("计费分项最大相关系数为 %.4f，存在强共线性" % correlation)
    if len(records) < 2 * len(TERMS):
        warnings.append("观测数少于参数数的两倍，建议增加独立条件并重复测量")
    identifiable = rank == len(TERMS) and condition is not None \
        and condition <= MAX_CONDITION_NUMBER
    predictions = [sum(row[index] * beta[index] for index in range(len(TERMS)))
                   for row in matrix]
    residuals = [value - prediction for value, prediction in zip(values, predictions)]
    mean_value = statistics.fmean(values)
    total_ss = math.fsum((value - mean_value) ** 2 for value in values)
    r_squared = 1 - sse / total_ss if total_ss > 0 else (1.0 if sse <= 1e-20 else 0.0)
    result = {
        "fit_version": 1,
        "model": model or sorted({record.get("model", "") for record in records}),
        "observations": len(records),
        "terms": list(TERMS),
        "rank": rank,
        "required_rank": len(TERMS),
        "condition_number": condition,
        "max_abs_feature_correlation": correlation,
        "identifiable": identifiable,
        "coefficients": ({COEFFICIENT_NAMES[term]: beta[index]
                          for index, term in enumerate(TERMS)} if identifiable else None),
        "approximate_95pct_ci": None,
        "uncertainty_note": "置信区间为正态近似，未完整建模 credit 取整与非负边界分布",
        "fit": {
            "rmse_credit": math.sqrt(sse / len(records)),
            "max_abs_residual_credit": max(abs(value) for value in residuals),
            "r_squared": r_squared,
        },
        "warnings": warnings,
    }
    if identifiable and len(records) > len(TERMS):
        gram = _gram(matrix)
        inverse_columns = [_solve_square(gram, [1.0 if row == column else 0.0
                                                for row in range(len(TERMS))])
                           for column in range(len(TERMS))]
        if all(column is not None for column in inverse_columns):
            variance = sse / (len(records) - len(TERMS))
            quantile = statistics.NormalDist().inv_cdf(0.975)
            standard_errors = [math.sqrt(max(0.0, variance * inverse_columns[index][index]))
                               for index in range(len(TERMS))]
            result["approximate_95pct_ci"] = {
                COEFFICIENT_NAMES[term]: [beta[index] - quantile * standard_errors[index],
                                          beta[index] + quantile * standard_errors[index]]
                for index, term in enumerate(TERMS)
            }
    return result


def fit_reports(paths):
    records = []
    run_ids = []
    models = set()
    credit_units = set()
    credit_unit_complete = True
    seen_events = set()
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise FitError("无法读取报告 %s：%s" % (path, exc)) from exc
        if not report.get("valid") or report.get("report_version", 0) < 3:
            raise FitError("只接受有效 v3+ 报告：%s" % path)
        model = report.get("model")
        if not model:
            raise FitError("报告缺少模型 ID：%s" % path)
        models.add(model)
        unit = report.get("credit_unit_cny")
        if unit is not None:
            if isinstance(unit, bool) or not isinstance(unit, (int, float)) \
                    or not math.isfinite(unit) or unit < 0:
                raise FitError("报告 %s 的 credit 单价非法" % path)
            credit_units.add(float(unit))
        else:
            credit_unit_complete = False
        run_ids.append(report.get("run_id") or path)
        for record in report.get("per_call") or []:
            if record.get("model") != model:
                raise FitError("报告 %s 中存在模型不一致的调用" % path)
            event_id = record.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise FitError("报告 %s 中存在无事件 ID 的调用" % path)
            if event_id in seen_events:
                raise FitError("跨报告事件 ID 重复：%s" % event_id)
            seen_events.add(event_id)
            records.append(record)
    if len(models) != 1:
        raise FitError("禁止跨模型合并拟合：%s" % "、".join(sorted(models)))
    if len(credit_units) > 1:
        raise FitError("报告使用了不同的官方 credit 单价，不能合并现金系数")
    if not records:
        raise FitError("报告中没有受控调用记录")
    result = fit_observations(records, model=next(iter(models)))
    result["run_ids"] = run_ids
    if credit_units and credit_unit_complete:
        unit = next(iter(credit_units))
        result["credit_unit_cny"] = unit
        if result["coefficients"]:
            result["coefficients_cny"] = {
                name.replace("credit", "cny"): value * unit
                for name, value in result["coefficients"].items()
            }
        else:
            result["coefficients_cny"] = None
    elif credit_units:
        result["warnings"].append("部分报告缺少官方 credit 单价，未输出任何现金系数")
    return result


def print_result(result, json_output=False):
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return
    print("模型：%s" % result["model"])
    print("观测：%d 次调用；设计矩阵秩：%d/%d"
          % (result["observations"], result["rank"], result["required_rank"]))
    if result["condition_number"] is not None:
        print("条件数：%.4g" % result["condition_number"])
    print("RMSE：%.8g credit；最大绝对残差：%.8g credit"
          % (result["fit"]["rmse_credit"], result["fit"]["max_abs_residual_credit"]))
    if result["coefficients"]:
        print("非负最小二乘系数：")
        for name, value in result["coefficients"].items():
            print("  %s = %.8g" % (name, value))
    else:
        print("系数不可识别，未输出单价。")
    if result.get("coefficients_cny"):
        print("按官方 credit 单价折算的现金系数：")
        for name, value in result["coefficients_cny"].items():
            print("  %s = %.8g" % (name, value))
    for warning in result["warnings"]:
        print("警告：" + warning)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="creditfit", description="离线拟合 credit 消耗函数")
    parser.add_argument("reports", nargs="+", help="有效 v3+ report.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = fit_reports(args.reports)
        print_result(result, json_output=args.json)
        if not result["identifiable"]:
            raise SystemExit(3)
    except FitError as exc:
        if args.json:
            print(json.dumps({"fit_version": 1, "identifiable": False,
                              "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print("错误：%s" % exc)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
