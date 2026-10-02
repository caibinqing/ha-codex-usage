# 备选方案：每日 Token 用量接口

> 状态：**仅调研，暂未接入**（2026-10-02）。留作以后增加 Token 用量传感器时参考。

## 接口

```
GET https://chatgpt.com/backend-api/wham/profiles/me
```

请求头与现有的 `wham/usage` 调用相同：

- `Authorization: Bearer <access_token>`
- `ChatGPT-Account-Id: <account_id>`
- `User-Agent: codex-cli`（可选，Codex CLI 默认值）

## 来源

在 [openai/codex](https://github.com/openai/codex)（commit `4cedd0c`，2026-10-02）中确认：

- `codex-rs/backend-client/src/client.rs`：`token_usage_profile_url()`，
  base URL 含 `/backend-api` 时使用 `/wham/profiles/me`，否则使用 `/api/codex/profiles/me`。
- `codex-rs/backend-client/src/types.rs`：`TokenUsageProfile` / `TokenUsageProfileStats` /
  `TokenUsageProfileDailyBucket`。
- `codex-rs/app-server/src/request_processors/account_processor.rs`：App Server 的
  `account/usage/read` RPC 就是调用该接口，并把字段转成 camelCase（`dailyUsageBuckets`）。

## 返回结构

根据源码类型定义与测试用例整理，所有字段均可能缺失，最小返回为 `{"stats":{}}`：

```json
{
  "profile": { "display_name": "...", "username": "..." },
  "metadata": { "stats_as_of": "2026-09-09" },
  "stats": {
    "lifetime_tokens": 142300000000,
    "peak_daily_tokens": 7000000000,
    "longest_running_turn_sec": 158760,
    "current_streak_days": 158,
    "longest_streak_days": 160,
    "daily_usage_buckets": [
      { "start_date": "2026-09-09", "tokens": 42 }
    ],
    "fast_mode_usage_percentage": 37.5,
    "most_used_reasoning_effort": "high",
    "total_threads": 0
  }
}
```

可派生的传感器：今日 Token、近 7 天 Token、累计 Token（`lifetime_tokens`）、单日峰值、连续使用天数。

## 注意事项

- **尚未用真实 token 调用验证**，接入前先确认实际返回。
- `start_date` 按 UTC 还是本地时区切分未知。
- 当天的桶可能有延迟。

## 参考：CodexMeter 的做法

[tomatoeggs/CodexMeter](https://github.com/tomatoeggs/CodexMeter) 不直接调 HTTP，而是在电脑上常驻
daemon，每 60 秒启动 `codex app-server --listen stdio://`，通过 JSON-RPC 调用
`account/rateLimits/read` 与 `account/usage/read`。当天桶缺失时，再增量读取本机
`~/.codex/sessions/**/*.jsonl` 中的 `token_count` 事件补齐今日用量。

这种方式依赖本机安装并登录 Codex CLI，不适合本集成；本集成直接调用上面的 HTTP 接口即可。
