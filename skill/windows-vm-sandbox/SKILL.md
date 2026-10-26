---
name: windows-vm-sandbox
description: 在 Linux/KVM 上从可复用 Windows 基础镜像创建隔离 VM，管理多实例的独立账户、资源、端口和磁盘，删除后重新开出干净实例；也支持已有 Windows VM 的资源、账户调整及 WinRM 操作。
---

# VirtOps：基础镜像与独立实例

默认生命周期是 **一次制作只读基础镜像 → 多次创建独立 VM → 删除实例 → 从基础镜像重新创建**。新实例不建立 C 盘恢复 WIM，不使用原机账户、TPM、NVRAM 或证书。每个实例自己的磁盘、账户与运行状态可以完整丢弃，基础镜像和其他实例不受该生命周期操作影响。

Agent 负责在本 Skill 的抽象层内执行；复用自带命令，不在用户工作目录拼装一次性的 resize、改账户、clone、cleanup 脚本。Profile、凭据和操作记录是运行数据，保存在工程及已安装 Skill 之外。缺少能力时修改工程并验证，而不是绕过其身份/资源校验。

| 用户意图 | 先读 | 自带入口 |
|---|---|---|
| 从基础镜像开一台或几台 VM、删除后重开 | [基础镜像与实例](references/images-instances.md) | `instance.py create/rebuild`、`destroy.py` |
| 制作一次可复用系统镜像 | [基础镜像与实例](references/images-instances.md) | `template.py generalize/capture/inspect` |
| 想知道某一步为什么停、停了怎么续作、峰值容量怎么算 | [实例生命周期](references/instance-lifecycle.md) | `instance.py create --resume`、`prepare_host.py --stage check` 的 `capacity_plan` |
| 首次准备 Linux 宿主或专用 Windows 构建 VM | [初始化](references/initialize.md) | `prepare_host.py`，仅构建 VM 使用 ISO 安装 |
| 调整已有 VM 的 CPU/内存、保留系统改用户密码 | [维护](references/configure.md) | `configure.py`、`account.py` |
| 查看、启停、日志、查询全部 VM | [日常管理](references/operate.md) | `host.py`、`status.sh --all` |
| 查看 VM 主动对外连接被阻断的记录 | [日志和网络计数](references/operate.md#日志和网络计数) | `host.py logs --network-only`、`host.py counters` |
| 上传、执行、取回 Windows 文件 | [Windows 操作](references/windows-agent.md) | `guest.ps1`、`credentials.ps1` |
| 诊断失败或升级旧运行时 | [排错](references/troubleshooting.md)、[日常管理](references/operate.md) | `host.py inspect`、`upgrade_runtime.py` |
| 明确维护旧式 C-only reset 实例 | [旧版恢复](references/reset.md)、[旧版换人](references/reprovision.md) | 仅现有兼容入口，不应用于新模板实例 |

字段含义见 [profile](references/profile.md)。GPU 需求不明确时读 [GPU 分支](references/gpu.md)，不能擅自抢占宿主 GPU。历史 `winvm01` 只用于 [案例排障](references/current-instance.md)，其参数不是新实例默认值。

## 执行边界

- 继承用户已给出的选择；合理的实例名、UUID 等可由 agent 生成。CPU、内存、磁盘预算、账户、GPU、管理入口等会改变结果的关键选择缺失时集中询问。用户说“沿用配置”即可复用非秘密配置，不重复追问。
- 每实例登记唯一 UUID/root、MAC、bridge、/30、端口及 CPU 预留。检查所有实例和其他宿主任务的总预算；CPU/内存上限不是物理资源预留，也不保证没有性能竞争。
- 同一只读模板通过 `qemu-img convert` 物化为独立 qcow2；默认不使用 backing chain。固件变量来自厂商模板，TPM 状态全新生成。实例间主动网络访问默认拒绝。
- `create` 无 `--apply` 时只检查；`destroy/rebuild --apply --discard` 完整清除该实例的系统、软件和数据。用户明确要求删除/重建即已授权此范围，不追加形式化许可。旧实例目录与共享根重合时阻断通用销毁，不递归删除共享根。
- 密码只经安全提示、DPAPI 或 root-only 文件传递，不进入 profile、Git、命令行、README 和日志。每实例选定管理员供桌面和 WinRM 共用，不额外创建 agent 账户。
- 网关规则负责阻断未批准的出站连接，采集器只记录该实例发起的单播拦截样本。过滤宿主流量、广播/组播和管理回复，保留目标 IP、端口、协议、时间等元数据，不采集报文载荷；日志采样不代表全部连接动作。
- 阶段 JSON 记录 `stage/target/evidence/next_action/needs_user_input`；同阶段失败保留现场和续作记录，不重复格式化或盲目重装。
- 分清已验证层次：XML/磁盘/身份/资源测试、Windows 启动、实际 WinRM 认证与上传哈希、RDP 真实桌面、实例间和出站网络阻断。模拟测试不能替代现场开出两台 VM 的验收。

结果只需说明：目标和动作、验证证据、任何明确未验证项或阻塞。
