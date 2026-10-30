export function validPort(value: number): boolean {
    return Number.isInteger(value) && value >= 1 && value <= 65535;
}
export function validResource(value: number): boolean {
    return Number.isInteger(value) && value >= 1;
}
export function resultTab(action: string): string {
    if (action === 'services' || action.includes('service') || action === 'publish')
        return 'services';
    if (action === 'logs')
        return 'logs';
    if (action === 'list_files')
        return 'files';
    return 'overview';
}
export function terminalJob(status: string): boolean {
    return status === 'succeeded' || status === 'failed' || status === 'interrupted';
}
export function completedLocalAction(action: string, result: unknown): boolean {
    if (action === 'create')
        return Boolean(result && typeof result === 'object' && (result as Record<string, unknown>).status === 'ready');
    if (action !== 'build_image')
        return true;
    if (!result || typeof result !== 'object')
        return false;
    const status = String((result as Record<string, unknown>).status ?? '');
    return status === 'imported' || status === 'ready';
}
export function isoEditions(result: unknown): Array<Record<string, unknown>> {
    if (!result || typeof result !== 'object')
        return [];
    const payload = result as Record<string, unknown>;
    if (payload.status !== 'ok' || typeof payload.path !== 'string' || !Array.isArray(payload.images))
        return [];
    return payload.images.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object');
}
export function sealedImage(result: unknown): Record<string, unknown> | undefined {
    if (!result || typeof result !== 'object')
        return undefined;
    const payload = result as Record<string, unknown>;
    const status = String(payload.status ?? '');
    return (status === 'imported' || status === 'ready') && payload.image && typeof payload.image === 'object'
        ? payload.image as Record<string, unknown>
        : undefined;
}
export function mergeServiceResult(previous: unknown, result: unknown, action: string): Record<string, unknown> {
    const oldValue = previous && typeof previous === 'object' ? previous as Record<string, unknown> : {};
    const nextValue = result && typeof result === 'object' ? result as Record<string, unknown> : {};
    const oldServices = Array.isArray(oldValue.services) ? oldValue.services : [];
    if (action === 'publish' && nextValue.service && ['ready', 'configured', 'configured_with_tunnel_failed'].includes(String(nextValue.status))) {
        const added = nextValue.service as Record<string, unknown>;
        return { ...nextValue, services: [...oldServices.filter((s) => (s as Record<string, unknown>).id !== added.id), added] };
    }
    if (action === 'remove_service') {
        if (!['removed', 'absent'].includes(String(nextValue.status)) && nextValue.host_closed !== true)
            return { ...nextValue, services: oldServices };
        const removed = String(nextValue.service_id ?? (nextValue.service as Record<string, unknown> | undefined)?.id ?? '');
        return { ...nextValue, services: oldServices.filter((service) => service && typeof service === 'object' && String((service as Record<string, unknown>).id ?? '') !== removed) };
    }
    return { ...nextValue, services: Array.isArray(nextValue.services) ? nextValue.services : oldServices };
}
