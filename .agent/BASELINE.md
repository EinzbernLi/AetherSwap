# TASK-003 测试基线

- TASK：TASK-003
- Issue：[#10](https://github.com/EinzbernLi/AetherSwap/issues/10)
- 基线分支：`integration/auto-buyer-offer`
- 开始执行时 base SHA：`6ca16d039866742029ddbbe1faa0d39e14628ff4`
- 执行分支：`luna/TASK-003-ci-contract-cleanup`
- 风险等级：MEDIUM

## CI 环境与命令

- 运行平台：GitHub Actions `ubuntu-latest`
- Python：workflow 使用 `3.12`
- pytest：`pytest==9.1.1`
- 安装命令：`python -m pip install -r requirements.txt`；`python -m pip install -r requirements-ci.txt`
- 测试命令：`python -m pytest -q --junitxml=pytest.xml`
- baseline gate：要求 pytest 退出码为 0、JUnit 总测试数为 447、failed/error/collection error/skipped 均为 0，并登记 0 个 registered failure。

## 变更范围

- `tests/test_buff_verification.py`：分别覆盖 GET 安全验证和 POST 非幂等写请求验证，并断言每类请求的底层调用次数严格为 1。
- `.github/workflows/python-ci.yml`：固定安装 pytest 版本并启用严格零失败门禁。
- `requirements-ci.txt`：固定 `pytest==9.1.1`。
- 未修改生产代码、生产依赖、BUFF/Steam 写操作或其他测试文件。

## 结果记录

- 最终精确 PR head SHA 与对应 CI run：以 Draft PR 正文及网页端 GPT 验收记录为准，避免文档提交产生 SHA/run 自引用循环。
- 未执行真实 BUFF/Steam 写操作。