import { invoke } from '@tauri-apps/api/core';
import './styles.css';
import { completedLocalAction, isoEditions, mergeServiceResult, resultTab, sealedImage, terminalJob, validPort, validResource } from './model';
type Profile = {
    id: string;
    name: string;
    host: string;
    provider?: string;
    vcpus?: number;
    memoryGiB?: number;
    guestIp?: string;
    adminUser?: string;
};
type Job = {
    id: string;
    profileId?: string;
    action: string;
    status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted';
    startedAt: string;
    completedAt?: string;
    result?: unknown;
    error?: string;
};
type Image = {
    id: string;
    name: string;
    path: string;
    sha256: string;
    virtualBytes?: number;
};
type Backend = {
    app_info: () => Promise<{
        version: string;
        platform: string;
        sshAvailable: boolean;
    }>;
    list_profiles: () => Promise<Profile[]>;
    import_profile: (args: {
        document: string;
    }) => Promise<Profile>;
    delete_profile: (args: {
        profileId: string;
    }) => Promise<void>;
    start_job: (args: {
        profileId: string;
        action: string;
        parameters: Record<string, unknown>;
    }) => Promise<Job>;
    list_jobs: () => Promise<Job[]>;
    open_rdp: (args: {
        profileId: string;
    }) => Promise<unknown>;
    local_environment: () => Promise<Record<string, unknown>>;
    local_action: (args: {
        action: string;
        parameters: Record<string, unknown>;
    }) => Promise<Job>;
};
const realBackend: Backend = {
    app_info: () => invoke('app_info'), list_profiles: () => invoke('list_profiles'), import_profile: (args) => invoke('import_profile', args),
    delete_profile: (args) => invoke('delete_profile', args), start_job: (args) => invoke('start_job', args), list_jobs: () => invoke('list_jobs'), open_rdp: (args) => invoke('open_rdp', args),
    local_environment: () => invoke('local_environment'), local_action: (args) => invoke('local_action', args),
};
const query = new URLSearchParams(location.search);
const preview = query.get('preview') === '1';
const previewMock = preview && query.get('mock') === '1';
let mockJobs: Job[] = [];
let mockPolls = 0;
const mockIsoPaths = new Map<string, string>();
let mockProfiles: Profile[] = [{ id: 'preview-vm', name: 'preview-vm', host: '示例配置（未连接）', provider: 'hyperv-local', vcpus: 8, memoryGiB: 16, guestIp: '172.30.61.2', adminUser: '工程师' }];
const mockBackend: Backend = {
    app_info: async () => ({ version: '0.1.0', platform: '预览 mock', sshAvailable: false }),
    list_profiles: async () => mockProfiles,
    import_profile: async () => (await mockBackend.list_profiles())[0],
    delete_profile: async () => undefined,
    start_job: async ({ profileId, action }) => { const job: Job = { id: `preview-${Date.now()}`, profileId, action, status: 'queued', startedAt: new Date().toISOString() }; mockJobs = [job, ...mockJobs]; mockPolls = 0; return job; },
    list_jobs: async () => { mockPolls += 1; mockJobs = mockJobs.map((job) => mockPolls > 1 ? { ...job, status: 'succeeded', completedAt: new Date().toISOString(), result: job.action === 'inspect_iso' ? { status: 'ok', path: mockIsoPaths.get(job.id) ?? '', images: [{ index: 1, name: 'Windows 11 Pro', edition: 'Professional', architecture: 'x64', languages: ['en-US'] }, { index: 2, name: 'Windows 11 Enterprise', edition: 'Enterprise', architecture: 'x64', languages: ['en-US'] }] } : job.action === 'build_image' ? { status: 'imported', image: { id: 'preview-image', name: '预览 Windows 模板', path: 'C:\\preview\\base.vhdx', sha256: 'preview-sha256', virtualBytes: 64 * 1024 ** 3 } } : job.action === 'services' ? { status: 'ok', services: [] } : job.action === 'list_images' ? { images: [{ id: 'preview-image', name: '示例 Windows 镜像', path: 'C:\\preview\\base.vhdx', sha256: 'preview-sha256', virtualBytes: 64 * 1024 ** 3 }] } : job.action === 'publish' ? { status: 'ready', service: { id: 'preview-service', guest_port: 8001, publish_port: 18001 }, endpoint: 'http://127.0.0.1:18001' } : job.action === 'create' ? { status: 'provisioning', profile: { uuid: 'preview-created', name: 'preview-created', provider: 'hyperv-local', resources: { vcpus: 4, memory_gib: 8 }, windows: { admin_user: 'vmadmin' }, network: {} } } : { status: 'preview', action: job.action } } : job); return mockJobs; },
    open_rdp: async () => ({ status: 'preview' }),
    local_environment: async () => ({ edition: '预览 Windows Pro', supported: true, hyperVInstalled: true, isAdministrator: true, hypervisorPresent: true, availableMemoryBytes: 32 * 1024 ** 3, logicalCpus: 8, ready: true, reason: null }),
    local_action: async ({ action, parameters }) => { if (action === 'create')
        mockProfiles = [...mockProfiles, { id: 'preview-created', name: 'preview-created', provider: 'hyperv-local', host: '本机（预览）', vcpus: 4, memoryGiB: 8 }]; const job: Job = { id: `preview-local-${Date.now()}`, profileId: 'local-host', action, status: 'queued', startedAt: new Date().toISOString() }; if (action === 'inspect_iso')
        mockIsoPaths.set(job.id, String(parameters.iso_path)); mockJobs = [job, ...mockJobs]; mockPolls = 0; return job; },
};
const backend: Backend = previewMock ? mockBackend : realBackend;
const state = { builderOpen: undefined as boolean | undefined, profiles: [] as Profile[], images: [] as Image[], jobs: [] as Job[], selected: '', nav: 'vms', mode: 'local' as 'local' | 'remote', detailTab: 'overview', info: null as {
        version: string;
        platform: string;
        sshAvailable: boolean;
    } | null, localEnvironment: null as Record<string, unknown> | null, isoDraft: { isoPath: '', name: 'windows-base', vcpus: '2', memory: '4', disk: '64', imageIndex: '', inspectedPath: '' }, message: '', error: '', results: {} as Record<string, Record<string, unknown>> };
const root = document.querySelector<HTMLDivElement>('#app')!;
function node(tag: string, className?: string, text?: string): HTMLElement { const element = document.createElement(tag); if (className)
    element.className = className; if (text !== undefined)
    element.textContent = text; return element; }
function selectedProfile(): Profile | undefined { return state.profiles.find((profile) => profile.id === state.selected); }
function isLocalProfile(profile: Profile): boolean { return profile.provider === 'hyperv-local' || profile.provider === 'kvm-local'; }
function resultKey(profileId = state.selected, tab = state.detailTab): string { return `${profileId}:${tab}`; }
function currentResult(): Record<string, unknown> | undefined { return state.results[resultKey()]; }
function button(text: string, className: string, action: () => void, disabled = false): HTMLButtonElement { const element = node('button', `button ${className}`, text) as HTMLButtonElement; element.type = 'button'; element.disabled = disabled; element.onclick = action; return element; }
function render(): void {
    root.replaceChildren();
    const shell = node('div', 'shell');
    const nav = node('aside', 'nav');
    const brand = node('div', 'brand');
    brand.append(node('span', undefined, 'VirtOps'), node('span', 'dot', '●'));
    nav.append(brand);
    for (const [id, label] of [['vms', '虚拟机'], ['jobs', '任务日志'], ['settings', '设置']])
        nav.append(button(label, `nav-item ${state.nav === id ? 'active' : ''}`, () => { state.nav = id; render(); }));
    nav.append(node('div', 'nav-foot', preview ? '预览模式 · 本地 mock' : (state.info?.platform ?? '检查中')));
    shell.append(nav);
    const main = node('main', 'main');
    const topbar = node('header', 'topbar');
    const heading = node('div');
    heading.append(node('div', 'eyebrow', '本机与远端虚拟机管理'), node('h1', undefined, state.nav === 'vms' ? '虚拟机' : state.nav === 'jobs' ? '任务日志' : '设置'));
    const modes = node('div', 'mode-switch');
    modes.append(button('本机工作站', state.mode === 'local' ? 'active' : '', () => { state.mode = 'local'; state.selected = ''; state.profiles = []; void refreshLocalEnvironment(); }), button('连接服务器', state.mode === 'remote' ? 'active' : '', () => { state.mode = 'remote'; void refreshRemoteProfiles(); }));
    topbar.append(heading, modes, node('div', 'status-pill', state.mode === 'local' ? (state.localEnvironment?.ready ? '本机可用' : '检查本机') : (state.info?.sshAvailable ? 'SSH 可用' : '未连接')));
    main.append(topbar);
    if (state.message)
        main.append(node('div', 'notice success', state.message));
    if (state.error)
        main.append(node('div', 'notice error', state.error));
    if (state.nav === 'vms')
        renderVms(main);
    else if (state.nav === 'jobs')
        renderJobs(main);
    else
        renderSettings(main);
    shell.append(main);
    root.append(shell);
}
function renderVms(main: HTMLElement): void {
    const toolbar = node('div', 'toolbar');
    toolbar.append(node('div', 'section-title', state.mode === 'local' ? '本机实例' : '远端实例'), state.mode === 'local' ? button('Windows ISO 构建', 'primary', () => scrollToBuilder()) : button('导入实例配置', 'primary', importProfile));
    main.append(toolbar);
    if (state.mode === 'local')
        renderLocalEnvironment(main);
    if (state.mode === 'local')
        renderIsoBuilder(main);
    if (!state.profiles.length) {
        const empty = node('section', 'empty');
        empty.append(node('div', 'empty-icon', '◇'), node('h2', undefined, state.mode === 'local' ? '还没有本机 VM' : '还没有实例配置'), node('p', undefined, state.mode === 'local' ? '先用 Windows ISO 构建本地模板，再创建普通实例。' : '导入由 VirtOps 生成的实例 profile 后，在这里检查状态、调整资源和管理服务端口。'), button(state.mode === 'local' ? 'Windows ISO 构建' : '导入实例配置', 'primary', state.mode === 'local' ? () => scrollToBuilder() : importProfile));
        main.append(empty);
        return;
    }
    const layout = node('div', 'layout');
    const list = node('section', 'panel vm-list');
    for (const profile of state.profiles) {
        const row = button('', `vm-row ${profile.id === state.selected ? 'selected' : ''}`, () => { state.selected = profile.id; state.detailTab = 'overview'; render(); });
        row.append(node('span', 'vm-name', profile.name), node('span', 'vm-host', `${profile.provider ?? 'remote'} · ${profile.host}`));
        list.append(row);
    }
    layout.append(list);
    const detail = node('section', 'panel detail');
    const profile = selectedProfile();
    if (profile)
        renderDetail(detail, profile);
    layout.append(detail);
    main.append(layout);
}
function renderLocalEnvironment(main: HTMLElement): void {
    const environment = state.localEnvironment;
    const card = node('section', 'panel environment');
    card.append(node('h2', undefined, '本机能力'));
    if (!environment)
        card.append(node('p', 'muted', '正在检查 Windows 版本、Hyper-V 和管理员权限…'));
    else {
        const availableGiB = Number(environment.availableMemoryBytes ?? 0) / (1024 ** 3);
        const linux = String(state.info?.platform ?? '').toLowerCase().includes('linux');
        if (linux)
            card.append(kv('系统', 'Linux'), kv('KVM', environment.ready ? '可用' : '不可用'), kv('可用内存', `${availableGiB.toFixed(1)} GiB`));
        else
            card.append(kv('Windows 版本', String(environment.edition ?? '未知')), kv('Hyper-V', environment.hyperVInstalled ? '已安装' : '未安装'), kv('管理员权限', environment.isAdministrator ? '已获得' : '未获得'), kv('可用内存', `${availableGiB.toFixed(1)} GiB`));
        if (!environment.ready)
            card.append(node('div', 'notice error', `本机暂不支持创建 VM：${String(environment.reason ?? '环境检查未通过')}。`));
        const imageSelect = document.createElement('select');
        imageSelect.id = 'local-image-select';
        imageSelect.className = 'image-select';
        imageSelect.append(node('option', undefined, state.images.length ? '选择已导入镜像' : '请先刷新镜像'));
        state.images.forEach((image) => { const option = document.createElement('option'); option.value = image.id; option.textContent = `${image.name} · ${image.sha256.slice(0, 12)}`; imageSelect.append(option); });
        card.append(imageSelect, button('刷新镜像', 'secondary', () => runLocalAction('list_images', {})), button('创建本机 VM', environment.ready && state.images.length > 0 ? 'primary' : 'secondary', renderCreateInstance, !environment.ready || state.images.length === 0));
    }
    main.append(card);
}
function renderIsoBuilder(main: HTMLElement): void {
    const panel = document.createElement('details');
    panel.className = 'panel iso-builder';
    panel.open = state.builderOpen ?? (state.images.length === 0 && state.profiles.length === 0);
    panel.addEventListener('toggle', () => { state.builderOpen = panel.open; });
    panel.id = 'iso-builder';
    const linux = String(state.info?.platform ?? '').toLowerCase().includes('linux');
    if (linux) {
        panel.append(node('summary', undefined, 'Linux 本机实例'), node('p', 'muted', 'Linux 本机使用已有 KVM profile；Windows ISO 自动构建模板暂未启用。'), button('导入已有 KVM profile', 'primary', importProfile));
        main.append(panel);
        return;
    }
    panel.append(node('summary', undefined, '从 Windows ISO 构建本地模板'));
    panel.append(node('p', 'muted', '需要用户提供 ISO 路径。VirtOps 会读取镜像索引，选择专业版或企业版后创建专用构建 VM。'));
    const form = node('form', 'resource-form') as HTMLFormElement;
    form.append(field('Windows ISO 绝对路径', 'iso_path', state.isoDraft.isoPath), field('模板名称', 'image_name', state.isoDraft.name));
    form.append(field('构建 vCPU', 'build_vcpus', state.isoDraft.vcpus), field('构建内存 GiB', 'build_memory_gib', state.isoDraft.memory), field('构建磁盘 GiB', 'build_disk_gib', state.isoDraft.disk));
    form.querySelectorAll<HTMLInputElement>('input').forEach((input) => input.addEventListener('input', () => { state.isoDraft.isoPath = String((form.elements.namedItem('iso_path') as HTMLInputElement).value); state.isoDraft.name = String((form.elements.namedItem('image_name') as HTMLInputElement).value); state.isoDraft.vcpus = String((form.elements.namedItem('build_vcpus') as HTMLInputElement).value); state.isoDraft.memory = String((form.elements.namedItem('build_memory_gib') as HTMLInputElement).value); state.isoDraft.disk = String((form.elements.namedItem('build_disk_gib') as HTMLInputElement).value); }));
    const inspect = button('读取 ISO 索引', 'secondary', () => { const data = new FormData(form); const path = String(data.get('iso_path') ?? '').trim(); if (!path) {
        state.error = '请输入 ISO 绝对路径';
        render();
        return;
    } runLocalAction('inspect_iso', { iso_path: path }); }, !state.localEnvironment?.ready);
    form.append(inspect);
    const editions = Array.isArray(state.localEnvironment?.isoImages) ? state.localEnvironment?.isoImages as Array<Record<string, unknown>> : [];
    if (editions.length) {
        const select = document.createElement('select');
        select.name = 'image_index';
        select.className = 'image-select';
        editions.forEach((image) => { const option = document.createElement('option'); option.value = String(image.index); option.textContent = `${String(image.index)} · ${String(image.name ?? image.edition ?? 'Windows')}`; select.append(option); });
        select.value = state.isoDraft.imageIndex || String(editions[0].index);
        state.isoDraft.imageIndex = select.value;
        select.addEventListener('change', () => { state.isoDraft.imageIndex = select.value; });
        const canBuild = Boolean(state.localEnvironment?.ready && state.isoDraft.inspectedPath && state.isoDraft.inspectedPath === state.isoDraft.isoPath && state.isoDraft.imageIndex);
        form.append(select, button('开始构建模板', 'primary', () => { const data = new FormData(form); const path = String(data.get('iso_path') ?? '').trim(); const vcpus = Number(data.get('build_vcpus')); const memory = Number(data.get('build_memory_gib')); const disk = Number(data.get('build_disk_gib')); if (path !== state.isoDraft.inspectedPath) {
            state.error = 'ISO 路径已变更，请重新读取索引';
            render();
            return;
        } if (!validResource(vcpus) || !validResource(memory) || disk < 64) {
            state.error = '构建资源无效，磁盘至少 64 GiB';
            render();
            return;
        } runLocalAction('build_image', { iso_path: path, image_index: Number(data.get('image_index')), name: data.get('image_name'), vcpus, memory_gib: memory, disk_gib: disk }); }, !canBuild));
    }
    panel.append(form);
    if (state.images.length)
        panel.append(button('创建普通本机实例', 'primary', renderCreateInstance, !state.localEnvironment?.ready));
    main.append(panel);
}
function scrollToBuilder(): void { const panel = document.querySelector<HTMLDetailsElement>('#iso-builder'); if (panel) {
    state.builderOpen = true;
    panel.open = true;
    panel.scrollIntoView({ behavior: 'smooth' });
} }
function renderCreateInstance(): void {
    if (!state.localEnvironment?.ready) {
        state.error = `本机环境不可用：${String(state.localEnvironment?.reason ?? '请先完成环境检查')}`;
        render();
        return;
    }
    const selectedImageId = document.querySelector<HTMLSelectElement>('#local-image-select')?.value;
    const image = state.images.find((candidate) => candidate.id === selectedImageId) ?? state.images[0];
    if (!image) {
        state.error = '请先刷新并选择已封存镜像';
        render();
        return;
    }
    const dialog = document.createElement('dialog');
    dialog.className = 'create-dialog';
    dialog.append(node('h2', undefined, '创建普通本机实例'));
    const form = node('form', 'resource-form') as HTMLFormElement;
    const minimumDisk = Math.max(64, Math.ceil(Number(image.virtualBytes ?? 0) / (1024 ** 3)));
    form.append(field('实例名称', 'name', 'windows-dev'), field('管理员用户名', 'admin_user', 'vmadmin'), field('vCPU', 'vcpus', '4'), field('内存 GiB', 'memory_gib', '8'), field('磁盘 GiB', 'disk_gib', String(minimumDisk)), field('密码（提交后清空，凭据由系统加密保存）', 'password', ''));
    const passwordInput = form.querySelector<HTMLInputElement>('input[name="password"]');
    if (passwordInput)
        passwordInput.type = 'password';
    form.append(button('创建实例', 'primary', () => { const data = new FormData(form); const password = String(data.get('password') ?? ''); const vcpus = Number(data.get('vcpus')); const memory = Number(data.get('memory_gib')); const disk = Number(data.get('disk_gib')); if (!password) {
        state.error = '请输入管理员密码';
        return;
    } if (!validResource(vcpus) || !validResource(memory) || !validResource(disk) || disk < minimumDisk) {
        state.error = `资源无效，磁盘至少 ${minimumDisk} GiB`;
        return;
    } const payload = { name: data.get('name'), image_id: image.id, vcpus, memory_gib: memory, disk_gib: disk, admin_user: data.get('admin_user'), password }; if (passwordInput)
        passwordInput.value = ''; dialog.close(); void runLocalAction('create', payload); }));
    form.append(button('取消', 'secondary', () => dialog.close()));
    dialog.addEventListener('cancel', () => dialog.close(), { once: true });
    dialog.append(form);
    root.append(dialog);
    dialog.showModal();
}
async function refreshLocalEnvironment(): Promise<void> { await run(async () => { state.localEnvironment = await backend.local_environment(); state.profiles = (await backend.list_profiles()).filter(isLocalProfile); state.selected = state.profiles[0]?.id ?? ''; }); }
async function refreshRemoteProfiles(): Promise<void> { await run(async () => { state.profiles = (await backend.list_profiles()).filter((profile) => !isLocalProfile(profile)); state.selected = state.profiles[0]?.id ?? ''; }); }
async function importImage(): Promise<void> { const path = prompt('输入本机 VHDX/VHD 的绝对路径'); if (!path || !/^[A-Za-z]:[\\/].+\.(vhdx|vhd)$/i.test(path)) {
    state.error = '需要输入 Windows 本机的绝对 VHDX/VHD 路径';
    render();
    return;
} const suggested = path.split(/[\\/]/).pop()?.replace(/\.(vhdx|vhd)$/i, '') ?? 'image'; const name = prompt('镜像显示名称', suggested); if (!name)
    return; await runLocalAction('import_image', { path, name }); }
async function runLocalAction(action: string, parameters: Record<string, unknown>): Promise<void> { await run(async () => { const created = await backend.local_action({ action, parameters }); state.jobs = [created, ...state.jobs.filter((job) => job.id !== created.id)]; if (action === 'create' || action === 'build_image') {
    state.message = action === 'build_image' ? '模板正在构建，请在任务日志查看进度' : '本机 VM 正在首次配置，请在任务日志查看进度';
    void trackJob(created.id, action);
    return;
} const result = await waitForJob(created.id); if (result.result && state.localEnvironment) {
    const payload = result.result as Record<string, unknown>;
    if (action === 'inspect_iso') {
        const editions = isoEditions(payload);
        state.localEnvironment = { ...state.localEnvironment, isoImages: editions };
        state.isoDraft.inspectedPath = String(parameters.iso_path ?? '');
        state.isoDraft.imageIndex = '';
    }
    if (action === 'list_images' && Array.isArray(payload.images))
        state.images = payload.images as Image[];
} if (result.status === 'succeeded')
    state.message = `${action} 已完成`; if (result.error)
    throw new Error(result.error); }); }
async function trackJob(id: string, action: string): Promise<void> { try {
    const result = await waitForJob(id, 7200);
    if (action === 'build_image') {
        const image = sealedImage(result.result);
        if (image)
            state.images = [image as Image, ...state.images.filter((item) => item.id !== image.id)];
    }
    if (action === 'create') {
        state.profiles = (await backend.list_profiles()).filter(isLocalProfile);
        state.selected = state.profiles[0]?.id ?? '';
    }
    if (result.status === 'succeeded' && completedLocalAction(action, result.result))
        state.message = action === 'build_image' ? '本机模板已封存，可创建普通实例' : '本机 VM 首次配置完成';
    else if (result.status === 'succeeded')
        state.message = action === 'create' ? '实例已创建，Windows 正在首次配置；稍后点击检查实例状态完成配置。' : '任务已结束，但模板尚未封存';
    if (result.error)
        state.error = result.error;
    render();
}
catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
    render();
} }
function renderDetail(panel: HTMLElement, profile: Profile): void {
    const header = node('div', 'detail-head');
    const checked = Boolean(state.results[resultKey(profile.id, 'overview')]);
    header.append(node('div', 'vm-title', profile.name), node('span', `badge ${checked ? 'ready' : 'unknown'}`, checked ? '已检查' : '未检查'), button('打开 RDP', 'secondary', () => run(() => backend.open_rdp({ profileId: profile.id }))));
    panel.append(header);
    const tabs = node('div', 'tabs');
    for (const [id, label] of [['overview', '概况'], ['resources', '资源'], ['services', '服务端口'], ['files', '文件'], ['logs', '日志']])
        tabs.append(button(label, state.detailTab === id ? 'active' : '', () => { state.detailTab = id; render(); if (id === 'overview' || id === 'resources')
            runJob('inspect', {}); if (id === 'services')
            runJob('services', {}); if (id === 'logs')
            runJob('logs', { network_only: true }); }));
    panel.append(tabs);
    const body = node('div', 'detail-body');
    if (state.detailTab === 'resources')
        renderResources(body, profile);
    else if (state.detailTab === 'services')
        renderServices(body);
    else if (state.detailTab === 'files')
        renderFiles(body);
    else if (state.detailTab === 'logs')
        renderLogs(body);
    else
        renderOverview(body, profile);
    panel.append(body);
}
function renderOverview(body: HTMLElement, profile: Profile): void { const result = currentResult(); body.append(kv('地址', profile.host), kv('guest IP', profile.guestIp ?? '未读取'), kv('管理员', profile.adminUser ?? '未读取'), button('检查实例状态', 'primary', () => runJob('inspect', {})), button('启动 VM', 'secondary', () => runJob('start', {})), button('停止 VM', 'secondary', () => { if (confirm('确认关机？这会停止真实 VM。'))
    runJob('stop', {}); })); if (isLocalProfile(profile))
    body.append(button('删除本机 VM', 'danger', deleteLocalVm)); if (result)
    renderResult(body, result); }
function renderResources(body: HTMLElement, profile: Profile): void { const form = node('form', 'resource-form') as HTMLFormElement; form.append(field('vCPU', 'vcpus', String(profile.vcpus ?? 4)), field('内存 GiB', 'memory_gib', String(profile.memoryGiB ?? 8)), field('宿主上限 GiB', 'host_limit_gib', '')); const stop = checkbox('先停机', 'stop'); const restart = checkbox('完成后恢复运行', 'restart'); form.append(stop, restart, button('保存资源配置', 'primary', () => { const data = new FormData(form); const vcpus = Number(data.get('vcpus')); const memory = Number(data.get('memory_gib')); if (!validResource(vcpus) || !validResource(memory)) {
    state.error = 'vCPU 和内存必须是正整数';
    render();
    return;
} runJob('configure', { vcpus, memory_gib: memory, host_limit_gib: Number(data.get('host_limit_gib') || 0) || undefined, stop: (stop.querySelector('input') as HTMLInputElement).checked, restart: (restart.querySelector('input') as HTMLInputElement).checked }); })); body.append(form); const result = currentResult(); if (result)
    renderResult(body, result); }
function renderServices(body: HTMLElement): void { const form = node('form', 'resource-form') as HTMLFormElement; form.append(field('VM 内 TCP 端口', 'guest_port', '8001'), field('服务 ID（可选）', 'service_id', '')); form.append(button('发布端口', 'primary', () => { const data = new FormData(form); const port = Number(data.get('guest_port')); if (!validPort(port)) {
    state.error = '端口必须为 1-65535 的整数';
    render();
    return;
} runJob('publish', { guest_port: port, service_id: data.get('service_id') || undefined }); })); body.append(form); const result = currentResult(); const services = Array.isArray(result?.services) ? result.services as Array<Record<string, unknown>> : []; for (const service of services) {
    const row = node('div', 'service-row');
    row.append(node('strong', undefined, String(service.id ?? 'service')), node('span', 'muted', `VM ${service.guest_port ?? '?'} → ${service.publish_port ?? '?'}`), button('撤销', 'danger', () => { if (confirm(`确认撤销 ${String(service.id)}？`))
        runJob('remove_service', { service_id: service.id }); }));
    body.append(row);
} if (result?.endpoint)
    body.append(node('div', 'notice success', `本机访问地址：${String(result.endpoint)}`)); if (!services.length)
    body.append(node('div', 'empty compact', '还没有受控服务端口')); }
function renderFiles(body: HTMLElement): void { const form = node('form', 'resource-form') as HTMLFormElement; form.append(field('Windows 路径', 'path', 'C:\\'), button('读取文件列表', 'primary', () => { const data = new FormData(form); const path = String(data.get('path') ?? '').trim(); if (!path) {
    state.error = '请输入 Windows 路径';
    render();
    return;
} runJob('list_files', { path }); })); body.append(form); const result = currentResult(); const items = Array.isArray(result?.items) ? result.items as unknown[] : []; if (items.length) {
    const list = node('ul', 'file-list');
    items.forEach((item) => list.append(node('li', undefined, typeof item === 'string' ? item : JSON.stringify(item))));
    body.append(list);
}
else
    body.append(node('div', 'empty compact', '输入路径后读取文件')); }
function renderLogs(body: HTMLElement): void { const result = currentResult(); const samples = Array.isArray(result?.network_samples) ? result.network_samples as unknown[] : []; if (!samples.length)
    body.append(node('div', 'empty compact', '没有网络阻断记录'));
else {
    const list = node('ul', 'file-list');
    samples.forEach((sample) => list.append(node('li', undefined, typeof sample === 'string' ? sample : JSON.stringify(sample))));
    body.append(list);
} }
function renderResult(body: HTMLElement, result: Record<string, unknown>): void { const details = node('details', 'result'); details.append(node('summary', undefined, '查看原始结果'), node('pre', undefined, JSON.stringify(result, null, 2))); body.append(details); }
function renderJobs(main: HTMLElement): void { const jobs = node('section', 'panel jobs'); if (!state.jobs.length)
    jobs.append(node('div', 'empty compact', '还没有任务记录')); for (const job of state.jobs) {
    const row = node('div', 'job-row');
    row.append(node('span', `job-status ${job.status}`, job.status), node('strong', undefined, job.action), node('span', 'muted', new Date(job.startedAt).toLocaleString()));
    if (job.error)
        row.append(node('span', 'error-text', job.error));
    if (job.result)
        row.append(button('查看', 'secondary', () => { if (job.profileId)
            state.selected = job.profileId; state.detailTab = resultTab(job.action); state.results[resultKey(job.profileId, state.detailTab)] = job.result as Record<string, unknown>; state.nav = 'vms'; render(); }));
    jobs.append(row);
} main.append(jobs); }
function renderSettings(main: HTMLElement): void { const panel = node('section', 'panel settings'); panel.append(node('h2', undefined, '客户端设置'), node('p', 'muted', '连接配置保存在本机应用数据目录。密码、Token 和私钥内容不会写入 profile。')); const row = node('div', 'setting-row'); row.append(node('span', undefined, '后端模式'), node('strong', undefined, '确定性代码执行 · 无 AI')); panel.append(row, node('p', 'muted', `版本 ${state.info?.version ?? '未知'} · SSH ${state.info?.sshAvailable ? '可用' : '未连接'}`)); if (state.selected)
    panel.append(button('移除本机配置', 'danger', removeProfile)); main.append(panel); }
function field(label: string, name: string, value: string): HTMLElement { const wrapper = node('label', 'field'); wrapper.append(node('span', undefined, label)); const input = document.createElement('input'); input.name = name; input.value = value; input.type = name.includes('port') || name.includes('gib') || name === 'vcpus' ? 'number' : 'text'; wrapper.append(input); return wrapper; }
function checkbox(label: string, name: string): HTMLElement { const wrapper = node('label', 'check'); const input = document.createElement('input'); input.type = 'checkbox'; input.name = name; wrapper.append(input, node('span', undefined, label)); return wrapper; }
function kv(key: string, value: string): HTMLElement { const item = node('div', 'kv'); item.append(node('span', undefined, key), node('strong', undefined, value)); return item; }
async function importProfile(): Promise<void> { const input = document.createElement('input'); input.type = 'file'; input.accept = '.json,application/json'; input.onchange = async () => { const file = input.files?.[0]; if (!file)
    return; await run(async () => { const profile = await backend.import_profile({ document: await file.text() }); state.profiles = [...state.profiles.filter((item) => item.id !== profile.id), profile]; state.selected = profile.id; state.message = '实例配置已导入'; }); }; input.click(); }
async function removeProfile(): Promise<void> { const profile = selectedProfile(); if (!profile || !confirm(`仅移除本机保存的 ${profile.name} 配置，不会删除 VM。`))
    return; await run(async () => { await backend.delete_profile({ profileId: profile.id }); state.profiles = state.profiles.filter((item) => item.id !== profile.id); state.selected = state.profiles[0]?.id ?? ''; state.message = '本机配置已移除'; }); }
async function deleteLocalVm(): Promise<void> { const profile = selectedProfile(); if (!profile || !isLocalProfile(profile))
    return; const confirmName = prompt(`请输入实例名称“${profile.name}”以确认删除`); if (confirmName !== profile.name)
    return; const force = confirm('是否启用强制删除？仅用于 OOBE/故障恢复，可能删除实例全部数据。'); await runJob('delete', { confirm_name: confirmName, force }); }
async function runJob(action: string, parameters: Record<string, unknown>): Promise<void> { const profile = selectedProfile(); if (!profile)
    return; const profileId = profile.id; const tab = state.detailTab; await run(async () => { const created = await backend.start_job({ profileId, action, parameters }); state.jobs = [created, ...state.jobs.filter((job) => job.id !== created.id)]; const result = await waitForJob(created.id); const rawResult = (result.result as Record<string, unknown> | undefined) ?? {}; state.results[resultKey(profileId, tab)] = action === 'publish' || action === 'remove_service' ? mergeServiceResult(state.results[resultKey(profileId, tab)], rawResult, action) : rawResult; if (result.status === 'succeeded') {
    state.message = `${action} 已完成`;
    if (action === 'delete') {
        state.profiles = (await backend.list_profiles()).filter(isLocalProfile);
        state.selected = state.profiles[0]?.id ?? '';
    }
}
else
    throw new Error(result.error || `任务未成功完成：${action}（${result.status}）`); }); }
async function waitForJob(id: string, maxAttempts = 360): Promise<Job> { for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const jobs = await backend.list_jobs();
    state.jobs = jobs;
    if (state.nav === 'jobs')
        render();
    const job = jobs.find((item) => item.id === id);
    if (job && terminalJob(job.status))
        return job;
    await new Promise((resolve) => setTimeout(resolve, 500));
} throw new Error('任务等待超时，请到任务日志查看状态'); }
async function run(fn: () => Promise<unknown>): Promise<void> { state.error = ''; try {
    await fn();
    if (!preview)
        state.jobs = await backend.list_jobs();
}
catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
} render(); }
async function boot(): Promise<void> {
    if (preview) {
        state.info = { version: '0.1.0', platform: '预览', sshAvailable: false };
        if (previewMock) {
            state.profiles = mockProfiles;
            state.selected = 'preview-vm';
            state.message = '预览 mock 数据，仅用于界面交互检查';
            state.localEnvironment = await backend.local_environment();
        }
        render();
        return;
    }
    await run(async () => {
        state.info = await backend.app_info();
        state.localEnvironment = await backend.local_environment();
        state.profiles = await backend.list_profiles();
        state.profiles = state.profiles.filter(isLocalProfile);
        state.selected = state.profiles[0]?.id ?? '';
        state.jobs = await backend.list_jobs();
    });
}
boot();
