# IM_live 飞书机器人

用飞书自建应用的机器人：收到消息后调用阶跃星辰模型，把答案直接回复到原会话。单进程、最少依赖，没有任务单、没有数据库、没有 HTTP 接口。

## 分层

| 层 | 文件 | 职责 |
|---|---|---|
| 平台适配器 | `feishu_channel.py` | 建立飞书长连接、把消息归一化为 `IncomingMessage`、`reply` 回原消息。不含业务规则 |
| 终端渠道 | `terminal_channel.py` | stdin 一行一个问题，答案打印到 stdout。与飞书渠道遵守同一套约定 |
| Gateway | `gateway.py` | 会话规则：白名单、群聊 @ 判断、重复投递拦截、`/help`；收到消息后起后台线程调模型并回答案 |
| 业务能力 | `chat_agent.py` | 输入校验与提示词拼装，暴露 `answer(question) -> str`。不认识飞书，也不认识会话 |
| 模型通道 | `llm_client.py` | 通过 OpenAI 兼容端点调用 StepFun，超时 180 秒 |
| 入口 | `main.py` | 装配各层，抢占单实例文件锁，主线程跑长连接 |
| 配置 | `config.py` | 修正根证书路径、加载并校验 `.env`、配置日志 |

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

约定落在 `gateway.py` 的 `ChannelPlugin` 上，共四项：

| 约定项 | 作用 | 飞书 | 终端 |
|---|---|---|---|
| `name` | 渠道名，与 `IncomingMessage.channel` 一致，Gateway 靠它把答案路由回来源入口 | `feishu` | `terminal` |
| `max_segment_chars` | 该渠道单条消息长度上限，决定超长答案怎么分段 | 3000（飞书单条限制） | 20000（等于答案总上限，即不分段） |
| `start()` | 开始接收本渠道的消息 | WebSocket 长连接阻塞运行 | REPL 循环读 stdin |
| `reply(target, text)` | 展示结果 | 按 `message_id` 回复到原消息 | 打印到 stdout |

装配方式：`gateway.attach_outbound(channel)` 按名字注册，**可以挂多个**。两个入口共用同一个 Gateway、同一个业务对象、同一份去重缓存；答案只回到提问的那个入口。

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

正常启动会打印：配置加载完成（含「启用入口=...」）→ 飞书入口已启动 → `connected to wss://msg-frontier.feishu.cn/ws/v2`。

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
收到消息: channel=feishu ... sender=ou_xxx 文本长度=9
模型调用开始: model=step-3.7-flash 提示词长度=114
模型调用完成: model=step-3.7-flash 耗时=4.3s 输出长度=63
回复成功: message_id=om_xxx
答案已回发: message_id=om_xxx 分段=1
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

cfg = config.load_config()
config.setup_logging(cfg.log_path)

class FakeChannel:
    def __init__(self): self.log = []
    def reply(self, message_id, text): self.log.append(text)

fake = FakeChannel()
gw = Gateway(ChatAgent(StepFunClient(cfg.step_api_key, cfg.step_base_url, cfg.step_model)),
             cfg.allowed_open_ids)
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
print(fake.log)
PY
```

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
| 重复投递同一 `message_id` | 假通道 | 日志「重复投递，忽略」，不重复回答 |
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

## 日志排障

日志同时写控制台与 `logs/im_live.log`，按三段定位：

```bash
grep "收到消息"        logs/im_live.log | tail    # 1) 事件有没有进来
grep "模型调用完成"     logs/im_live.log | tail    # 2) 模型有没有跑完、耗时多少
grep -E "回复成功|回复失败" logs/im_live.log | tail # 3) 回复有没有发出去
```

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
