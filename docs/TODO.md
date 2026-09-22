# 待办 / 暂缓事项（TODO）

> 只记录**已评估但暂不实施**的事项（含结论与触发条件），避免重复讨论。
> 已实现的能力见 [README](../README.md)；各 provider 的实测见 `docs/PROVIDER_*.md`。

| # | 事项 | 状态 | 结论 / 触发条件 | 详见 |
|---|---|---|---|---|
| 1 | **配置热重载 + UI 动态启用/停用** | 暂缓 | 改 `config.yaml` 现在只需 `docker compose restart`（**不需 rebuild**）；热重载能让"调选择器/切开关"秒级生效。硬约束：路由闭包持有 registry → 必须**就地重建**；需 `gate.busy` 拦在途请求 + `close_all` 会话。触发：开关/选择器调整频繁到 20s restart 成为明显摩擦（约半天，3 个提交） | [`CONFIG_HOT_RELOAD.md`](CONFIG_HOT_RELOAD.md) |
| 2 | **按 provider 区分 headless（能力声明）** | 暂缓 | Docker 下已是"无打扰"（共享 headful + Xvfb）；分组会让 Chrome 进程翻倍而 Xvfb 省不掉 → 收益只在本地开发体验。更优形态是 provider 声明 `requires_headful` 由系统自动决策。触发：本地频繁用 chatgpt 被打扰 / 更多 must-headful 站点 / 需按 provider 定制浏览器参数 | [`PER_PROVIDER_HEADLESS.md`](PER_PROVIDER_HEADLESS.md) |
| 3 | **GLM「常驻页面自动续期」验证** | **决定不追** | WAF 票据 TTL 实测 ≈30 分钟；若只在临近过期刷新，则必须看满一个周期才能判定（短时间测=假阴性）；且页面常驻与本项目 thread 生命周期（TTL 900s、按需重建）冲突 → 收益小、复杂度高。长期方案用**智谱官方 API** | [`PROVIDER_GLM.md`](PROVIDER_GLM.md) §「未验证的设想」 |
| 4 | **`options.widget_png`（把组件截图也返回给 API 客户端）** | 未做 | 组件已按 `widget_capture` 落盘（html+png），Playground 可交互/切截图；OpenAI 侧默认只给链接（避免响应体膨胀）。触发：有客户端确实需要"图片形式"的组件 | [`WIDGET_CAPTURE.md`](WIDGET_CAPTURE.md) C4 |
| 5 | **Kimi/豆包「过程性文本」过滤**（工具调用前的过渡句） | 未做 | 那些是页面上模型**真实可见**的文字（非思考），删除有误伤正文的风险 → 默认保留。触发：有明确用例证明它污染了正文 | [`PROVIDER_KIMI.md`](PROVIDER_KIMI.md) §4 |
| 6 | **保真度测试接入每日流程** | 已有脚本 | `./scripts/text_fidelity.sh`（退出码可给 cron 告警，结果写 `reports/*.jsonl`）；尚未配置到任何机器的定时任务 | [`TEXT_FIDELITY.md`](TEXT_FIDELITY.md) §3 |
