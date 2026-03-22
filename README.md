# qq-onebot-channel

一个基于 **NapCat / OneBot11** 的 `nanobot` QQ channel 单文件实现。

它的目标是替代原本依赖官方 QQ Bot 方案的实现，让 `nanobot` 可以通过 **OneBot webhook + HTTP API** 的方式接入 QQ。

## 使用方法
将其替换原本channel/qq.py


## 需要配置的参数

`QQConfig` 里主要需要关注这些参数：

```python
enabled: bool = False

# OneBot HTTP API
base_url: str = "http://127.0.0.1:6098"
token: str = ""
trust_env: bool = False

# Webhook listener for inbound events from OneBot/NapCat
listen_host: str = "0.0.0.0"
listen_port: int = 8092
event_path: str = "/onebot/event"

# Bot account QQ number (optional, auto-learn from events)
self_id: str = ""

# nanobot allowlist
allow_from: list[str] = Field(default_factory=list)

# Outbound progress / tool hints
send_progress: bool = True
send_tool_hints: bool = False
```

### 参数说明

- `enabled`：是否启用该 channel
- `base_url`：NapCat / OneBot HTTP API 地址
- `token`：OneBot access token
- `trust_env`：是否继承系统代理环境变量
- `listen_host`：webhook 监听地址
- `listen_port`：webhook 监听端口
- `event_path`：NapCat 上报事件的路径
- `self_id`：机器人自己的 QQ 号
- `allow_from`：允许接入的来源列表
- `send_progress`：是否发送进度消息
- `send_tool_hints`：是否发送工具提示



## 如何配合 NapCat / OneBot 使用

1. 在 NapCat 中启用 **HTTP Client / HTTP 上报**
2. 将上报地址设置为：

```text
http://localhost:8092/onebot/event
```

如果 `nanobot` 和 NapCat 不在同一台机器，请改成 `nanobot` 实际可访问的地址。

3. 记录 NapCat 配置的 token，并填入 `qq.py` 对应配置中的 `token`
4. 确保 `base_url` 指向 NapCat 的 HTTP API 地址，例如：

```text
http://127.0.0.1:6098
```

5. 重启 `nanobot`

原则上，其他兼容 OneBot11 的实现也可以接入，只要接口行为与 NapCat 基本一致。

## 当前使用到的 OneBot / NapCat API

代码目前会调用这些接口：

- `/send_private_msg`
- `/send_group_msg`
- `/upload_private_file`
- `/upload_group_file`
- `/get_private_file_url`
- `/get_group_file_url`
- `/get_file`


## 依赖

这个文件不是完全独立运行的脚本，它依赖 `nanobot` 运行时中的这些模块：

- `nanobot.bus.events.OutboundMessage`
- `nanobot.bus.queue.MessageBus`
- `nanobot.channels.base.BaseChannel`
- `nanobot.config.paths.get_media_dir`
- `nanobot.config.schema.Base`

同时还依赖这些第三方库：

- `aiohttp`
- `httpx`
- `pydantic`
- `loguru`

## 本项目由nanobot完成