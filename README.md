# Codex Usage for Home Assistant

一个极简的 Home Assistant 自定义集成，用于跟踪 OpenAI Codex（ChatGPT 订阅）
的限额用量。

它轮询 Codex 客户端使用的同一个未公开接口
（`https://chatgpt.com/backend-api/wham/usage`），提供以下传感器：

| 传感器 | 说明 |
| --- | --- |
| 5-Hour Usage | 滚动 5 小时窗口已用百分比 |
| 5-Hour Reset Time | 5 小时窗口重置时间 |
| Weekly Usage | 周窗口已用百分比 |
| Weekly Reset Time | 周窗口重置时间 |
| Plan Type | ChatGPT/Codex 套餐类型（如 `plus`、`pro`） |

## 数据质量

用量接口偶尔会返回瞬时异常值。集成会先验证百分比是否为 `0..100` 内的
有限数字；当一次读数较上次可信值下降至少 5 个百分点，或突然上升至少
50 个百分点时，会等待 5 秒并额外取样一次。只有新旧样本形成一致趋势时
才会发布，否则该轮用量显示为 `unknown`，避免异常数值进入历史统计。

这项复核不依赖 reset time，因此提前 reset 也能被连续低位样本确认。正常
轮询不会增加请求；primary 和 secondary 同时异常时也只会共享一次复核请求。

## 安装

把 `custom_components/codex_usage` 复制到 Home Assistant 的
`config/custom_components/` 目录下，然后重启 Home Assistant。
（也可以在 HACS 中把本仓库添加为自定义仓库。）

## 配置

1. 在使用 Codex CLI 的机器上运行 `codex login`（如尚未登录）。
2. 打开 `~/.codex/auth.json`（Windows 为 `%USERPROFILE%\.codex\auth.json`），
   复制其完整内容。
3. 在 Home Assistant 中进入 **设置 → 设备与服务 → 添加集成 → Codex Usage**，
   粘贴该 JSON。
4. 如果 Home Assistant 无法直连 OpenAI，可在配置表单或选项中设置出站代理
   （格式 `http://host:port`，不支持 `https://` 代理）。

Home Assistant 会保存自己的一份令牌副本，并通过 Codex CLI 的公开 OAuth
客户端自动续期；除非 refresh token 被吊销（此时集成会提示重新认证），
否则无需再次粘贴。

## 注意事项

- 用量接口不是有文档的公开 API，OpenAI 随时可能变更。
- 令牌刷新可能导致服务端轮换 refresh token。如果配置后本机 Codex CLI
  登录失效，重新运行 `codex login` 即可——此后两份凭据相互独立。
