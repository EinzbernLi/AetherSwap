# TASK-001 测试基线

- TASK：TASK-001
- Issue：[#3](https://github.com/EinzbernLi/AetherSwap/issues/3)
- 基线分支：`integration/auto-buyer-offer`
- 基线预期 SHA：`2d2d4f1d864591af308edc84ff41b70bd29d4e7a`
- 执行分支：`luna/TASK-001-ci-baseline`
- CI 权威 head：`beb048635f8b06ed3bc93618586249f9171e0189`
- CI 权威 run：[31072221548](https://github.com/EinzbernLi/AetherSwap/actions/runs/31072221548)
- CI job：`tests`，结论：`success`
- 记录日期：2026-08-06

## CI 环境与命令

- 运行平台：GitHub Actions `ubuntu-latest`
- Python：workflow 使用 `3.12`
- 安装命令：`python -m pip install -r requirements.txt`；`python -m pip install pytest`
- 安装结果：CI job `Install dependencies` 成功；pytest 显式安装，未修改生产依赖版本。
- 测试命令：`python -m pytest -q --junitxml=pytest.xml`
- 门禁：解析 CI 生成的 JUnit XML，要求总测试数为 446，失败集合严格等于登记失败集合，且 error 集合为空；否则 job 失败。

## CI 权威结果

- 总数：446
- 通过：445
- 已登记失败：1
- error：0
- collection error：0
- 跳过：0
- 已登记失败：`tests.test_buff_verification::test_make_request_raises_verification_required`
- 失败原因：测试期望写请求的验证响应抛出 `BuffVerificationRequired`，当前实现按写请求安全契约抛出 `BuffWriteResultUnknown`；本任务不修改业务代码或测试。
- baseline gate：成功；观察到的失败集合与登记集合完全一致。
- 未执行真实 BUFF/Steam 写操作。

## 真实性与限制

上述数量、失败集合和门禁结论来自 GitHub Actions 对实际 PR head `beb048635f8b06ed3bc93618586249f9171e0189` 的权威 job，不使用无法证明同 SHA 的本地结果作为最终基线。已知失败被原样保留，未跳过、删除或弱化测试。