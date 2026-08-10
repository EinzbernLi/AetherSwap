# AetherSwap 项目固定上下文

本文件保存跨 TASK 稳定、需要长期复用的项目事实。TASK 目标、临时 workspace、候选 SHA 和测试结果属于各自 Issue/PR，不在这里重复。

## 仓库与分支

- 工作仓库：`EinzbernLi/AetherSwap`
- 上游 / source：`VexedWilosn/AetherSwap`
- Auto Offer 集成分支：`integration/auto-buyer-offer`
- `main`：最终产品分支；从 integration 推进到 `main` 仍需 OWNER 明确批准。

## 行为参考项目

- 参考仓库：`Steamauto/Steamauto`
- 当前 clean-room 行为参考基线：`e803e1ab00cfcede6ef8a7f1b9e784f9bb8da25a`
- 用途：只参考公开行为、接口和状态语义。
- 禁止：复制参考项目源码、vendor 参考实现、形成 Steamauto / steampy runtime dependency。
- 若某个 TASK 需要新的参考基线，应在该 TASK 的证据文档/Issue 中明确锁定，不要静默改写历史证据。

## GitHub 与本机职责边界

GitHub 是唯一持久事实来源：任务设计、冻结范围、历史决策、验收证据、source handoff、commit、branch/ref、PR、CI 和 merge 结果都必须可从 GitHub 恢复。

本机 workspace 只是执行载体，不是长期事实来源。能在 GitHub 完成的工作默认由 Sol 直接完成；只有确实依赖 OWNER Windows 环境的实现、复现、测试或文件处理才交给 Luna。

## Windows 本机环境

- 共享 verifier：`E:\python\python.exe`
- 受保护历史 checkout：`F:\AetherSwap`
- `F:\AetherSwap` 不得被任务 clean、reset、stash、delete、覆盖、移动或作为实验 workspace 使用。
- 本机 TASK 必须使用隔离 workspace/worktree。
- 优先从可信、长期保持干净的 base repo 创建 fresh `git worktree`；只有 base repo 不存在、损坏或来源不可信时才重新 clone。
- 具体 TASK workspace 路径属于该 TASK 的 Issue 证据，不写死在本文件。

## 角色入口

角色、权限和安全规则见 [`AGENTS.md`](../AGENTS.md)。完整任务流程见 [`WORKFLOW.md`](WORKFLOW.md)。

Luna 使用的具体模型/思考等级属于 Sol 的执行时路由决定，不是仓库持久事实；不要把模型等级重复写入可复用 TASK prompt 或仓库任务规范。
