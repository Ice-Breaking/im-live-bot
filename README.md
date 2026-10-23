# IM_live 飞书机器人

用飞书自建应用的机器人：收到消息后调用阶跃星辰模型，把答案直接回复到原会话。单进程、最少依赖，没有任务单、没有数据库、没有 HTTP 接口。

## 分层

| 层 | 文件 | 职责 |
|---|---|---|
| 平台适配器 | `feishu_channel.py` | 建立飞书长连接、把消息归一化为 `IncomingMessage`、`reply` 回原消息。不含业务规则 |
| 终端渠道 | `terminal_channel.py` | stdin 一行一个问题，答案打印到 stdout。与飞书渠道遵守同一套约定 |
| 渠道约定 | `channel_contract.py` | `IncomingMessage`、`ChannelPlugin`、`SendError`：渠道与 Gateway 都只依赖它 |
| Gateway | `gateway.py` | 编排：判定之后分派，起后台线程调模型并把答案交出去 |
| 会话规则判定 | `session_policy.py` | 只回答「这条消息该做什么」，不执行、不记日志 |
| 出站发送 | `outbound.py` | 按渠道单条上限分段，失败按固定间隔重试，返回发送结果 |
| 结果缓存 | `result_store.py` | 最近处理过的消息与结果，容量固定、滚动淘汰，启动时可恢复 |
| 业务能力 | `chat_agent.py` | 输入校验与提示词拼装，暴露 `answer(question) -> str`。不认识飞书，也不认识会话 |
| 模型通道 | `llm_client.py` | 通过 OpenAI 兼容端点调用 StepFun，超时 180 秒 |
| 任务记录 | `task_log.py` | 写日志与 `data/tasks.jsonl`、按编号回看、取回待重发结果 |
| 任务阶段 | `task_stage.py` | 阶段名与编号分配，记录与编排共用 |
| 装配 | `assembly.py` | 把配置、业务、网关与启用的入口接起来 |
| 入口 | `main.py` | 加载配置、抢单实例锁、按入口启动；`--task` 查任务记录 |
| 配置 | `config.py` | 修正根证书路径、读环境并校验、配置日志 |

```
飞书 IM --WebSocket--> feishu_channel ---+
                                          |--> gateway --后台线程--> chat_agent --> llm_client --> StepFun
终端 stdin --REPL----> terminal_channel --+        |
                        ^                          |
                        +---- 按消息来源 reply -----+
```

消息在后台线程处理，长连接的事件回调线程不被模型耗时占用，因此连续提问不会互相阻塞。

## 渠道插件约定（新增入口时照着做）

渠道只做三件事，业务能力与会话规则都不在渠道里：

1. 连接或读取本渠道的输入；
2. 把平台报文转换成 `IncomingMessage` 交给 `Gateway.handle`；
3. 实现 `reply`，把结果按本渠道的方式展示出去。

约定落在 `channel_contract.py` 的 `ChannelPlugin` 上（gateway 会把它转出去，`from gateway import IncomingMessage, SendError` 仍然可用），共四项：

| 约定项 | 作用 | 飞书 | 终端 |
|---|---|---|---|
| `name` | 渠道名，与 `IncomingMessage.channel` 一致，Gateway 靠它把答案路由回来源入口 | `feishu` | `terminal` |
| `max_segment_chars` | 该渠道单条消息长度上限，决定超长答案怎么分段 | 3000（飞书单条限制） | 20000（等于答案总上限，即不分段） |
| `start()` | 开始接收本渠道的消息 | WebSocket 长连接阻塞运行 | REPL 循环读 stdin |
| `reply(target, text)` | 展示结果 | 按 `message_id` 回复到原消息 | 打印到 stdout |

装配方式：`gateway.attach_outbound(channel)` 按名字注册，**可以挂多个**。两个入口共用同一个 Gateway、同一个业务对象、同一份去重缓存；答案只回到提问的那个入口。

补充一条：发送不成功时 `reply` 必须抛 `SendError`（`gateway.SendError`），网关据此重试三次并记 `回复失败`；只打日志会让失败悄悄过去。终端渠道用 `print` 展示，不会失败。

## 环境准备

依赖只有四个：

```bash
pip install -r requirements.txt   # lark-oapi python-dotenv certifi openai
```

项目根 `.env`（已被 gitignore）需要这 7 项，缺任一项启动即报错：

| 键 | 说明 |
|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用的凭证 |
| `FEISHU_BOT_OPEN_ID` | 机器人自己的 open_id，`ou_` 开头，用于判断群聊是否 @ 了机器人 |
| `STEP_API_KEY` | StepFun 密钥 |
| `STEP_BASE_URL` | 这里用 Step Plan 通道 `https://api.stepfun.com/step_plan/v1` |
| `STEP_MODEL` | 现在用 `step-3.7-flash` |
| `ALLOWED_OPEN_IDS` | 允许使用的飞书用户 open_id，`ou_` 开头，多个用逗号分隔 |
| `ENABLED_CHANNELS` | 可选，启用哪些入口：`feishu`、`terminal`，逗号分隔，默认 `feishu`；写错启动即报错 |
| `TERMINAL_USER_OPEN_ID` | 可选，终端入口的身份，默认 `ou_terminal`，自动并入白名单，不用再写进 `ALLOWED_OPEN_IDS` |

可选：`LARK_LOG_LEVEL`（DEBUG/INFO/WARNING/ERROR，排障用 DEBUG 能看到长连接的数据帧）。

复制模板再填值：

```bash
cp .env.example .env   # .env 已在 .gitignore 中，不会进版本库
```

**凭证处理**：密钥只存在于 `.env`；`.env.example` 只有键名没有值；启动日志只打印模型名与端点，不回显密钥；SDK 会把长连接地址连同 `access_key` / `ticket` 打进日志，`config.setup_logging` 挂了脱敏过滤器，日志里一律显示为 `***`。

## 启动

```bash
cd IM_live
python3 main.py                       # 按 .env 的 ENABLED_CHANNELS 启动
python3 main.py --channel terminal    # 命令行覆盖配置，只开终端
python3 main.py --channel feishu,terminal   # 两个入口同时开
python3 main.py --ask "一句话说明长连接机器人的优点"   # 终端问一次就退出
```

正常启动会打印：配置加载完成（含「启用入口=... 任务记录=...」）→ `平台连接: 飞书长连接 启动中` → `已建立`（同时 SDK 会打印 `connected to wss://msg-frontier.feishu.cn/ws/v2`，凭证已脱敏）。

终端入口是交互式的：输入问题回车，`exit` / `quit` / Ctrl-D 退出，`/help` 看用法；答案直接打印在屏幕上。

```bash
$ python3 main.py --channel terminal
终端入口已启动，输入问题后回车；exit / quit / Ctrl-D 退出。
你> /help

助手> 机器人用法：...
```

后台运行时（`nohup`）stdin 指向 `/dev/null`，终端 REPL 会自动跳过并打印一条告警，飞书入口继续运行。

**只允许跑一个实例。** 飞书同一应用的多条长连接会把事件分走，表现为「机器人不回复」。启动时会对 `data/bot.lock` 加独占锁，第二个实例看到的是：

```
ERROR im_live: 已有实例在运行（锁文件 .../data/bot.lock），请先停掉再启动
```

后台运行（关掉终端也继续跑）：

```bash
nohup python3 main.py > /tmp/im_live_stdout.log 2>&1 &
tail -f logs/im_live.log      # 业务日志始终在这里
```

停止：`pkill -f "python3 main.py"`，锁随进程退出自动释放。

## 飞书里怎么用

| 输入 | 行为 |
|---|---|
| 任意一句话 | 调用模型，答案回复到这条消息下 |
| `/help`、`help`、`帮助` | 回复用法说明 |
| 群聊里发消息 | 只有 `@机器人` 才响应，否则忽略 |
| 空消息 | 回「消息内容为空，请直接把想问的内容发给我」 |
| 单次超过 8000 字 | 回「请拆分后发送」 |
| 白名单外的账号 | 拒绝，并回带其真实 open_id，方便加进白名单 |

答案超过 3000 字会自动分段，最多发 20000 字。

## 本地验证

### 1) 飞书端到端（真实模型调用）

给机器人发一句话，日志里应出现三段：

```bash
tail -f logs/im_live.log
```

```
任务 T00006 收到消息 chat_type=p2p 文本长度=3 耗时=0ms
任务 T00006 开始分析 问题长度=3 耗时=1ms
chat_agent: 开始回答: 问题长度=3
llm_client: 模型调用开始: model=step-3.7-flash 提示词长度=114
llm_client: 模型调用完成: model=step-3.7-flash 耗时=2.1s 输出长度=74
任务 T00006 分析完成 结果长度=74 耗时=2082ms
feishu_channel: 回复成功: message_id=om_xxx
任务 T00006 回复发出 分段=1 耗时=800ms
```

### 2) 会话规则分支（不调模型）

想脱离飞书单独验证，用假通道直接喂消息给 `Gateway`：

```bash
python3 - <<'PY'
import time
import config
from chat_agent import ChatAgent
from gateway import Gateway, IncomingMessage
from llm_client import StepFunClient
from task_log import TaskLog

cfg = config.load_config()
config.setup_logging(cfg.log_path)

class FakeChannel:
    """假渠道也要满足插件约定：有名字、有单条上限；这里只收集结果，真实渠道发送失败要抛 SendError。"""
    name = "fake"
    max_segment_chars = 20000
    def __init__(self): self.log = []
    def start(self): pass
    def reply(self, message_id, text): self.log.append(text)

fake = FakeChannel()
gw = Gateway(ChatAgent(StepFunClient(cfg.step_api_key, cfg.step_base_url, cfg.step_model)),
             cfg.allowed_open_ids, TaskLog(cfg.task_log_path))
gw.attach_outbound(fake)

real_id = cfg.allowed_open_ids[0]
def msg(**kw):
    base = dict(channel="fake", user_open_id=real_id, chat_id="oc_probe", chat_type="p2p",
                message_id="om_t" + str(time.time_ns()), text="", mentioned_bot=False)
    return IncomingMessage(**{**base, **kw})

gw.handle(msg(text="/help"))                                  # 用法说明
gw.handle(msg(user_open_id="ou_not_allowed", text="你好"))     # 拒绝并回带 open_id
gw.handle(msg(chat_type="group", mentioned_bot=False, text="你好"))   # 忽略，无出站
gw.handle(msg(text="   "))                                    # 空消息提示
gw.handle(msg(text="很" * 9000))                              # 超长提示
time.sleep(2)                                                 # 后两条走后台线程，等它们跑完
print(fake.log)
PY
```

## 测试（特征测试）

测试锁的是「现在的行为」：重构前后跑同一套，全绿即行为没变。

```bash
python3 -m unittest discover -s tests -v                       # 日常：秒级，不调模型、不联网
IM_LIVE_REAL_MODEL=1 python3 -m unittest discover -s tests -v  # 额外跑真实模型用例
```

| 文件 | 锁住什么 |
|---|---|
| `tests/test_gateway_flow.py` | 端到端编排：阶段顺序、分段与标题、空/超长、模型异常、越权、群聊 @、帮助、重复投递复用、发送失败重试与结果保留 |
| `tests/test_session_policy.py` | 会话规则判定矩阵与判定顺序 |
| `tests/test_outbound.py` | 分段长度、重试次数与间隔、失败停在第几段 |
| `tests/test_result_store.py` | 去重登记、结果保存、容量滚动淘汰 |
| `tests/test_result_restore.py` | 待重发结果的识别与跨重启复用（不再分析） |
| `tests/test_task_log.py` | 编号续号、落盘字段、`--task` 输出、答案正文不进日志 |
| `tests/test_config.py` | 校验规则、入口解析、凭证脱敏 |
| `tests/test_channels.py` | 终端等待与输出语义、飞书归一化与发送失败抛 `SendError` |
| `tests/test_assembly.py` | 多入口共用同一网关、答案按来源路由 |
| `tests/test_connection_watch.py` | 长连接守护：连上/断开/恢复各记一条，状态不变不重复记 |
| `tests/test_real_model.py` | 真实模型冒烟，默认跳过 |

约定：替身放在 `tests/support.py`（`StubClient` / `FakeChannel`），记录文件用临时目录，重试等待注入为 0，所以套件不联网、不调模型、不污染 `data/` 与 `logs/`；测试只用标准库，`requirements.txt` 没有测试依赖。

## 运行记录与查看方式

**启动**时说清两件事：

- 服务本身：`配置加载完成: model=... base_url=... 白名单=N 条 启用入口=... 任务记录=...`
- 平台连接：`平台连接: 飞书长连接 启动中` → `已建立`；断开记 `断开，重连中`；连不上记 ERROR `启动失败`

**每次处理**是一个任务，编号从 `T00001` 开始，重启后接着历史编号往下发（不会撞号）。五个阶段：

```
收到消息 → 开始分析 → 分析完成 → 回复发出 / 回复失败
```

被拒绝（不在白名单）或忽略（群聊未 @、重复投递）的任务也会各记一条。

**三种看法**

```bash
python3 main.py --ask "一句话说明长连接的好处"   # 终端入口：屏幕直接显示任务编号与答案
tail -f logs/im_live.log                        # 后台运行时实时看
python3 main.py --task T00006                   # 按编号看一次处理的全过程，不启动服务
grep T00006 logs/im_live.log                    # 或者直接搜日志
```

`--task` 的输出：

```
任务 T00006  渠道=feishu  消息=om_x100b...  发送人=ou_xxx
  12:57:55.100  收到消息  chat_type=p2p 文本长度=3  +0ms
  12:57:55.101  开始分析  问题长度=3  +1ms
  12:57:58.183  分析完成  结果长度=74  +2082ms
  12:57:58.983  回复发出  分段=1  +800ms
```

**记录落在两处**

| 位置 | 记什么 | 说明 |
|---|---|---|
| `logs/im_live.log` | 编号、阶段、耗时、长度 | 只记长度，**不记消息与答案原文**；连接凭证（`access_key` / `ticket`）自动换成 `***` |
| `data/tasks.jsonl` | 完整字段（含答案正文） | 供发送失败后重发用，避免重复分析；`data/` 已在 `.gitignore` 里 |

**发送失败会怎样**：渠道发送失败抛 `SendError`，网关自动重试三次（间隔 3 秒），仍失败就记 `回复失败` 并把答案留在内存；同一条消息再次到达时直接重发已有结果，**不再调用模型**（记录里是 `复用结果重发`）。

注意：`LARK_LOG_LEVEL=DEBUG` 时 SDK 会打印收发的数据帧，里面带消息原文，日常用默认的 INFO。

## 三步走进度（连接 → 原样回复 → 接入业务）

### 步骤一：准备连接程序（已完成）

- 平台连接与消息格式只留在适配器 `feishu_channel.py`：长连接、事件归一化、`reply` 回原消息
- 凭证：`.env` 存真实值、`.env.example` 给模板、日志脱敏
- 启动：`python3 main.py`，`data/bot.lock` 保证只有一个实例

怎么验的：构造 `P2ImMessageReceiveV1` 事件对象直接驱动 `FeishuChannel._on_message_receive`，**不发真实 API**。

| 构造的事件 | 归一化结果 | 结论 |
|---|---|---|
| 单聊文本「你好」 | `chat_type=p2p, text='你好', sender=ou_xxx` | 符合预期 |
| 群聊 @机器人（文本带 `<at>` 标签） | `chat_type=group, mentioned_bot=True, text='帮我看下这条'` | `<at>` 已剥离 |
| 群聊 @别人 | `mentioned_bot=False` | 不会响应 |
| 非文本消息（image） | 未产生入站消息 | 已跳过 |

通过标志：日志出现 `connected to wss://msg-frontier.feishu.cn/ws/v2?...&access_key=***`（脱敏生效），且构造事件能产出正确的 `IncomingMessage`。

### 步骤二：验证原样回复（已完成）

假通道收集出站消息 + 一个返回原文的替身业务对象（`answer(question) -> question`，不调模型）：

| 场景 | 出站结果 |
|---|---|
| 单聊原文 | 与原文逐字一致，且回复到原 `message_id` |
| 群聊 @机器人 | 回复去掉 `<at>` 后的原文 |
| 群聊未 @ 机器人 | 无出站消息 |
| 越权 open_id | 拒绝并回带其真实 open_id |
| 重复投递同一 `message_id` | 忽略，不重复回复 |
| `/help` | 回复用法说明 |

通过标志：出站内容与原文本一致；本步 `grep -c "模型调用开始" logs/im_live.log` 为 **0**，确认没有混进真实模型调用。

### 步骤三：接入真实业务（已完成）

业务能力复用 `chat_agent` + `llm_client`（`step-3.7-flash`），结果回原会话。

| 验证 | 模型耗时 | 输出 | 端到端 |
|---|---|---|---|
| 假通道真实调用（问：长连接机器人与 Webhook 机器人的区别） | 5.6s | 109 字 | 5.7s |
| 飞书真实消息（单聊） | 9.4s | 60 字 | 收到 11:06:21 → 模型完成 11:06:31 → 回复成功 11:06:32，共 11s |

通过标志：飞书里收到答案，日志三段齐全（收到消息 / 模型调用完成 / 回复成功）。

## 验收记录

### 真实模型调用

| 场景 | 模型 | 耗时 | 结果 |
|---|---|---|---|
| 飞书私聊「用一句话介绍你自己」 | `step-3.7-flash` | 模型 4.3s，端到端约 5s（10:35:11 收到 → 10:35:16 回复成功） | 回复成功，63 字 |
| 假通道同一问题 | `step-3.7-flash` | 模型 2.5s | 回复成功，53 字 |

判定依据：真实调用必然成对出现 `模型调用开始` 与 `模型调用完成: model=... 耗时=...s 输出长度=...` 两行日志。

### 非模型分支

| 检查项 | 触发方式 | 结果 |
|---|---|---|
| `/help` | 假通道 | 回复用法说明，无模型调用 |
| 越权 open_id | 假通道 | 拒绝并回带其真实 open_id |
| 群聊未 @ 机器人 | 假通道 | 忽略，无任何出站消息 |
| 空消息 | 假通道 | 回「消息内容为空…」，`EMPTY_QUESTION` |
| 超长 9000 字 | 假通道 | 回「超过 8000 字上限，请拆分后发送」，`QUESTION_TOO_LONG` |
| 重复投递同一 `message_id` | 假通道 | 结果还在就 `复用结果重发`（不重新分析）；结果已被淘汰才记「重复投递，忽略」 |
| 第二实例启动 | 真实 | 锁冲突，打印「已有实例在运行」并退出，进程数仍为 1 |

## 入口与插件边界验证记录

判定口径：真实模型调用必然成对出现 `模型调用开始` 与 `模型调用完成: model=step-3.7-flash 耗时=...s`；无该日志的是模拟分支。

| 场景 | 怎么跑的 | 真实 / 模拟 | 结果 |
|---|---|---|---|
| 插件边界：两个渠道同挂一个 Gateway | 替身业务返回 7000 字，两个假渠道分别声明单条 3000 与 20000 | 模拟 | 3000 的收到 3 段、20000 的收到 1 段；先问的那个渠道的消息不会串到另一个（回程路由正确） |
| 只开终端（命令行覆盖） | `python3 main.py --ask "..."` | 真实 | 模型 3~5s，答案打印到终端，未拉起飞书长连接 |
| 只开终端（配置关闭飞书） | `ENABLED_CHANNELS=terminal python3 main.py --ask "..."` | 真实 | 启动日志「启用入口=terminal」，无飞书连接，答案正常 |
| 终端 REPL | `printf '/help\n问题\nexit\n' \| python3 main.py --channel terminal` | `/help` 模拟、问题真实 | 逐条回复，`exit` 正常退出 |
| 两个入口同开 | `python3 main.py --channel feishu,terminal`，飞书与终端各问一句 | 真实 ×2 | 同一进程内并行：飞书 11:28:03→06（2.5s），终端 11:28:05→08（3.5s），线程名 `answer-feishu` / `answer-terminal`，各自回各自的入口 |
| 关闭终端后只开飞书 | 默认配置重启 | 真实 | 12:22:24 收到 → 12:22:28 回复成功（3.2s），飞书不受影响 |
| 非法入口名 | `python3 main.py --channel sms` | 模拟 | 启动即报 `ENABLED_CHANNELS 含未知入口 ['sms']`，不静默降级 |
| 正常处理（终端） | `python3 main.py --ask "..."` | 真实 | T00002：收到 → 开始分析 → 分析完成 3.98s → 回复发出，`--task` 能查到全过程 |
| 正常处理（飞书） | 默认配置启动后在飞书发一条消息 | 真实 | T00006：收到 → 分析完成 2.08s → 回复发出 0.8s |
| 受控发送失败 A | 假渠道 `reply` 抛 `SendError` | 真实模型调用 + 注入的发送失败 | T00003：重试 3 次（间隔 3 秒）→ 回复失败，答案 57 字已保留 |
| 复用已有结果 | 同一 `message_id` 再次到达 | 模拟（无模型调用） | T00004：`复用结果重发`，全程模型调用次数仍为 1，没有重复分析 |
| 受控发送失败 B | 真实飞书 reply API 对不存在的 `message_id` | 真实模型调用 + 真实 API 失败 | T00005：飞书返回 `code=99992354`（message_id 不存在），重试 3 次 → 回复失败，答案 79 字已保留 |
| 日志体检 | 在 `logs/im_live.log` 里搜明文凭证与聊天原文 | 模拟 | 明文连接凭证 0 条、消息与答案原文 0 条 |
| 会话规则分支（复核） | 照 README 的假通道示例重跑一遍 | 模拟（模型调用 0 次） | T00007~T00011：`/help` 回复用法、越权记「拒绝未授权」、群聊未 @ 记「忽略」、空消息 `EMPTY_QUESTION`、超长 `QUESTION_TOO_LONG` |
| 三人同时提问 | 替身业务（睡 1 秒）+ 三个不同 open_id | 模拟（不调模型） | T00012~T00014：1.02s 全部返回（串行应约 3s），各回各自的 `message_id`，不串号 |
| `--task` 复查 | `python3 main.py --task T00006` 与 `grep T00006` | 真实（读记录文件） | 四个阶段齐全，耗时与日志一致 |
| 重构后回归（日常套件） | `python3 -m unittest discover -s tests` | 模拟（不调模型、不联网） | 88 例全绿，约 2.5 秒；真实模型用例默认跳过 |
| 重构后真实模型 | `IM_LIVE_REAL_MODEL=1 python3 -m unittest discover -s tests` | 真实 ×2 | 88 例全绿，真实问答与端到端各一次 |
| 重启恢复待重发结果 | 先制造真实发送失败，再重启服务 | 真实 | T00020 真实 API 失败后结果保留；重启日志「已恢复 3 条未发出的结果」 |
| 重构后飞书真实消息 | 飞书里发一句话 | 真实 | T00021：收到 → 分析 3.12s → 回复发出 0.96s（分段=1），无重试、无失败 |

受控失败 B 的做法说明：把 `FEISHU_APP_SECRET` 改错会让长连接本身连不上（收不到消息，验证不到发送环节），所以改用「真实凭证 + 不存在的 `message_id`」让飞书回复 API 真实失败，效果等价且不用动凭证。

## 模型与耗时

之前用 `step-5-preview`，端到端要 20~50 秒；换成 `step-3.7-flash` 后降到 5 秒左右，同一条问题的实测对比：

| 模型 | 场景 | 模型耗时 | 端到端 |
|---|---|---|---|
| `step-5-preview` | 飞书私聊 | 51.7s | 约 52s |
| `step-5-preview` | 飞书私聊 | 23.4s | 约 24s |
| `step-5-preview` | HTTP 提交 | 49.5s | 50.4s |
| `step-3.7-flash` | 飞书私聊 | 4.3s | 约 5s |
| `step-3.7-flash` | 假通道 | 2.5s | 2.5s |

延迟几乎全部来自模型推理，换快模型是最直接的一档。改模型只动 `.env` 的 `STEP_MODEL` 一行；该端点当前可用模型：`step-3.5-flash`、`step-3.5-flash-2603`、`step-3.7-flash`、`step-5-preview`、`step-router-v1` 及语音系列。

## 排查失败（按阶段定位）

日志同时写控制台与 `logs/im_live.log`。先拿到任务编号，再顺着阶段看它停在哪：

```bash
grep -E "任务 T[0-9]+ (收到消息|回复失败)" logs/im_live.log | tail   # 1) 最近有哪些任务、哪些失败
python3 main.py --task T00006                                      # 2) 这一条走到哪一步
grep "回复失败" logs/im_live.log | tail                             # 3) 最近失败的有哪些
```

停在哪个阶段，就是卡在哪：

| 停在 | 含义 | 下一步 |
|---|---|---|
| `收到消息` | 没进分析 | 同一条记录里的阶段名就是原因：`拒绝未授权`（不在白名单）、`忽略`（群聊未 @、重复投递、渠道未注册） |
| `开始分析` | 已进后台线程，模型还没返回 | 看有没有成对的 `模型调用开始` / `模型调用完成`；没有就是模型通道卡住（上限 180 秒） |
| `分析失败` | 输入不合法或模型报错 | `code=EMPTY_QUESTION` / `QUESTION_TOO_LONG` 让用户改输入；`MODEL_ERROR` 查 `STEP_API_KEY` 与端点 |
| `分析完成` 之后没有 `回复发出` | 正在发送，或在重试 | 重试会记 `重试发送`，间隔 3 秒、最多三次 |
| `回复失败` | 三次都失败 | 答案已留在内存，同一条消息再来会 `复用结果重发`；具体原因看紧挨着的 `WARNING ... 回复失败: task=...` |

失败原因只在日志里记一句（不含消息与答案原文），例如真实飞书返回 `code=99992354` 表示 `message_id` 不存在。

| 现象 | 原因与处理 |
|---|---|
| 完全没有「收到消息」 | 平台没有推事件。私聊必须单独开通 `im:message.p2p_msg:readonly`（`im:message` 总 scope 不覆盖私聊），群聊 @ 是 `im:message.group_at_msg.include_bot:readonly`；权限改动后必须重新创建版本并发布 |
| 有「收到消息」但没回复 | 发送人不在 `ALLOWED_OPEN_IDS`（日志有 `拒绝未授权发送人`，回复里会带上其真实 open_id）；群聊则是未 @ 机器人 |
| 长连接反复断开 | 网络切换或休眠导致，日志会有 `keepalive ping timeout`，SDK 会自动重连 |
| 启动报「已有实例在运行」 | 先 `pkill -f "python3 main.py"` |
| `RuntimeError: .env 缺少配置` | 按「环境准备」表补齐 7 个键 |
| WebSocket 证书校验失败 | `config.py` 会在导入 `lark_oapi` 之前把 `SSL_CERT_FILE` 指向 certifi，不要调整这几行的导入顺序 |

## 平台操作位置与完成标志

| 操作位置 | 做什么 | 完成标志 |
|---|---|---|
| 开放平台 → 凭证与基础信息 | 取 App ID / App Secret 填进 `.env` | `.env` 的值与页面一致 |
| 权限管理 | 开通 `im:message.p2p_msg:readonly`（单聊收消息）、`im:message.group_at_msg.include_bot:readonly`（群聊 @）、`im:message:send_as_bot`（发消息） | 权限状态为「已开通」 |
| 事件订阅 | 订阅方式选「使用长连接接收事件」，添加「接收消息 `im.message.receive_v1`」 | 页面显示长连接，事件在已添加列表里 |
| 版本管理与发布 | 创建版本 → 申请发布 → 上线 | 页面提示「当前修改均已发布」 |
| 机器人能力 / 可用范围 | 启用机器人，可用范围包含你自己 | 飞书里能搜到 IM_Live 并发消息 |
| 本地 `.env` | `ALLOWED_OPEN_IDS` 填自己的 open_id（`ou_` 开头） | 启动日志不再出现「ALLOWED_OPEN_IDS 中存在非 ou_ 开头的条目」告警 |

两个容易漏的点：权限改动**必须重新发布版本**才生效；单聊必须单独开 `im:message.p2p_msg:readonly`，`im:message` 总 scope 不覆盖私聊事件推送（缺少它时表现是：长连接正常、心跳正常，但一条消息都收不到）。

## 重构范围与不动点

**动过的**（只调内部结构，行为不变）：

| 模块 | 变化 |
|---|---|
| `gateway.py` | 从「一个类做六件事」收敛为编排：284 → 251 行 |
| `channel_contract.py` | 新增：插件约定与入站消息，让渠道与编排不再互相依赖 |
| `session_policy.py` | 新增：会话规则判定，纯函数 |
| `outbound.py` | 新增：分段与重试，不记日志、不碰记录文件 |
| `result_store.py` | 新增：去重缓存、结果缓存、启动时恢复 |
| `task_stage.py` | 新增：阶段名与编号分配 |
| `assembly.py` | 新增：装配各层 |
| `config.py` | 内部拆成 读环境 / 解析 / 日志配置 三段 |
| `main.py` | 只管启动方式 |

**不动点**（改动即视为破坏）：

| 类别 | 内容 |
|---|---|
| 插件约定 | `name` / `max_segment_chars` / `start()` / `reply()`，发送失败抛 `SendError` |
| 公共接口 | `IncomingMessage` 字段、`Gateway(agent, allowed_open_ids, task_log, on_task_done=None)`、`handle() -> Optional[str]`、`attach_outbound`、两个渠道的构造与 `reply`/`start` |
| 可见行为 | 五阶段与中文阶段名、`--task` 输出、重试 3 次间隔 3 秒、分段规则（20000 与渠道单条上限）、白名单与群聊 @、`/help` 文案、空与超长提示 |
| 数据 | `data/tasks.jsonl` 字段名、日志脱敏规则、`.env` 键名、`ENABLED_CHANNELS` |
| 依赖 | `requirements.txt` 未变，测试只用标准库 |

**不纳入**：日志与 JSONL 轮转、限流与排队、多轮上下文、模型与端点、飞书事件归一化逻辑。

**分阶段与回滚**：每阶段一个提交，回滚用 `git revert <提交号>`，任何一步都能单独回退。

| 阶段 | 提交 | 内容 | 验证 |
|---|---|---|---|
| 0 基线 | `test: 建立特征测试基线` | 只写测试，不动产品代码 | 51 例全绿 |
| 1 | `refactor: 抽出 task_stage` | 阶段常量与编号分配独立 | 51 例全绿 + `--task` 输出不变 |
| 2 | `refactor: 拆出 channel_contract/session_policy/outbound/result_store` | Gateway 收敛为编排 | 75 例全绿 + `--ask` 真实冒烟 |
| 3 | `refactor: config 拆三段，装配收敛到 assembly.py` | main 变薄 | 80 例全绿 + `--task` 输出不变 |
| 4 | `feat: 重启后恢复未发出的结果` | **唯一行为变更** | 先红测后实现，88 例全绿 + 重启实测恢复 3 条 |

## 扩展与维护：改哪里

### 新增一个 IM（企业微信、Slack 等）

| 步骤 | 改哪里 |
|---|---|
| 1. 新增渠道文件 | 如 `wecom_channel.py`，照 `terminal_channel.py` 写，只做三件事：读输入、转 `IncomingMessage`、展示结果 |
| 2. 实现插件约定 | `name`（与 `IncomingMessage.channel` 一致）、`max_segment_chars`（单条上限，决定分段）、`start()`、`reply(target, text)`；发送失败必须抛 `SendError` |
| 3. 登记渠道名 | `config.py` 的 `CHANNEL_NAMES` |
| 4. 装配 | `main.py` 里构造 + `gateway.attach_outbound(channel)`；阻塞型入口用后台线程起 |
| 5. 可选启用 | `.env` 的 `ENABLED_CHANNELS` 加上这个名字 |

Gateway 与业务层不用动。四项约定见「渠道插件约定」表。

### 新增业务能力

只动业务层：改 `chat_agent.py` 的 `answer(question) -> str`，或在其上新增业务模块并让 Gateway 调它。渠道与 Gateway 不动；返回结果仍是字符串，分段与回程由 Gateway 与各自渠道负责。需要新配置就加进 `config.py` 的 `REQUIRED_KEYS` 与 `.env.example`。

### 多人使用

| 要做 | 改哪里 |
|---|---|
| 允许新用户 | `.env` 的 `ALLOWED_OPEN_IDS` 加 `ou_` 开头的 open_id，逗号分隔，**重启生效** |
| 群聊可用 | 群成员也要在白名单里；群里必须 @ 机器人（`FEISHU_BOT_OPEN_ID` 决定认不认这个 @） |
| 平台侧 | 权限与可用范围包含这些人，改动后必须重新发布版本 |
| 并发 | 每人一个后台线程（`answer-<channel>`），无排队与限流；实测三人同时提问 1.02s 全部返回、各自回到各自的 `message_id`（替身业务，不调模型） |
| 只允许一个进程 | `data/bot.lock`，第二个实例启动即退出 |

拿不到某人的 open_id：让他给机器人发一句话，拒绝回复里会回带他的真实 open_id，直接抄进 `.env` 即可。

### 长期在线

- **连接**：SDK 自带心跳与断线重连，状态看 `平台连接: 飞书长连接 已建立 / 断开，重连中`；需要重连时不用干预，进程退出即断开，重启恢复
- **容量**：去重缓存与结果缓存上限都是 200 条（`gateway.DEDUP_CACHE_SIZE`），滚动淘汰，不会无限增长
- **日志与记录文件没有轮转**：`logs/im_live.log` 与 `data/tasks.jsonl` 都只追加。要清就停服务后删掉，`tasks.jsonl` 删除后编号会从 `T00001` 重新开始：

```bash
pkill -f "python3 main.py"; rm -f logs/im_live.log data/tasks.jsonl
nohup python3 main.py > /tmp/im_live_stdout.log 2>&1 &
```

需要自动轮转时改 `config.setup_logging`，把 `FileHandler` 换成标准库的 `RotatingFileHandler`；`tasks.jsonl` 再另加按天切分或定期清理。
- **机器休眠 / 断网**：长连接会断开并由 SDK 重连；休眠期间平台推的事件不会补发。

## 当前状态：已完成 / 未完成 / 待验证

### 已完成

| 能力 | 状态 | 依据 |
|---|---|---|
| 飞书长连接收发消息 | 完成 | 真实消息 T00006：收到 → 分析 2.08s → 回复 0.8s |
| 终端入口、入口可配置 | 完成 | `--channel` / `--ask` / `ENABLED_CHANNELS`，两个入口可同开 |
| 渠道插件约定 | 完成 | `ChannelPlugin` 四项 + `reply` 失败抛 `SendError` |
| 凭证脱敏 | 完成 | 日志里 `access_key=***`、明文凭证体检 0 条 |
| 任务运行记录 | 完成 | 编号 `T00001` 起、五阶段、`--task` 查询，重启续号 |
| 发送失败重试与结果保留 | 完成 | T00003 重试 3 次 → 回复失败；T00004 复用结果重发（模型调用仍 1 次）；T00005 真实 API 失败同样保留 |
| 结果跨重启保留 | 完成 | 重启时从 `tasks.jsonl` 恢复未发出的结果；实测恢复 3 条，同一条消息再来直接重发、不重新分析 |
| 重构 + 特征测试 | 完成 | gateway 284 → 251 行；88 个用例全绿（2 个真实模型用例默认跳过），日常套件约 2.5 秒；重构后飞书真实消息 T00021 一次成功 |

### 未完成（已知缺口，本轮没做）

| 缺口 | 说明 |
|---|---|
| 日志与记录文件无轮转 | `logs/im_live.log`、`data/tasks.jsonl` 只追加，长期在线会一直变大；清理方式见上 |
| 重复投递会再发一次答案 | 结果记住后，同一条消息再来会 `复用结果重发`（不重新分析）；若希望「重复投递一律忽略」，要改 `session_policy` 的判定 |
| 没有限流与排队 | 多人同时提问会并发调模型，平台侧限流未考虑 |
| 没有多轮上下文 | 每次提问独立，不带历史对话 |
| `gateway.py` 仍偏长 | 251 行（原 284），剩下的主要是分支分派与阶段记录；再压就要把分派也挪走，可读性反而下降 |

### 待验证（本轮没实测）

| 事项 | 为什么没做 |
|---|---|
| 断线重连后自动恢复 | 需要断网或让机器休眠，代价高；SDK 自带重连，连接状态有日志可观察 |
| 数小时以上连续运行 | 需要长时间挂机观察稳定性与日志增长 |
| 群聊真实 @ | 此前只用构造事件验过归一化，没在真实群里 @ 过 |
| 飞书里多人同时提问 | 已用替身业务并发验证线程模型（三人 1.02s、不串号），真实多人并发未做 |

## 发布到 GitHub

新建仓库表单：

| 字段 | 值 |
|---|---|
| Owner | 你的账号（默认） |
| Repository name | `im-live-bot` |
| Description | `飞书机器人：长连接收消息，调用阶跃星辰模型直接回答（Python 单进程）` |
| Visibility | Public（仓库新建、无历史提交，`.env` 已被忽略；不放心就先 Private） |
| Add README | Off（本文件会随推送上去，勾了会冲突） |
| Add .gitignore | No .gitignore（已有） |
| Add license | MIT License（已带 `LICENSE` 文件，可不加） |

推送前确认没有密钥和本地数据：

```bash
cd IM_live
git init
git add .
git status --short                 # 列表里不应出现 .env、data/、logs/
git check-ignore -v .env data/bot.lock logs/im_live.log
git commit -m "feat: 飞书机器人接入阶跃星辰模型"
git branch -M main
git remote add origin git@github.com:<你的账号>/im-live-bot.git
git push -u origin main
```

`data/` 里有过期的 `tasks.db`（含飞书 open_id 与旧分析记录）和 `bot.lock`，`logs/` 是运行日志，三者都已在忽略名单。**别把 `.env` 传上去**——换机器只需按「环境准备」表重建一份。

## 许可

MIT，见 `LICENSE`。
