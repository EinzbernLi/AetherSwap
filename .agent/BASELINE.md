# TASK-001 测试基线

- TASK：TASK-001
- Issue：[#3](https://github.com/EinzbernLi/AetherSwap/issues/3)
- 基线分支：`integration/auto-buyer-offer`
- 基线预期 SHA：`2d2d4f1d864591af308edc84ff41b70bd29d4e7a`
- 执行分支：`luna/TASK-001-ci-baseline`
- 记录日期：2026-08-06

## 环境与命令

- Python：`3.12.5`
- pytest：`8.4.2`
- 安装命令：`python -m pip install -r requirements.txt`
- 安装结果：命令成功；依赖均显示 `Requirement already satisfied`，未升级生产依赖。
- 测试命令：`python -m pytest --collect-only -q`
- 收集结果：`446 tests collected in 3.91s`
- 全量命令：`python -m pytest -q`

## 全量结果

- 总数：446
- 通过：445
- 失败：1
- 跳过：0
- 失败测试：`tests/test_buff_verification.py::test_make_request_raises_verification_required`
- 失败原因：测试期望写请求的验证响应抛出 `BuffVerificationRequired`，当前实现按写请求安全契约抛出 `BuffWriteResultUnknown`；本任务不修改业务代码或测试。
- 警告：1 个 `InsecureRequestWarning`，来自既有测试的本地代理请求。
- 未执行真实 BUFF/Steam 写操作。

## 真实性与限制

测试在本机只读代码副本 `F:\AetherSwap` 上执行；该目录的本地 Git 远端不是目标仓库，且本机对 `EinzbernLi/AetherSwap` 的 Git HTTPS clone 被 Schannel 凭据错误阻断，因此本次未能对远程基线 SHA 做本地 checkout 后再运行测试。远程分支引用通过 GitHub API 已核验精确指向预期 SHA。CI 将在 GitHub runner 对本 PR 的实际提交执行同一安装和测试命令。

已知失败被原样保留，未跳过、删除或弱化测试。