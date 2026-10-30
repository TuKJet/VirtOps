# Design

## Source of truth
- Status: Active; refreshed 2026-10-30.
- Surfaces: Windows x64 and Linux x64 desktop clients, sharing one Tauri 2 application.
- Evidence: README.md; skill/windows-vm-sandbox/SKILL.md; existing lifecycle, resource and service commands; user requires no AI, Rust + WebView, and a simple attractive interface.
- No existing frontend, brand assets or reference screenshots were present.

## Brand
- Calm engineering utility: legible, precise, restrained.
- Trust comes from real connection state, visible task results and exact destructive-action scope.
- Avoid chat UI, model configuration, decorative dashboards, fabricated VM data and unexplained technical identifiers.

## Product goals
- Start with the user's own ISO on a Windows Pro/Enterprise workstation; make a local reusable template and independent instances through deterministic code.
- Offer existing Linux/KVM instances locally or over SSH on both client platforms. A server is optional.
- Reuse VirtOps host isolation and transactions; ship reproducible downloadable installers through GitHub Actions.
- No AI backend, browser-accessible shell, or automatic production VM changes during application development.
- Success: authenticated read-only VM inspection and tested command contracts; independent successful Windows/Linux builds.

## Personas and jobs
- Engineers accustomed to desktop software: select a VM, connect to its desktop, change resources, open a service port, inspect files and blocked-connection logs.
- Local users provide an ISO and choose resources/accounts. Server users import a prepared profile with SSH trust; ordinary operation should not require Linux commands.

## Information architecture
- Sidebar: 虚拟机, 任务记录, 设置.
- VM details: 概况, 资源, 服务端口, 文件, 日志.
- Primary actions: 选择 ISO, 构建本地模板, 创建实例, 连接桌面, 开机, 关机, 开放端口.
- Keep ISO setup expanded initially, collapsed once instances/templates exist. Create instances in a focused native dialog with a password field.
- Unsupported providers/actions show their reason. ISO installers and VM disks never ship in GitHub artifacts.

## Design principles
- Prefer familiar forms, tables and explicit buttons.
- Keep selection when refreshing; show operation progress without blocking unrelated navigation.
- Display meaningful errors and safe retry actions. Unknown connectivity is not a running/connected state.

## Visual language
- Warm gray canvas, white surfaces, deep navy sidebar, teal accent; red only for destructive actions/errors.
- System sans-serif (Segoe UI on Windows), 14px body, 12px secondary text, 24px page title.
- 8px spacing grid, 10px rounded surfaces, thin borders, minimal shadows.
- Short opacity transitions; honor reduced motion. Inline simple SVG icons, no remote assets/fonts.

## Components
- Sidebar, page header, VM list, status badges, tab panel, labeled inputs, port table, task drawer, confirmation dialog, empty/error state.
- CSS variables in desktop/src/styles.css own visual tokens. Vanilla TypeScript owns view state.
- Every asynchronous action has loading, success and failure states.

## Accessibility
- Keyboard-operable buttons/tabs/dialogs, visible focus, correct labels and table headings.
- Target WCAG AA contrast, text accompanies color, live regions for operation status.
- Escape closes non-destructive dialogs; focus returns to trigger.

## Responsive behavior
- Desktop minimum target 1000x700; content remains usable at 900px width.
- VM list and detail split view may stack when narrow; avoid horizontal page overflow.

## Interaction states
- Loading: retain previous data with a clear refresh indicator.
- Empty: check the local host and ask for an ISO; remote mode explains profile import.
- Error/offline: keep configuration and last verified result, mark stale state clearly.
- Success: report completed operation and resulting endpoint/state.
- Disabled: explain missing host connection, SSH client or unsupported workflow.

## Content voice
- Chinese, direct and specific; show VM name instead of internal UUID by default.
- Distinguish 移除本机连接 from 删除虚拟机数据.
- Never label a TCP connection as successful application authentication.

## Implementation constraints
- Tauri 2 / Rust; Vite + vanilla TypeScript/CSS, no AI or remote web backend.
- Frontend invokes a fixed command allowlist; no shell plugin granted to web content.
- No credentials, runtime profiles or machine-specific addresses in Git or bundled demo data.
- Rust owns SSH children, task lifecycle, local storage and native desktop integration.
- Windows local hosts use Hyper-V on Pro/Enterprise; Linux local/remote hosts use the existing VirtOps KVM engine. Windows remains the guest OS.
- Validate frontend build, Rust tests, backend contracts, and both actual CI bundle jobs.

## Open questions
- ARM64, additional Linux distributions, installer signing and a public auto-update feed are outside this first x64 build.
- The Windows ISO wizard is a development preview until real Pro/Enterprise installation, specialization, RDP and isolation acceptance. Linux ISO wizard, signing and update feed remain outstanding.
