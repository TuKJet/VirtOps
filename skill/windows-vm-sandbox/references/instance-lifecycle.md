# 实例生命周期：从构建机到独立实例

本页把「做一次镜像 → 开多台实例 → 删掉再开」拆成阶段，说明每阶段的输入、输出、失败表现和续作方法，并给出容量峰值与执行顺序。命令语义以 [基础镜像与实例](images-instances.md) 为准，本页补的是**流程知识**：每一步为什么会停、停了以后该做什么。

## 两条路径

```
构建路径（一次，产出只读镜像）            实例路径（可重复，产出独立 VM）
─────────────────────────────         ─────────────────────────────
prepare_host check                      instance.py create
  └ 容量预检：单台门槛 + 同宿主余量        └ storage   预分配 store（占满）
prepare_host storage                    └ seed      写入安装答案
  └ posix_fallocate 占满 storage_gib     └ activate  隔离 + define + start
prepare_host seed                       └ booting   等 QGA 就绪（可能很久）
  └ 生成含答案的 seed ISO               └ authenticated  认证 + 固定证书
prepare_host activate                   └ finishing 清理 + 重启 + 复验
  └ 隔离、define、start → Windows 装机   └ running   写入 instance.json
  └ 等待：QGA 不通是正常的               template.py generalize
  └ 验收：QGA/WinRM/RDP/资源              └ 需要 running + QGA 可用
prepare_host finish                     template.py capture
  └ 清答案文件、停机、换最终 XML、重启      └ 需要 shut-off，离线读盘
template.py generalize                  instance.py create（下一台）
  └ 需要 running；Sysprep 后自行关机
template.py capture
  └ 需要 shut-off；发布 0444 只读镜像
destroy builder
  └ 释放 store；capture 之后立即做
```

关键：**`generalize` 需要运行中的构建机，`capture` 需要已关机的构建机**。两者对电源状态的要求相反，中间必然有一次「Sysprep 触发关机」。`generalize` 自己会等关机（最多 300 秒）并返回 `generalized_source_stopped`，但它**不会等 QGA 恢复**——`prepare_host finish` 会停机再启动，此后要等 guest agent 重新应答，`generalize` 才能执行。

## 阶段、失败表现与续作

| 阶段 | 入口 | 成功输出 | 典型失败表现 | 该做什么 |
|---|---|---|---|---|
| check | `prepare_host.py --stage check` | `plan_ready` + `capacity_plan` | `NEEDS_PACKAGES` / `NEEDS_DOWNLOAD` / `NEEDS_USER_INPUT`（容量冲突） | 补输入后重跑 check；不写盘，可反复跑 |
| storage | `--stage storage --apply` | `completed` | root 已存在、挂载/格式化失败 | 脚本拒绝覆盖 root；先查部分状态，禁止自动重试格式化 |
| seed | `--stage seed --apply` | `completed` | 答案文件已写但 ISO 未生成 | 不重新生成密码；核对同一份 seed 是否完整 |
| activate | `--stage activate --apply` | `completed` | 定义已存在、prepare hook 未执行 | 按实际状态修原阶段，不覆盖盘 |
| 装机等待 | `guest_status.py` | `provision_success=true` | 连续返回 `not_ready` | **这是正常等待**。装机要几十分钟，QGA 在装完前一直不答 |
| finish | `--stage finish --apply` | `completed` | QGA 不通、凭据文件权限不对 | 按停止/定义/seed 引用实际状态续作，不重装 |
| generalize | `template.py generalize` | `generalized_source_stopped` | `Guest agent unavailable` | 确认 `domstate` 是 running 且 QGA 已应答再重试；不要手删 marker |
| capture | `template.py capture` | `published` + sha256 | `requires a shut-off source` | 先确认构建机已关机；不要对活动磁盘操作 |
| create | `instance.py create --apply` | `running` + journal | `blocked` | **实例已存在**，按 journal 续作，见下节 |

## 状态语义

`guest_status.py` 与各脚本用同一套词，含义不要混：

| 状态 | 含义 | 该做什么 |
|---|---|---|
| `ready` | guest 已就绪，字段齐全 | 进入下一步 |
| `not_ready` | 域在运行，但 guest agent 还没应答 | **继续等**。装机中就是这个样子，不要重装 |
| `not_running` | 域已定义但没运行 | 先 start，再等 guest agent |
| `unknown_instance` | 域没定义 | 核对 profile 的 name/UUID |
| `blocked` | 完整性或前置条件不满足 | 修那个条件，不要盲目重试 |

`not_ready` 与 `blocked` 的区别就是「还在等」和「等也没用」。把前者当成后者去重装，是最常见的浪费。

## blocked 不等于失败

`instance.py create` 被阻塞时，磁盘、网络、TPM 和已经完成的阶段都还在，journal 记录了停在哪一步。输出里带 `journal`、`stage`、`domstate` 和可直接执行的 `resume` 命令：

```json
{
  "status": "blocked",
  "reason": "guest readiness timed out; preserve this instance and resume, never reinstall automatically",
  "journal": "/home/vm_run/operations/create-<uuid>.json",
  "stage": "booting",
  "target": "winvm02",
  "domstate": "running",
  "resume": "sudo python3 scripts/instance.py create --profile PROFILE.json --apply --resume /home/vm_run/operations/create-<uuid>.json",
  "note": "the instance is preserved; resume from the journal instead of creating a second instance"
}
```

`domstate: running` 说明实例已经活着，只是某个阶段没走完。**按 `resume` 续作，不要再跑一次 create。** journal 位于 `host_root/operations/`，该目录 root 0700，普通用户读不到，需要 `sudo -n find <host_root>/operations -maxdepth 1 -name 'create-*.json'` 列出。

## 容量峰值与执行顺序

`.storage/<name>.store` 是 `posix_fallocate` **全额预分配**：storage 阶段一执行，宿主根盘立刻少掉整个 `storage_gib`，不管里面写了多少数据。而 qcow2 和发布出来的镜像是稀疏的（Windows 装好只有二三十 GiB）。所以：

- 峰值 = **预分配额 + 实体数据量**，不是 2 倍 `storage_gib`。
- 顺序决定峰值：**capture → 立刻 destroy builder → 再 create 实例**。不 destroy 就直接开实例，峰值要多背一个 builder 的 store。
- `prepare_host --stage check` 的 `capacity_plan` 会报告 `store_allocation_gib`、`projected_root_free_gib`、每个已有实例的 `root_free_threshold_gib` 和 `instances_at_risk`。

`instances_at_risk` 非空时 check 会以 `NEEDS_USER_INPUT` 阻断，含义是：**这次分配会把同宿主其它实例压到它们的磁盘守卫阈值以下，它们的容量守卫会暂停它们**。阈值取自各实例已安装的 `vmctl.py`（`START_ROOT_FREE_MIN`），不是 profile 里的旧值。先腾空间，或与 owner 确认后用 `configure` 下调对方的 `root_free_gib`，再继续。不要自动暂停别人的 VM。

`check` 的单台门槛 `free >= storage_gib + root_free_gib` 只覆盖本机，跨实例耦合由 `capacity_plan` 单独报告。

## 参考：一次完整跑通的实测时间线

以下是 2026-10-26 在 Ubuntu 24.04 + 单卡 RTX PRO 6000 宿主上，从建 builder 到开出一台实例的实测节奏。用于判断「卡住了」还是「还在跑」：

| 时刻 | 事件 |
|---|---|
| 15:58 | 预分配 builder store 200 GiB，根盘空闲 350 → 150 GiB；**同宿主另一台实例因磁盘守卫阈值 250 GiB 被自动暂停** |
| 16:00 | builder 启动，开始装机 |
| 16:00–16:29 | 每分钟轮询一次，连续 30 次「QGA 未应答」——**正常**，Windows 还没装完 |
| 16:18–16:52 | 构建机磁盘从 2.0 GiB 增长到 24 GiB |
| 16:33 | 装机中途重启一次（guest 累计 CPU 时间归零后重新计数） |
| 16:30 | 首次出现 `provision_success=true` |
| 16:42–16:48 | 设备加密补救：`manage-bde -off` 后约 4 分钟解密完成，删除 OOBE 占位账户 |
| 16:52 | Sysprep 完成，构建机自行关机 |
| 16:56 | `capture` 发布镜像：`disk.qcow2` 约 19 GiB，权限 0444，manifest 记录 sha256 |
| 16:58 | `destroy` 构建机，根盘空闲回到 331 GiB |
| 17:05 | `create` 实例：根盘回到 131 GiB；create 返回 `blocked`（QGA 未应答），实例其实已 running |
| 17:15 | 按 journal `--resume`，实例完成 |

三处容易误判的地方：装机期间 QGA 长期不通是正常的；装机过程会重启，`domstate=running` 不能证明「还在装」；`create` 报 blocked 时实例已经活着。

## 验收清单

- 镜像：`manifest.json` 的 `sha256` 与 `disk.qcow2` 实测一致；`generalization.image_state` 是 `IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE`；`c_partition.unique_guid` 已记录。
- 实例：`instance.json` 的 `winrm_thumbprint` 已固定；`registry/<name>.json` 与 profile 的 name/UUID/root/bridge/subnet/端口一致。
- 通道：WinRM 用选定管理员上传并回读哈希一致；RDP 建立**真实桌面会话**（guest 内 `query session` 显示 Active），端口可连不算通过。
- 隔离：guest 主动出站被宿主 DROP 且日志有记录。
- 多实例：从同一模板开出两台，比较 SID、主机名、MAC、TPM、证书和 C 分区 GUID，确认互不相同且都不同于模板。

同一模板连续开出两台并完成上述比较，才算「多实例已现场通过」。历史 winvm01 的验收不能替代。
