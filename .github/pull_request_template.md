## 对应任务

- TASK / GOV / HARDENING：
- Issue：
- base：`integration/auto-buyer-offer`
- execution mode：`GitHub-only` / `Local-required`
- 风险等级：LOW / MEDIUM / HIGH / CRITICAL
- exact accepted source/tree SHA：
- exact head SHA：

项目固定事实见 [`.agent/PROJECT_CONTEXT.md`](../.agent/PROJECT_CONTEXT.md)，流程见 [`.agent/WORKFLOW.md`](../.agent/WORKFLOW.md)。

## 角色与发布状态

- Sol / Web GPT：同一个技术总控与最终技术验收角色；负责 exact-source、commit、PR、CI、merge-to-integration 和 post-merge review。
- Luna：仅在 `Local-required` 时提供本机实现/测试/source-tree handoff 证据；不负责最终技术验收和常规 GitHub 发布。
- OWNER：真实平台写操作、integration → `main` 和重大范围/外部风险的独立授权者。
- integration 内 task publication：技术门禁通过后可由 Sol 连续推进，无需逐步重复 OWNER 授权。

## 修改范围

<!-- 列出实际任务白名单文件；必须与 Issue 冻结范围一致。 -->

## 未修改范围

确认未修改：任务禁止范围、无关业务/测试/依赖/配置、`main` 以及 Issue 未授权文件。

## 行为变化

<!-- 文档-only 也明确写“无 runtime 行为变化”。 -->

## 当前轮验证

- 环境/执行位置：
- target：
- historical regression：
- host regression：
- full suite：
- baseline：
- `git diff --check` / static scan：
- 真实 Steam/BUFF 请求：

只填写本轮实际执行结果；历史结果如需引用必须明确标为 historical evidence。

## Local-required source handoff

<!-- GitHub-only 填 N/A。 -->

- workspace：
- base SHA / base tree：
- unreferenced source tree SHA：
- exact paths / hashes：
- remote read-back：PASS / FAIL / N/A
- line-ending / minimal-diff check：PASS / FAIL / N/A
- 受保护 checkout 未触碰：PASS / FAIL / N/A

## Historical + Simplicity Review

- [ ] 只完成当前 TASK。
- [ ] 无重复状态机/执行器/写入权威。
- [ ] 无纯未来需求抽象。
- [ ] host touchpoints 最小。
- [ ] 安全复杂度有当前不变量依据。
- [ ] 历史依赖/架构兼容。

结论与已知债务：

## Exact source / commit review

- commit SHA：
- parent SHA：
- exact changed paths：
- GitHub-native diff：PASS / CHANGES_REQUESTED
- Sol technical verdict：PASS / CHANGES_REQUESTED / BLOCKED

## CI

- exact head CI run：
- result：passed / failed / not_configured
- baseline：

## 安全确认

完整规则见 [`AGENTS.md`](../AGENTS.md)。

- [ ] 未泄露 credentials/secrets。
- [ ] 未用跳过/弱化测试换取 PASS。
- [ ] 未执行未授权的真实平台写请求。
- [ ] 不可幂等未知结果没有自动重发。
- [ ] TASK 安全不变量保持。

## Merge 与 post-merge

- merge SHA：
- integration HEAD：
- post-merge CI：
- base → merge exact diff：PASS / FAIL
- Mandatory Historical + Simplicity Review：PASS / CHANGES_REQUESTED / BLOCKED

只有 post-merge 门禁完成后才关闭 TASK。integration → `main` 不因本 PR 的 integration merge 自动获得授权。

## 风险 / 回滚 / 未解决问题

- 风险：
- 回滚：
- 未解决问题：
