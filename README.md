# tokencost — 当前 agent 的受控 token/credit 自测

目标不是模拟其他平台，而是让**当前会话里的 agent** 完成一次固定测试，读取本地真实调用记录，再与模型厂商官方刊例价和 WorkBuddy 官方积分价格比较。结果是固定负载下的“加量包补充成本比”，不是 WorkBuddy 的实际采购价、厂商结算价或官方折扣承诺。

## 运行方式

### 环境准备

- Python 3.10 或更高版本。
- Bash 用户直接使用 `bash ./tokencost`；Windows CMD/PowerShell 可使用 `tokencost.cmd`。
- 当前自动适配 WorkBuddy 桌面版和 CLI。其他客户端需要对应会话适配器，不支持时会明确停止。
- 不需要配置模型 API Key。模型由用户在客户端中选择，官方取价只访问工具白名单中的厂商页面。

### 在客户端中直接自测

1. 解压分发包，让客户端的工作目录位于解压后的工具根目录。
2. 在客户端选择要测试的模型，建议使用新会话。
3. 粘贴下面这句话：

```text
请在当前目录严格按照 AGENTS.md 执行一次受控自测，从 bash ./tokencost start 开始，依次完成固定题目读取、finish 和 report，不要读取其他文件或执行非流程操作，最后原样展示 token、官方预估价、credit 和折扣结果，credit 缺失时不得按 0 计算。
```

agent 会依次执行 `start → Read 固定题目 → finish → report`，报告保存在 `data/runs/<run_id>/report.json`，最近结果保存在 `data/latest-report.json`。

### 与 WorkBuddy 完整请求显示值对账

`report` 只统计 Read+finish 两次受控调用。若要与 WorkBuddy 页面显示的完整请求消耗比较，必须在下一轮用户消息粘贴：

```text
请在当前目录执行 bash ./tokencost settle，核对同一 conversationRequestId 的完整 token 与 credit 消耗，并展示 settled-report 的结果；若账本不一致或未结算，原样报告错误并停止。
```

结果写入 `data/runs/<run_id>/settled-report.json`。settle 会包含本轮最终回答并核对本地账本，不包含 settle 自身的新请求。

### 手动运行

在工具根目录可按同样顺序执行：

```bash
bash ./tokencost start
# 按 start 提示完整读取固定题目
bash ./tokencost finish --answer '<合格率>|<主检人>'
bash ./tokencost report
# 下一轮用户请求中再执行
bash ./tokencost settle
```

Windows CMD/PowerShell 可将 `bash ./tokencost` 换成 `tokencost.cmd`。不得通过重复 finish、删除状态或合并其他会话来挑选结果。

### 隐私与本地数据

分发包不包含本机路径、账号信息、历史会话或测试结果。运行后产生的测量状态和报告仅保存在当前工具的 `data/` 目录；该目录、客户端记忆目录和 Python 缓存均不进入分发包。

## 唯一命令流程

给 agent 的完整约束在 `AGENTS.md`。标准执行顺序：

```bash
bash ./tokencost start
# 只读取 start 打印的固定题目文件
bash ./tokencost finish --answer '<合格率>|<主检人>'
bash ./tokencost report
```

`report --json` 只输出一个机器可读 JSON 文档。报告分别记录 `flow_valid`、`token_price_valid` 和 `credit_discount_valid`：行为违规返回退出码 3；流程正确但 credit 不完整时仍给出官方 token 预估价，credit 金额和折扣为 `null`；状态或取价错误返回退出码 2。所有报告都会独立归档。

`report` 的主口径是 **Read+finish 两次受控调用**，不包含 start/report 管理调用和报告后的最终回答。WorkBuddy 页面展示的是完整请求总消耗，二者不能直接比较。需要与完整请求对账时，必须在下一轮用户消息执行 `bash ./tokencost settle`；它生成 `settled-report.json`，纳入本轮协议读取、start、report 和最终回答，并核对 `session_usage.credit_json`，但不计入 settle 自身的新请求。

## 数据从哪里来

### Token 与本地 credit

工具通过客户端适配器读取**当前会话 ID 对应的 transcript** 中每次模型调用的 `providerData.rawUsage`，按事件 ID diff 新增调用。`client_adapters.py` 当前自动适配 WorkBuddy 桌面版的 `~/.workbuddy/projects` 和新版 CLI 的 `~/.codebuddy/projects`，只按当前精确会话 ID 查找；未知客户端明确停止，不扫描或替代为其他会话：

- `prompt_tokens`
- `prompt_cache_hit_tokens`
- `prompt_cache_miss_tokens`
- `prompt_cache_write_tokens` / `cache_creation_input_tokens`
- `completion_tokens`
- 推理 token
- 实际模型 ID
- 每次调用扣除的 `credit`

不会选择“上下文最大的会话”代替当前会话；不会从会话总账猜单次调用成本。报告中的 `reported_credit_sum` 是逐调用 `rawUsage.credit` 的合计，**不是账户余额前后差**。每次调用必须具有唯一事件 ID、有效时间戳和模型 ID；输入分项不一致或推理输出大于计费输出会使流程无效。credit 缺失只使折扣不可用，不再抹掉可信的官方 token 预估价。

### 官方刊例价

按实际模型 ID 从白名单中的官方页面取价，不套用手工价格：

- Kimi：`https://platform.moonshot.cn/pricing`
- DeepSeek：`https://api-docs.deepseek.com/quick_start/pricing`
- 智谱、Qwen、MiniMax 和 WorkBuddy：使用 `tokencost.py` 中登记的白名单官方地址

只支持下表精确列出的模型 ID，不会因前缀相似而套用其他型号。价格证据保存原始内容、SHA-256、来源与最终 URL、抓取时间。DeepSeek 官方价为美元时，按调用日期读取中国外汇交易中心美元兑人民币中间价，不会把美元数字直接当人民币。

### WorkBuddy credit 价格

读取官方个人版定价页中的加量包：**1,000 积分 / 50 元**，即当前补充成本 **¥0.05/credit**。

这是加量包补充成本，不是：

- 订阅赠送积分的分摊成本；
- 会员积分的市场公允价值；
- 可退款或可兑换现金价值。

## 计算口径

```text
官方预估价 =
    缓存未命中输入 × 厂商未命中输入价
  + 缓存命中输入 × 厂商缓存价
  + 计费输出（可见输出 + 推理输出）× 厂商输出价
  + 缓存写入 × 厂商缓存写入价（有单列时）

逐调用 credit 上报合计 = Σ rawUsage.credit
加量包补充成本 = 逐调用 credit 上报合计 × WorkBuddy 官方加量包单价

补充成本比 = 加量包补充成本 ÷ 官方预估价
补充成本比 ≤ 1 时：按此口径相当于 补充成本比×10 折
差额比例 = 1 - 补充成本比
刊例等效价值 = 官方预估价 ÷ 逐调用 credit 上报合计（credit 合计大于 0 时）

计费 token 总量 = 输入总量 + 计费输出（缓存写入不重复计数）
本次负载等效现金均价 = 加量包补充成本 ÷ 计费 token 总量 × 1,000,000
本次负载刊例均价 = 官方预估价 ÷ 计费 token 总量 × 1,000,000
```

例如补充成本比为 0.10，则展示为 **按加量包口径相当于 1.0 折、差额比例 90%**。该比例不是 WorkBuddy 官方折扣。“刊例等效价值”也不是 credit 的现金面值。

`credit_cost_cny_per_million_tokens` 是本次特定输入、输出和缓存结构下的等效现金均价，不是可跨负载套用的统一模型单价。普通输入、缓存和输出各自的现金单价只能通过多探针拟合获得；`fit` 在报告具有一致 `credit_unit_cny` 时额外输出 `coefficients_cny`。

## 行为约束与无效判定

`start` 后只允许：

1. 一次完整 `Read` 固定题目；
2. 一次 `finish`；
3. 一次 `report`。如果流程或答案校验失败，测量立即无效且不可重试；只有官方取价、汇率获取等外部读取失败时，才允许在状态仍为 `finished` 的情况下重试 `report`。

检测到以下任一项都会把结果判为无效，并且不计算价格或折扣：

- 额外联网、额外交互、读取其他文件或调用其他会话记录；
- 重复 Read/finish，或 Read 使用 offset/limit 等分页参数；
- 测量期间切换模型，包括管理调用与受控调用模型不一致；
- 答案格式错误或检索结果错误；
- 任一受控调用缺少实际模型 ID、时间戳或稳定事件 ID；
- token/credit 数值非法，缓存输入分项不一致，或推理输出大于计费输出；
- 当前会话或 transcript 与基准不一致，基准前缀被修改；
- 题目文件哈希变化。

以上行为或 token 数据违规时不计算任何价格。若这些检查全部通过、官方取价成功，但部分 credit 缺失或非法，则生成部分报告：`valid=false`、`flow_valid=true`、`token_price_valid=true`、`credit_discount_valid=false`，保留官方 token 预估价，所有 credit 价值和折扣字段为 `null`。

CLI 不是操作系统沙箱，不能物理禁止 agent 调工具；它会识别越界并拒绝给出有效价格结果。

## 精度边界

- 题目文件字节和 SHA-256 固定，本地名义值为 15,209 o200k token。
- 不同平台的 tokenizer 可能给出不同输入 token 数；报告以实际调用记录为准。
- 可见输出可约束，推理输出不可硬控。
- 该结果测量“这两次受控调用”，不能外推成整个 agent 会话成本，也不能跨不同上下文长度直接比较。
- 两次调用不足以反解固定调用费、输入、缓存和输出的分项 credit 单价；本项目不输出这种欠定拟合。
- 历史上下文超过 60,000 token 时拒绝开始；真实模式下无法读取上下文用量也会拒绝开始。数据库不可用时，可从当前 transcript 最新 `prompt_tokens` 得到已用上下文，但上下文总大小会明确记为未知，不伪造 1M 配置或剩余空间。

## 当前支持的模型价格解析器

仅收录已经能用官方页面或官方 JSON 直接解析、并覆盖缓存分项的模型：

| 厂商 | 模型 ID | 当前解析项 |
|---|---|---|
| Moonshot / Kimi | `kimi-k3.1-pro`、`kimi-k3.1-pro-highspeed`、`kimi-k3` | 输入、输出、缓存命中、缓存写入 |
| DeepSeek | `deepseek-v4.1-flash`、`deepseek-flash`、`deepseek-v4-pro` | 峰谷输入、峰谷缓存、峰谷输出 |
| 智谱 / BigModel | `glm-5.3`、`glm-5.3-flash`、`glm-5.3-flashx` | 输入、输出、缓存命中；缓存存储当前官方标注限时免费 |
| 阿里云百炼 / Qwen | `qwen3.8-max`、`qwen3.8-flash` | 输入、输出、隐式缓存命中、显式缓存创建 |
| MiniMax | `minimax-m3.1-flash` | 输入、输出、缓存读取 |

遇到其他模型时，工具会明确停止，而不会猜测或套用其他厂商价格。OpenAI、Anthropic、Google 等厂商当前未纳入，原因是其官方价格页在本环境中无法稳定直接解析；不会在不可验证时硬编码。

## 可选：多报告 credit 机制拟合

`fit` 是测量完成后的离线分析，不属于单次受控自测流程，不访问网络：

```bash
bash ./tokencost fit data/runs/<run-a>/report.json data/runs/<run-b>/report.json --json
# 也可直接运行
python creditfit.py data/runs/<run-a>/report.json data/runs/<run-b>/report.json
```

拟合模型：

```text
credit = 固定调用费
       + 未命中输入（百万）× 对应系数
       + 缓存命中输入（百万）× 对应系数
       + 缓存写入（百万）× 对应系数
       + 计费输出（百万）× 对应系数
```

分析器使用非负最小二乘，并输出设计矩阵秩、条件数、分项相关系数、RMSE、最大残差和近似置信区间。它只接受**同一模型、事件 ID 不重复的有效 v3+ 报告**，禁止跨模型合并。

要识别五个参数，实验必须分别改变普通输入、缓存命中、缓存写入和输出，且这些变量不能始终一起等比例变化。建议至少准备 8–12 个独立条件，并对部分条件重复测量：

- 用不同长度的未缓存输入估计普通输入系数；
- 对相同前缀形成不同程度的真实缓存命中；
- 在平台支持时独立改变显式缓存写入；
- 用不同输出长度估计输出系数，并保留实际推理 token；
- 用近零输入/输出调用帮助识别固定调用费。

**重复运行当前同一个固定题目通常仍然秩不足。** 此时 `fit` 返回退出码 3、`identifiable=false`、`coefficients=null`，这是正确的防误报行为。拟合得到的是 credit 消耗系数，不是现金单价，也不是厂商结算价；当前版本只提供分析器，不把常规自测自动扩展成多探针流程。

## 文件

```text
tokencost                    跨平台 shell 启动入口（自动选择 python3/python/py）
tokencost.cmd                Windows CMD/PowerShell 启动入口
tokencost.py                 受控测量主程序
client_adapters.py           当前客户端与会话定位适配层
creditfit.py                 可选的多报告非负最小二乘分析器
cases/workload.json          固定题目定义与预期答案
cases/selftest/S1min.txt     固定题目文件（SHA-256 校验）
scripts/test_tokencost.py    单元与状态机测试
data/measurement-state.json  当前测量状态（运行后生成）
data/latest-report.json      最近报告（运行后生成）
data/runs/<run_id>/          受控报告、settled-report.json 与 price-evidence 价格证据
```

## 验证

```bash
python scripts/test_tokencost.py
```

测试使用本地夹具，不消耗真实模型额度，也不写真实会话记录。`data/`、`.workbuddy/` 和缓存只可能在本机运行后生成，分发包采用明确文件清单，不包含历史测量、账号信息、会话标识或测试结果。

