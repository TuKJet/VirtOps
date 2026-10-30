use base64::{engine::general_purpose::STANDARD as B64, Engine};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
#[cfg(target_os = "linux")]
use std::os::unix::process::CommandExt as UnixCommandExt;
#[cfg(target_os = "windows")]
use std::os::windows::io::AsRawHandle;
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt as WindowsCommandExt;
use std::{
    collections::HashMap,
    fs,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::JoinHandle,
};
use tauri::{AppHandle, Manager, State};
use thiserror::Error;
use uuid::Uuid;

const ACTIONS: &[&str] = &[
    "inspect",
    "status",
    "start",
    "stop",
    "logs",
    "configure",
    "services",
    "publish",
    "remove_service",
    "list_files",
];
const LOCAL_ACTIONS: &[&str] = &[
    "environment",
    "inspect_iso",
    "build_image",
    "list_images",
    "import_image",
    "create",
    "inspect",
    "start",
    "stop",
    "configure",
    "publish",
    "remove_service",
    "list_files",
    "logs",
    "services",
    "delete",
];

#[derive(Debug, Error)]
enum AppError {
    #[error("invalid profile: {0}")]
    Profile(String),
    #[error("profile not found")]
    NotFound,
    #[error("job is not allowed: {0}")]
    Action(String),
    #[error("operation failed: {0}")]
    Operation(String),
    #[error("storage error: {0}")]
    Storage(String),
}
impl serde::Serialize for AppError {
    fn serialize<S>(&self, s: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        s.serialize_str(&self.to_string())
    }
}

#[derive(Clone)]
pub struct AppState {
    pub app: AppHandle,
    pub jobs: Arc<Mutex<HashMap<String, Job>>>,
    pub locks: Arc<Mutex<HashMap<String, Arc<Mutex<()>>>>>,
    forwards: ForwardRegistry,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppInfo {
    version: String,
    platform: String,
    ssh_available: bool,
    providers: Vec<String>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProfileSummary {
    pub id: String,
    pub provider: String,
    pub name: String,
    pub host: String,
    pub vcpus: Option<u64>,
    pub memory_gib: Option<u64>,
    pub guest_ip: Option<String>,
    pub publish_ip: Option<String>,
    pub rdp_port: Option<u64>,
    pub admin_user: Option<String>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Job {
    pub id: String,
    pub profile_id: String,
    pub action: String,
    pub status: String,
    pub started_at: String,
    pub completed_at: Option<String>,
    pub result: Option<Value>,
    pub error: Option<String>,
}
#[derive(Clone)]
struct Forward {
    handle: ForwardHandle,
    local_port: u16,
    signature: Vec<String>,
}
#[cfg(target_os = "windows")]
struct WindowsJob {
    handle: isize,
}
#[cfg(target_os = "windows")]
impl Drop for WindowsJob {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.handle);
        }
    }
}
#[derive(Clone)]
enum ForwardHandle {
    Child {
        child: Arc<Mutex<std::process::Child>>,
        #[cfg(target_os = "windows")]
        job: Arc<WindowsJob>,
    },
    Relay {
        stop: Arc<AtomicBool>,
        thread: Arc<Mutex<Option<JoinHandle<()>>>>,
    },
}

#[derive(Clone, Default)]
struct ForwardRegistry {
    inner: Arc<Mutex<HashMap<String, Forward>>>,
}
impl ForwardRegistry {
    fn new() -> Self {
        Self::default()
    }
    fn shutdown(&self) {
        if let Ok(mut forwards) = self.inner.lock() {
            for forward in forwards.values() {
                stop_handle(&forward.handle);
            }
            forwards.clear();
        }
    }
}
fn stop_handle(handle: &ForwardHandle) {
    match handle {
        ForwardHandle::Child {
            child,
            #[cfg(target_os = "windows")]
            job,
        } => {
            #[cfg(target_os = "windows")]
            let _ = Arc::strong_count(job);
            if let Ok(mut child) = child.lock() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
        ForwardHandle::Relay { stop, thread } => {
            stop.store(true, Ordering::SeqCst);
            if let Ok(mut h) = thread.lock() {
                if let Some(h) = h.take() {
                    let _ = h.join();
                }
            }
        }
    }
}
fn prepare_owned_command(command: &mut Command) {
    #[cfg(target_os = "windows")]
    {
        command.creation_flags(0x0800_0000);
    }
    #[cfg(target_os = "linux")]
    {
        let parent = std::process::id() as i32;
        unsafe {
            command.pre_exec(move || {
                if prctl(1, 15, 0, 0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if getppid() != parent {
                    return Err(std::io::Error::other(
                        "parent exited before SSH child startup",
                    ));
                }
                Ok(())
            });
        }
    }
}
#[cfg(target_os = "linux")]
unsafe extern "C" {
    fn prctl(option: i32, arg2: i32, arg3: usize, arg4: usize, arg5: usize) -> i32;
    fn getppid() -> i32;
}
#[cfg(target_os = "windows")]
unsafe extern "system" {
    fn CreateJobObjectW(a: *const u8, name: *const u16) -> isize;
    fn SetInformationJobObject(job: isize, class: u32, info: *const u8, len: u32) -> i32;
    fn AssignProcessToJobObject(job: isize, process: isize) -> i32;
    fn CloseHandle(handle: isize) -> i32;
}
#[cfg(target_os = "windows")]
#[repr(C)]
struct JobBasicLimitInfo {
    per_process_user_time: i64,
    per_job_user_time: i64,
    limit_flags: u32,
    min_working_set: usize,
    max_working_set: usize,
    active_process_limit: u32,
    affinity: usize,
    priority_class: u32,
    scheduling_class: u32,
}
#[cfg(target_os = "windows")]
#[repr(C)]
struct IoCounters {
    read_ops: u64,
    write_ops: u64,
    other_ops: u64,
    read_bytes: u64,
    write_bytes: u64,
    other_bytes: u64,
}
#[cfg(target_os = "windows")]
#[repr(C)]
struct JobExtendedLimitInfo {
    basic: JobBasicLimitInfo,
    io: IoCounters,
    process_memory_limit: usize,
    job_memory_limit: usize,
    peak_process_memory_used: usize,
    peak_job_memory_used: usize,
}
#[cfg(target_os = "windows")]
fn attach_kill_on_close(child: &mut std::process::Child) -> Result<Arc<WindowsJob>, AppError> {
    let job = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
    if job == 0 {
        return Err(AppError::Operation("CreateJobObject failed".into()));
    }
    let mut info = JobExtendedLimitInfo {
        basic: JobBasicLimitInfo {
            per_process_user_time: 0,
            per_job_user_time: 0,
            limit_flags: 0x0000_2000,
            min_working_set: 0,
            max_working_set: 0,
            active_process_limit: 0,
            affinity: 0,
            priority_class: 0,
            scheduling_class: 0,
        },
        io: IoCounters {
            read_ops: 0,
            write_ops: 0,
            other_ops: 0,
            read_bytes: 0,
            write_bytes: 0,
            other_bytes: 0,
        },
        process_memory_limit: 0,
        job_memory_limit: 0,
        peak_process_memory_used: 0,
        peak_job_memory_used: 0,
    };
    let ok = unsafe {
        SetInformationJobObject(
            job,
            9,
            (&mut info as *mut JobExtendedLimitInfo).cast(),
            std::mem::size_of::<JobExtendedLimitInfo>() as u32,
        )
    } != 0;
    let assigned =
        ok && unsafe { AssignProcessToJobObject(job, child.as_raw_handle() as isize) } != 0;
    if !assigned {
        let _ = child.kill();
        let _ = child.wait();
        unsafe {
            CloseHandle(job);
        }
        return Err(AppError::Operation(
            "AssignProcessToJobObject failed".into(),
        ));
    }
    Ok(Arc::new(WindowsJob { handle: job }))
}

fn app_dir(app: &AppHandle) -> Result<PathBuf, AppError> {
    app.path()
        .app_data_dir()
        .map_err(|e| AppError::Storage(e.to_string()))
}
fn profiles_dir(app: &AppHandle) -> Result<PathBuf, AppError> {
    let p = app_dir(app)?.join("profiles");
    fs::create_dir_all(&p).map_err(|e| AppError::Storage(e.to_string()))?;
    Ok(p)
}
fn jobs_dir(app: &AppHandle) -> Result<PathBuf, AppError> {
    let p = app_dir(app)?.join("jobs");
    fs::create_dir_all(&p).map_err(|e| AppError::Storage(e.to_string()))?;
    Ok(p)
}
fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), AppError> {
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, bytes).map_err(|e| AppError::Storage(e.to_string()))?;
    fs::rename(tmp, path).map_err(|e| AppError::Storage(e.to_string()))
}
fn profile_path(app: &AppHandle, id: &str) -> Result<PathBuf, AppError> {
    if Uuid::parse_str(id).is_err() {
        return Err(AppError::Profile("invalid profile id".into()));
    }
    Ok(profiles_dir(app)?.join(format!("{id}.json")))
}
fn profile_value(app: &AppHandle, id: &str) -> Result<Value, AppError> {
    let p = profile_path(app, id)?;
    let raw = fs::read_to_string(p).map_err(|_| AppError::NotFound)?;
    serde_json::from_str(&raw).map_err(|e| AppError::Profile(e.to_string()))
}

fn reject_secret_keys(v: &Value) -> Result<(), AppError> {
    match v {
        Value::Object(m) => {
            for (k, x) in m {
                if matches!(
                    k.to_ascii_lowercase().as_str(),
                    "password" | "token" | "secret" | "private_key" | "admin_password"
                ) {
                    return Err(AppError::Profile(format!(
                        "inline secret is not allowed: {k}"
                    )));
                }
                reject_secret_keys(x)?
            }
        }
        Value::Array(a) => {
            for x in a {
                reject_secret_keys(x)?
            }
        }
        _ => {}
    }
    Ok(())
}
fn validate_profile(v: &Value) -> Result<ProfileSummary, AppError> {
    reject_secret_keys(v)?;
    let o = v
        .as_object()
        .ok_or_else(|| AppError::Profile("profile must be an object".into()))?;
    let name = o
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Profile("name is required".into()))?;
    if !name
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        || name.len() < 2
        || name.len() > 12
    {
        return Err(AppError::Profile(
            "name must be 2-12 lowercase ASCII characters".into(),
        ));
    }
    let id = o
        .get("uuid")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Profile("uuid is required".into()))?;
    Uuid::parse_str(id).map_err(|_| AppError::Profile("uuid is invalid".into()))?;
    let provider = o.get("provider").and_then(Value::as_str).unwrap_or("ssh");
    if !matches!(provider, "ssh" | "hyperv-local" | "kvm-local") {
        return Err(AppError::Profile("provider is unsupported".into()));
    }
    let root = o.get("root").and_then(Value::as_str).unwrap_or("");
    if provider == "ssh" || provider == "kvm-local" {
        if !root.starts_with('/') || root.contains("..") || root.contains(' ') {
            return Err(AppError::Profile(
                "root must be a safe absolute Linux path".into(),
            ));
        }
    } else if root.is_empty()
        || root.contains("..")
        || root.contains(['\r', '\n'])
        || !(root.starts_with("\\\\")
            || (root.len() >= 3 && root.as_bytes()[1] == b':' && root.as_bytes()[2] == b'\\'))
    {
        return Err(AppError::Profile(
            "root must be a safe absolute Windows path".into(),
        ));
    }
    let (host, admin_user) = if provider == "ssh" {
        let ssh = o
            .get("ssh")
            .and_then(Value::as_object)
            .ok_or_else(|| AppError::Profile("ssh is required".into()))?;
        let host = ssh
            .get("host")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::Profile("ssh.host is required".into()))?;
        let user = ssh
            .get("user")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::Profile("ssh.user is required".into()))?;
        if host.is_empty()
            || host.contains(char::is_whitespace)
            || user.is_empty()
            || user.contains(char::is_whitespace)
        {
            return Err(AppError::Profile("SSH host/user is invalid".into()));
        }
        let port = ssh.get("port").and_then(Value::as_u64).unwrap_or(22);
        if port == 0 || port > 65535 {
            return Err(AppError::Profile("ssh.port is invalid".into()));
        }
        (
            host.to_owned(),
            o.get("windows")
                .and_then(|w| w.get("admin_user"))
                .and_then(Value::as_str)
                .map(str::to_owned),
        )
    } else {
        (
            if provider == "hyperv-local" {
                "本机 Hyper-V"
            } else {
                "本机 KVM"
            }
            .into(),
            o.get("windows")
                .and_then(|x| x.get("admin_user"))
                .and_then(Value::as_str)
                .map(str::to_owned),
        )
    };
    Ok(ProfileSummary {
        id: id.into(),
        name: name.into(),
        provider: provider.into(),
        host,
        vcpus: o
            .get("resources")
            .and_then(|x| x.get("vcpus"))
            .and_then(Value::as_u64),
        memory_gib: o
            .get("resources")
            .and_then(|x| x.get("memory_gib"))
            .and_then(Value::as_u64),
        guest_ip: o
            .get("network")
            .and_then(|x| x.get("guest_ip"))
            .and_then(Value::as_str)
            .map(str::to_owned),
        publish_ip: o
            .get("network")
            .and_then(|x| x.get("publish_ip"))
            .and_then(Value::as_str)
            .map(str::to_owned),
        rdp_port: o
            .get("network")
            .and_then(|x| x.get("rdp_port"))
            .and_then(Value::as_u64),
        admin_user,
    })
}
fn load_jobs(app: &AppHandle) -> HashMap<String, Job> {
    let mut out = HashMap::new();
    if let Ok(d) = jobs_dir(app) {
        if let Ok(es) = fs::read_dir(d) {
            for e in es.flatten() {
                if let Ok(v) =
                    serde_json::from_str::<Job>(&fs::read_to_string(e.path()).unwrap_or_default())
                {
                    let mut job = v;
                    if matches!(job.status.as_str(), "running" | "queued") {
                        job.status = "interrupted".into();
                        job.error = Some("application restarted; remote outcome is unknown; inspect before retry".into());
                        job.completed_at = Some(Utc::now().to_rfc3339());
                        let _ = write_atomic(
                            &e.path(),
                            serde_json::to_vec_pretty(&job).unwrap().as_slice(),
                        );
                    }
                    out.insert(job.id.clone(), job);
                }
            }
        }
    }
    out
}

mod commands {
    use super::*;

    #[tauri::command]
    pub fn app_info() -> AppInfo {
        AppInfo {
            version: env!("CARGO_PKG_VERSION").into(),
            platform: std::env::consts::OS.into(),
            ssh_available: Command::new("ssh").arg("-V").output().is_ok(),
            providers: vec!["ssh".into(), "local".into()],
        }
    }
    #[tauri::command]
    pub fn list_profiles(app: AppHandle) -> Result<Vec<ProfileSummary>, AppError> {
        let mut out = vec![];
        let mut seen = std::collections::HashSet::new();
        for e in fs::read_dir(profiles_dir(&app)?)
            .map_err(|e| AppError::Storage(e.to_string()))?
            .flatten()
        {
            if let Ok(v) =
                serde_json::from_str::<Value>(&fs::read_to_string(e.path()).unwrap_or_default())
            {
                if let Ok(s) = validate_profile(&v) {
                    seen.insert(s.id.clone());
                    out.push(s)
                }
            }
        }
        let instances = app_dir(&app)?.join("hyperv").join("instances");
        if let Ok(entries) = fs::read_dir(instances) {
            for entry in entries.flatten() {
                let dir = entry.path();
                let id = entry.file_name().to_string_lossy().to_string();
                if Uuid::parse_str(&id).is_err() || !dir.is_dir() || seen.contains(&id) {
                    continue;
                }
                let path = dir.join("instance.json");
                let Ok(raw) = fs::read_to_string(&path) else {
                    continue;
                };
                let Ok(v) = serde_json::from_str::<Value>(&raw) else {
                    continue;
                };
                let root = v.get("root").and_then(Value::as_str).unwrap_or("");
                let canonical = |s: &str| {
                    s.replace('/', "\\")
                        .trim_end_matches('\\')
                        .to_ascii_lowercase()
                };
                if v.get("provider").and_then(Value::as_str) != Some("hyperv-local")
                    || v.get("uuid").and_then(Value::as_str) != Some(id.as_str())
                    || canonical(root) != canonical(&dir.to_string_lossy())
                {
                    continue;
                }
                if let Ok(summary) = validate_profile(&v) {
                    let _ = write_atomic(
                        &profile_path(&app, &summary.id)?,
                        serde_json::to_vec_pretty(&v).unwrap().as_slice(),
                    );
                    seen.insert(summary.id.clone());
                    out.push(summary);
                }
            }
        }
        Ok(out)
    }
    #[tauri::command]
    pub fn import_profile(app: AppHandle, document: String) -> Result<ProfileSummary, AppError> {
        let v: Value =
            serde_json::from_str(&document).map_err(|e| AppError::Profile(e.to_string()))?;
        let s = validate_profile(&v)?;
        let p = profile_path(&app, &s.id)?;
        write_atomic(&p, serde_json::to_vec_pretty(&v).unwrap().as_slice())?;
        Ok(s)
    }
    #[tauri::command]
    pub fn delete_profile(app: AppHandle, profile_id: String) -> Result<(), AppError> {
        let p = profile_path(&app, &profile_id)?;
        if p.exists() {
            fs::remove_file(p).map_err(|e| AppError::Storage(e.to_string()))?
        }
        Ok(())
    }
    #[tauri::command]
    pub fn list_jobs(state: State<AppState>) -> Vec<Job> {
        let mut j = state.jobs.lock().unwrap();
        if j.is_empty() {
            *j = load_jobs(&state.app)
        }
        j.values().cloned().collect()
    }

    fn remote_source() -> &'static str {
        include_str!("../../engine/remote.py")
    }
    fn build_remote_source(request: &Value) -> String {
        let encoded = B64.encode(serde_json::to_vec(request).expect("request is serializable"));
        format!(
            "import base64,json\nREQUEST=json.loads(base64.b64decode({encoded:?}))\n{}",
            remote_source()
        )
    }
    fn read_bounded<R: Read>(mut reader: R) -> Vec<u8> {
        const LIMIT: usize = 512 * 1024;
        let mut output = Vec::new();
        let mut buffer = [0_u8; 8192];
        while let Ok(size) = reader.read(&mut buffer) {
            if size == 0 {
                break;
            }
            let remaining = LIMIT.saturating_sub(output.len());
            output.extend_from_slice(&buffer[..size.min(remaining)]);
            if output.len() >= LIMIT {
                break;
            }
        }
        output
    }
    fn ssh_target(v: &Value) -> Result<(Vec<String>, String), AppError> {
        let s = v
            .get("ssh")
            .and_then(Value::as_object)
            .ok_or_else(|| AppError::Profile("ssh is required".into()))?;
        let host = s
            .get("host")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::Profile("ssh.host is required".into()))?;
        let user = s
            .get("user")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::Profile("ssh.user is required".into()))?;
        if host.is_empty()
            || !host
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || ".:-_".contains(c))
            || user.is_empty()
            || !user
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || ".-_".contains(c))
        {
            return Err(AppError::Profile(
                "SSH host/user contains unsafe characters".into(),
            ));
        }
        let port = s.get("port").and_then(Value::as_u64).unwrap_or(22);
        let mut a = vec![
            "-o".into(),
            "BatchMode=yes".into(),
            "-o".into(),
            "StrictHostKeyChecking=yes".into(),
            "-o".into(),
            "ConnectTimeout=10".into(),
            "-p".into(),
            port.to_string(),
        ];
        if let Some(b) = s.get("bind_address").and_then(Value::as_str) {
            if !b
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || ".:[]".contains(c))
            {
                return Err(AppError::Profile(
                    "bind_address contains unsafe characters".into(),
                ));
            }
            a.extend(["-o".into(), format!("BindAddress={b}")])
        }
        if let Some(k) = s.get("host_key_alias").and_then(Value::as_str) {
            if !k
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || ".:_-".contains(c))
            {
                return Err(AppError::Profile(
                    "host_key_alias contains unsafe characters".into(),
                ));
            }
            a.extend(["-o".into(), format!("HostKeyAlias={k}")])
        }
        if let Some(i) = s.get("identity_file").and_then(Value::as_str) {
            if !Path::new(i).is_absolute()
                || i.contains('\r')
                || i.contains('\n')
                || i.contains('"')
            {
                return Err(AppError::Profile("identity_file must be absolute".into()));
            }
            a.extend(["-i".into(), i.into()])
        }
        Ok((a, format!("{user}@{host}")))
    }
    fn profile_ssh(v: &Value) -> Result<Vec<String>, AppError> {
        let (mut args, target) = ssh_target(v)?;
        args.extend([
            target,
            "sudo".into(),
            "-n".into(),
            "python3".into(),
            "-".into(),
        ]);
        Ok(args)
    }
    fn forward_args(
        v: &Value,
        local_port: u16,
        publish_port: u16,
    ) -> Result<Vec<String>, AppError> {
        let (mut args, target) = ssh_target(v)?;
        args.extend([
            "-o".into(),
            "ExitOnForwardFailure=yes".into(),
            "-o".into(),
            "ServerAliveInterval=30".into(),
            "-o".into(),
            "ServerAliveCountMax=2".into(),
            "-N".into(),
            "-L".into(),
            format!("127.0.0.1:{local_port}:127.0.0.1:{publish_port}"),
            target,
        ]);
        Ok(args)
    }
    fn forward_key(profile_id: &str, service_id: &str) -> String {
        format!("{profile_id}:{service_id}")
    }
    fn forward_local_port() -> Result<u16, AppError> {
        let listener =
            TcpListener::bind(("127.0.0.1", 0)).map_err(|e| AppError::Operation(e.to_string()))?;
        Ok(listener
            .local_addr()
            .map_err(|e| AppError::Operation(e.to_string()))?
            .port())
    }
    fn stop_forward(registry: &ForwardRegistry, key: &str) {
        if let Some(forward) = registry.inner.lock().unwrap().remove(key) {
            stop_handle(&forward.handle);
        }
    }
    fn ensure_forward(
        profile: &Value,
        profile_id: &str,
        service: &Value,
        registry: &ForwardRegistry,
    ) -> Result<Value, AppError> {
        let service_id = service
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::Operation("published service has no id".into()))?;
        let publish_port = service
            .get("publish_port")
            .and_then(Value::as_u64)
            .ok_or_else(|| AppError::Operation("published service has no publish port".into()))?;
        if publish_port == 0 || publish_port > 65535 {
            return Err(AppError::Operation(
                "published service port is invalid".into(),
            ));
        }
        let key = forward_key(profile_id, service_id);
        let local_port_hint = registry
            .inner
            .lock()
            .unwrap()
            .get(&key)
            .map(|f| f.local_port);
        let requested_port = local_port_hint.unwrap_or_else(|| forward_local_port().unwrap_or(0));
        if requested_port == 0 {
            return Err(AppError::Operation(
                "cannot allocate local tunnel port".into(),
            ));
        }
        let requested_args = forward_args(profile, requested_port, publish_port as u16)?;
        let existing = {
            let guard = registry.inner.lock().unwrap();
            guard.get(&key).cloned()
        };
        if let Some(existing) = existing {
            if existing.signature != requested_args {
                stop_forward(registry, &key);
            }
            if let ForwardHandle::Child { child, .. } = &existing.handle {
                if child
                    .lock()
                    .unwrap()
                    .try_wait()
                    .map_err(|e| AppError::Operation(e.to_string()))?
                    .is_none()
                {
                    return Ok(
                        json!({"localPort": existing.local_port, "endpoint": format!("127.0.0.1:{}", existing.local_port), "reused": true}),
                    );
                }
            } else {
                return Ok(
                    json!({"localPort": existing.local_port, "endpoint": format!("127.0.0.1:{}", existing.local_port), "reused": true}),
                );
            }
            stop_forward(registry, &key);
        }
        let local_port = requested_port;
        let args = forward_args(profile, local_port, publish_port as u16)?;
        let mut command = Command::new("ssh");
        command
            .args(&args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        prepare_owned_command(&mut command);
        let mut child = command
            .spawn()
            .map_err(|e| AppError::Operation(format!("SSH tunnel start failed: {e}")))?;
        #[cfg(target_os = "windows")]
        let job = attach_kill_on_close(&mut child)?;
        let forward = Forward {
            handle: ForwardHandle::Child {
                child: Arc::new(Mutex::new(child)),
                #[cfg(target_os = "windows")]
                job,
            },
            local_port,
            signature: args.clone(),
        };
        let child_ref = match &forward.handle {
            ForwardHandle::Child { child, .. } => child.clone(),
            _ => unreachable!(),
        };
        let mut ready = false;
        for _ in 0..50 {
            if child_ref
                .lock()
                .unwrap()
                .try_wait()
                .map_err(|e| AppError::Operation(e.to_string()))?
                .is_some()
            {
                break;
            }
            if TcpStream::connect_timeout(
                &SocketAddr::from(([127, 0, 0, 1], local_port)),
                std::time::Duration::from_millis(150),
            )
            .is_ok()
            {
                ready = true;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        if !ready {
            if let Ok(mut child) = child_ref.lock() {
                let _ = child.kill();
                let _ = child.wait();
            }
            return Err(AppError::Operation(
                "SSH tunnel did not become ready within 5 seconds".into(),
            ));
        }
        registry.inner.lock().unwrap().insert(key, forward);
        Ok(
            json!({"localPort": local_port, "endpoint": format!("127.0.0.1:{local_port}"), "reused": false}),
        )
    }
    fn ensure_relay(
        profile_id: &str,
        service: &Value,
        target: &str,
        registry: &ForwardRegistry,
    ) -> Result<Value, AppError> {
        let service_id = service
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::Operation("published service has no id".into()))?;
        let target: SocketAddr = target
            .parse()
            .map_err(|_| AppError::Operation("local target is invalid".into()))?;
        let key = forward_key(profile_id, service_id);
        let existing = { registry.inner.lock().unwrap().get(&key).cloned() };
        if let Some(existing) = existing {
            if existing.signature == vec![target.to_string()] {
                return Ok(
                    json!({"localPort": existing.local_port, "endpoint": format!("127.0.0.1:{}", existing.local_port), "reused": true}),
                );
            }
            stop_forward(registry, &key);
        }
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .map_err(|e| AppError::Operation(format!("relay bind failed: {e}")))?;
        listener
            .set_nonblocking(true)
            .map_err(|e| AppError::Operation(e.to_string()))?;
        let local_port = listener
            .local_addr()
            .map_err(|e| AppError::Operation(e.to_string()))?
            .port();
        let stop = Arc::new(AtomicBool::new(false));
        let stop_thread = stop.clone();
        let thread = std::thread::spawn(move || {
            while !stop_thread.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut incoming, _)) => {
                        let target = target;
                        std::thread::spawn(move || {
                            if let Ok(mut outgoing) = TcpStream::connect_timeout(
                                &target,
                                std::time::Duration::from_secs(5),
                            ) {
                                if let (Ok(mut incoming_read), Ok(mut outgoing_write)) =
                                    (incoming.try_clone(), outgoing.try_clone())
                                {
                                    let t = std::thread::spawn(move || {
                                        let _ =
                                            std::io::copy(&mut incoming_read, &mut outgoing_write);
                                    });
                                    let _ = std::io::copy(&mut outgoing, &mut incoming);
                                    let _ = t.join();
                                }
                            }
                        });
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(std::time::Duration::from_millis(50))
                    }
                    Err(_) => break,
                }
            }
        });
        let forward = Forward {
            handle: ForwardHandle::Relay {
                stop,
                thread: Arc::new(Mutex::new(Some(thread))),
            },
            local_port,
            signature: vec![target.to_string()],
        };
        registry.inner.lock().unwrap().insert(key, forward);
        Ok(
            json!({"localPort": local_port, "endpoint": format!("127.0.0.1:{local_port}"), "reused": false}),
        )
    }
    fn run_remote(profile: Value, action: String, parameters: Value) -> Result<Value, AppError> {
        let request = json!({"profile":profile,"action":action,"parameters":parameters});
        let source = build_remote_source(&request);
        let args = profile_ssh(&request["profile"])?;
        let mut c = Command::new("ssh");
        c.args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        prepare_owned_command(&mut c);
        prepare_owned_command(&mut c);
        let mut child = c.spawn().map_err(|e| AppError::Operation(e.to_string()))?;
        #[cfg(target_os = "windows")]
        let _job = attach_kill_on_close(&mut child)?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| AppError::Operation("SSH stdout unavailable".into()))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| AppError::Operation("SSH stderr unavailable".into()))?;
        let stdout_reader = std::thread::spawn(|| read_bounded(stdout));
        let stderr_reader = std::thread::spawn(|| read_bounded(stderr));
        child
            .stdin
            .take()
            .unwrap()
            .write_all(source.as_bytes())
            .map_err(|e| AppError::Operation(e.to_string()))?;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(180);
        let status = loop {
            if let Some(status) = child
                .try_wait()
                .map_err(|e| AppError::Operation(e.to_string()))?
            {
                break status;
            }
            if std::time::Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout_reader.join();
                let _ = stderr_reader.join();
                return Err(AppError::Operation(
                    "SSH operation timed out; remote outcome is unknown; inspect before retry"
                        .into(),
                ));
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        };
        let stdout = stdout_reader
            .join()
            .map_err(|_| AppError::Operation("SSH stdout reader failed".into()))?;
        let stderr = stderr_reader
            .join()
            .map_err(|_| AppError::Operation("SSH stderr reader failed".into()))?;
        let stderr = String::from_utf8_lossy(&stderr);
        if !status.success() {
            if let Ok(value) = serde_json::from_slice::<Value>(&stdout) {
                if let Some(reason) = value.get("reason").and_then(Value::as_str) {
                    return Err(AppError::Operation(reason.to_owned()));
                }
            }
            return Err(AppError::Operation(
                stderr
                    .chars()
                    .rev()
                    .take(2000)
                    .collect::<String>()
                    .chars()
                    .rev()
                    .collect(),
            ));
        }
        serde_json::from_slice(&stdout)
            .map_err(|e| AppError::Operation(format!("remote returned invalid JSON: {e}")))
    }
    #[cfg(target_os = "windows")]
    fn local_hyperv_source() -> &'static str {
        include_str!("../../engine/local_hyperv.ps1")
    }
    #[cfg(target_os = "windows")]
    fn local_iso_source() -> &'static str {
        include_str!("../../engine/local_iso.ps1")
    }
    #[cfg(target_os = "windows")]
    fn build_local_powershell_source(encoded: &str) -> String {
        format!(
            "$Request=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{}'))|ConvertFrom-Json\n{}\n{}",
            encoded,
            local_iso_source(),
            local_hyperv_source()
        )
    }
    #[cfg(target_os = "windows")]
    fn powershell_encoded_bootstrap() -> String {
        let bootstrap = "[Console]::InputEncoding=[Text.UTF8Encoding]::new($false); [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $script=[Console]::In.ReadToEnd(); & ([ScriptBlock]::Create($script))";
        let mut utf16 = Vec::with_capacity(bootstrap.len() * 2);
        for unit in bootstrap.encode_utf16() {
            utf16.extend_from_slice(&unit.to_le_bytes());
        }
        B64.encode(utf16)
    }
    #[cfg(not(target_os = "windows"))]
    fn local_linux_source() -> &'static str {
        include_str!("../../engine/remote.py")
    }
    fn run_local(
        profile: Option<Value>,
        action: String,
        parameters: Value,
        store_root: &Path,
    ) -> Result<Value, AppError> {
        let request = json!({"action": action, "parameters": parameters, "profile": profile, "storeRoot": store_root});
        let encoded = B64
            .encode(serde_json::to_vec(&request).map_err(|e| AppError::Operation(e.to_string()))?);
        #[cfg(target_os = "windows")]
        let bootstrap = powershell_encoded_bootstrap();
        #[cfg(target_os = "windows")]
        let (program, args, source) = (
            "powershell.exe",
            vec![
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                bootstrap.as_str(),
            ],
            build_local_powershell_source(&encoded),
        );
        #[cfg(not(target_os = "windows"))]
        let (program, args, source) = (
            "sudo",
            vec!["-n", "python3", "-"],
            format!(
                "import base64,json\nREQUEST=json.loads(base64.b64decode({encoded:?}))\n{}",
                local_linux_source()
            ),
        );
        let mut local_command = Command::new(program);
        local_command
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        prepare_owned_command(&mut local_command);
        let mut child = local_command
            .spawn()
            .map_err(|e| AppError::Operation(e.to_string()))?;
        #[cfg(target_os = "windows")]
        let _job = attach_kill_on_close(&mut child)?;
        child
            .stdin
            .take()
            .unwrap()
            .write_all(source.as_bytes())
            .map_err(|e| AppError::Operation(e.to_string()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| AppError::Operation("local stdout unavailable".into()))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| AppError::Operation("local stderr unavailable".into()))?;
        let stdout_reader = std::thread::spawn(|| read_bounded(stdout));
        let stderr_reader = std::thread::spawn(|| read_bounded(stderr));
        let timeout_secs = if request.get("action").and_then(Value::as_str) == Some("build_image") {
            3600
        } else {
            1800
        };
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
        let status = loop {
            if let Some(status) = child
                .try_wait()
                .map_err(|e| AppError::Operation(e.to_string()))?
            {
                break status;
            }
            if std::time::Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout_reader.join();
                let _ = stderr_reader.join();
                return Err(AppError::Operation(
                    "local operation timed out; outcome is unknown; inspect before retry".into(),
                ));
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        };
        let stdout = stdout_reader
            .join()
            .map_err(|_| AppError::Operation("local stdout reader failed".into()))?;
        let stderr = stderr_reader
            .join()
            .map_err(|_| AppError::Operation("local stderr reader failed".into()))?;
        if !status.success() {
            let stdout = String::from_utf8_lossy(&stdout).trim().to_owned();
            let stderr = String::from_utf8_lossy(&stderr).trim().to_owned();
            return Err(AppError::Operation(if stdout.is_empty() {
                stderr
            } else {
                stdout
            }));
        }
        serde_json::from_slice(&stdout)
            .map_err(|e| AppError::Operation(format!("local provider returned invalid JSON: {e}")))
    }
    #[tauri::command]
    pub fn local_environment(app: AppHandle) -> Result<Value, AppError> {
        #[cfg(not(target_os = "windows"))]
        {
            let kvm = Path::new("/dev/kvm").exists();
            let libvirt = Command::new("virsh").arg("--version").output().is_ok();
            let sudo = Command::new("sudo")
                .args(["-n", "true"])
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false);
            return Ok(json!({
                "platform": std::env::consts::OS,
                "provider": "kvm-local",
                "kvmPresent": kvm,
                "libvirtPresent": libvirt,
                "sudoAvailable": sudo,
                "ready": kvm && libvirt && sudo
            }));
        }
        #[cfg(target_os = "windows")]
        run_local(
            None,
            "environment".into(),
            json!({}),
            &app_dir(&app)?.join("hyperv"),
        )
    }
    #[tauri::command]
    pub async fn local_action(
        state: State<'_, AppState>,
        app: AppHandle,
        action: String,
        parameters: Value,
        profile: Option<Value>,
    ) -> Result<Job, AppError> {
        if !LOCAL_ACTIONS.contains(&action.as_str()) {
            return Err(AppError::Action(action));
        }
        let id = Uuid::new_v4().to_string();
        let started = Utc::now().to_rfc3339();
        let profile_id = profile
            .as_ref()
            .and_then(|v| v.get("uuid"))
            .and_then(Value::as_str)
            .unwrap_or("local")
            .to_owned();
        if let Some(ref p) = profile {
            let provider = p.get("provider").and_then(Value::as_str).unwrap_or("");
            let valid = if cfg!(target_os = "windows") {
                provider == "hyperv-local"
            } else {
                provider == "kvm-local"
            };
            if !valid {
                return Err(AppError::Action(
                    "local provider does not match this host platform".into(),
                ));
            }
        }
        let job = Job {
            id: id.clone(),
            profile_id,
            action: action.clone(),
            status: "running".into(),
            started_at: started,
            completed_at: None,
            result: None,
            error: None,
        };
        let d = jobs_dir(&app)?;
        state
            .jobs
            .lock()
            .unwrap()
            .insert(job.id.clone(), job.clone());
        write_atomic(
            &d.join(format!("{}.json", job.id)),
            serde_json::to_vec_pretty(&job).unwrap().as_slice(),
        )?;
        let app_worker = app.clone();
        let job_for_worker = job.clone();
        let jobs_for_worker = state.jobs.clone();
        let app_for_block = app_worker.clone();
        tauri::async_runtime::spawn(async move {
            let result = tauri::async_runtime::spawn_blocking(move || {
                let store = app_dir(&app_for_block)?.join("hyperv");
                run_local(profile, action, parameters, &store)
            })
            .await;
            let (value, error) = match result {
                Ok(Ok(v)) => (Some(v), None),
                Ok(Err(e)) => (None, Some(e.to_string())),
                Err(e) => (None, Some(e.to_string())),
            };
            let mut done = job_for_worker.clone();
            let cleanup = value
                .as_ref()
                .and_then(|v| v.get("status"))
                .and_then(Value::as_str)
                == Some("cleanup_required");
            if let Some(v) = value.as_ref().and_then(|x| x.get("profile")).cloned() {
                if let Ok(summary) = validate_profile(&v) {
                    if let Ok(path) = profile_path(&app_worker, &summary.id) {
                        let _ =
                            write_atomic(&path, serde_json::to_vec_pretty(&v).unwrap().as_slice());
                    }
                }
            }
            done.status = if error.is_some() || cleanup {
                "failed".into()
            } else {
                "succeeded".into()
            };
            done.completed_at = Some(Utc::now().to_rfc3339());
            done.result = value;
            done.error = error;
            jobs_for_worker
                .lock()
                .unwrap()
                .insert(done.id.clone(), done.clone());
            if let Ok(dir) = jobs_dir(&app_worker) {
                let _ = write_atomic(
                    &dir.join(format!("{}.json", done.id)),
                    serde_json::to_vec_pretty(&done).unwrap().as_slice(),
                );
            }
        });
        Ok(job)
    }
    #[tauri::command]
    pub async fn start_job(
        state: State<'_, AppState>,
        profile_id: String,
        action: String,
        parameters: Value,
    ) -> Result<Job, AppError> {
        if !ACTIONS.contains(&action.as_str()) && action != "delete" {
            return Err(AppError::Action(action));
        }
        let profile = profile_value(&state.app, &profile_id)?;
        validate_profile(&profile)?;
        let provider = profile
            .get("provider")
            .and_then(Value::as_str)
            .unwrap_or("ssh")
            .to_owned();
        if provider == "hyperv-local" && !cfg!(target_os = "windows")
            || provider == "kvm-local" && cfg!(target_os = "windows")
        {
            return Err(AppError::Action(
                "profile provider does not match this host platform".into(),
            ));
        }
        if action == "delete" && provider != "hyperv-local" && provider != "kvm-local" {
            return Err(AppError::Action(
                "delete is only available for local providers".into(),
            ));
        }
        let id = Uuid::new_v4().to_string();
        let job = Job {
            id: id.clone(),
            profile_id: profile_id.clone(),
            action: action.clone(),
            status: "running".into(),
            started_at: Utc::now().to_rfc3339(),
            completed_at: None,
            result: None,
            error: None,
        };
        state.jobs.lock().unwrap().insert(id.clone(), job.clone());
        let _ = jobs_dir(&state.app).and_then(|d| {
            write_atomic(
                &d.join(format!("{id}.json")),
                serde_json::to_vec_pretty(&job).unwrap().as_slice(),
            )
        });
        let app = state.app.clone();
        let app_for_job = app.clone();
        let profile_id_for_persist = profile_id.clone();
        let local_store = app_dir(&state.app)?.join("hyperv");
        let jobs = state.jobs.clone();
        let locks = state.locks.clone();
        let forwards = state.forwards.clone();
        tauri::async_runtime::spawn(async move {
            let result = tauri::async_runtime::spawn_blocking(move || {
                let lock = {
                    let mut m = locks.lock().unwrap();
                    m.entry(profile_id.clone())
                        .or_insert_with(|| Arc::new(Mutex::new(())))
                        .clone()
                };
                let _guard = lock.lock().unwrap();
                let remove_service_id = parameters
                    .get("service_id")
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                let provider = profile
                    .get("provider")
                    .and_then(Value::as_str)
                    .unwrap_or("ssh");
                let remote = if provider == "hyperv-local" || provider == "kvm-local" {
                    run_local(
                        Some(profile.clone()),
                        action.clone(),
                        parameters,
                        &local_store,
                    )
                } else {
                    run_remote(profile.clone(), action.clone(), parameters)
                };
                match action.as_str() {
                    "publish" => remote.and_then(|value| {
                        if !matches!(
                            value.get("status").and_then(Value::as_str),
                            Some("ready" | "configured")
                        ) {
                            return Ok(value);
                        }
                        let service = value.get("service").cloned().ok_or_else(|| {
                            AppError::Operation("remote publish returned no service".into())
                        })?;
                        let mut result = value;
                        let provider = profile
                            .get("provider")
                            .and_then(Value::as_str)
                            .unwrap_or("ssh");
                        let tunnel = match if provider == "hyperv-local" || provider == "kvm-local"
                        {
                            let target = result
                                .get("local_target")
                                .and_then(Value::as_str)
                                .ok_or_else(|| {
                                    AppError::Operation(
                                        "local publish returned no local_target".into(),
                                    )
                                })?;
                            ensure_relay(&profile_id, &service, target, &forwards)
                        } else {
                            ensure_forward(&profile, &profile_id, &service, &forwards)
                        } {
                            Ok(tunnel) => tunnel,
                            Err(e) => {
                                if let Some(object) = result.as_object_mut() {
                                    object.insert(
                                        "status".into(),
                                        json!("configured_with_tunnel_failed"),
                                    );
                                    object.insert("tunnelError".into(), json!(e.to_string()));
                                }
                                return Ok(result);
                            }
                        };
                        if let Some(object) = result.as_object_mut() {
                            object.insert("tunnel".into(), tunnel);
                            if let Some(endpoint) = object
                                .get("tunnel")
                                .and_then(|t| t.get("endpoint"))
                                .cloned()
                            {
                                object.insert("endpoint".into(), endpoint);
                            }
                        }
                        Ok(result)
                    }),
                    "remove_service" => {
                        let result = remote;
                        if let Ok(ref value) = result {
                            let closed = value
                                .get("status")
                                .and_then(Value::as_str)
                                .map(|s| {
                                    s == "removed"
                                        || s == "absent"
                                        || (s == "cleanup_required"
                                            && value.get("host_closed").and_then(Value::as_bool)
                                                == Some(true))
                                })
                                .unwrap_or(false);
                            if closed {
                                if let Some(service_id) = remove_service_id {
                                    stop_forward(&forwards, &forward_key(&profile_id, &service_id));
                                }
                            }
                        }
                        result
                    }
                    "delete" => {
                        let result = remote;
                        if let Ok(ref value) = result {
                            if value.get("status").and_then(Value::as_str) == Some("deleted") {
                                let _ = fs::remove_file(
                                    profile_path(&app_for_job, &profile_id).unwrap_or_default(),
                                );
                                let prefix = format!("{profile_id}:");
                                let keys: Vec<String> = forwards
                                    .inner
                                    .lock()
                                    .unwrap()
                                    .keys()
                                    .filter(|k| k.starts_with(&prefix))
                                    .cloned()
                                    .collect();
                                for key in keys {
                                    stop_forward(&forwards, &key);
                                }
                            }
                        }
                        result
                    }
                    _ => remote,
                }
            })
            .await;
            let (result, mut error) = match result {
                Ok(Ok(v)) => (Some(v), None),
                Ok(Err(e)) => (None, Some(e.to_string())),
                Err(e) => (None, Some(e.to_string())),
            };
            if let Some(p) = result.as_ref().and_then(|v| v.get("profile")).cloned() {
                let matches_identity = p.get("uuid").and_then(Value::as_str)
                    == Some(&profile_id_for_persist)
                    && p.get("provider").and_then(Value::as_str).unwrap_or("ssh") == provider;
                if matches_identity {
                    if let Ok(path) = profile_path(&app, &profile_id_for_persist) {
                        let _ =
                            write_atomic(&path, serde_json::to_vec_pretty(&p).unwrap().as_slice());
                    }
                }
            }
            let mut j = jobs.lock().unwrap();
            if let Some(x) = j.get_mut(&id) {
                let cleanup_warning = result
                    .as_ref()
                    .and_then(|v| v.get("status"))
                    .and_then(Value::as_str)
                    .map(|status| {
                        status == "cleanup_required" || status == "configured_with_tunnel_failed"
                    })
                    .unwrap_or(false);
                if cleanup_warning && error.is_none() {
                    error = Some("remote configured; local cleanup or tunnel readiness failed; inspect before retry".into());
                }
                x.status = if error.is_some() || cleanup_warning {
                    "failed".into()
                } else {
                    "succeeded".into()
                };
                x.result = result;
                x.error = error;
                x.completed_at = Some(Utc::now().to_rfc3339());
                if let Ok(d) = jobs_dir(&app) {
                    let _ = write_atomic(
                        &d.join(format!("{id}.json")),
                        serde_json::to_vec_pretty(x).unwrap().as_slice(),
                    );
                }
            }
        });
        Ok(job)
    }
    #[tauri::command]
    pub fn open_rdp(app: AppHandle, profile_id: String) -> Result<(), AppError> {
        let v = profile_value(&app, &profile_id)?;
        let s = validate_profile(&v)?;
        let network = v
            .get("network")
            .and_then(Value::as_object)
            .ok_or_else(|| AppError::Profile("network is required".into()))?;
        let publish_ip = network
            .get("publish_ip")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::Profile("network.publish_ip is required for RDP".into()))?;
        let rdp_port = network
            .get("rdp_port")
            .and_then(Value::as_u64)
            .ok_or_else(|| AppError::Profile("network.rdp_port is required for RDP".into()))?;
        if rdp_port == 0
            || rdp_port > 65535
            || publish_ip.contains('\r')
            || publish_ip.contains('\n')
            || publish_ip.contains(' ')
        {
            return Err(AppError::Profile("RDP endpoint is invalid".into()));
        }
        let admin = s.admin_user.clone().unwrap_or_default();
        if admin.is_empty() || admin.contains('\r') || admin.contains('\n') {
            return Err(AppError::Profile("admin_user is invalid".into()));
        }
        let dir = app_dir(&app)?.join("rdp");
        fs::create_dir_all(&dir).map_err(|e| AppError::Storage(e.to_string()))?;
        let path = dir.join(format!("{}.rdp", s.id));
        fs::write(
            &path,
            format!(
                "full address:s:{}:{}\nusername:s:{}\nprompt for credentials:i:1\n",
                publish_ip, rdp_port, admin
            ),
        )
        .map_err(|e| AppError::Storage(e.to_string()))?;
        #[cfg(target_os = "windows")]
        {
            Command::new("mstsc.exe")
                .arg(&path)
                .spawn()
                .map_err(|e| AppError::Operation(e.to_string()))?;
        }
        #[cfg(target_os = "linux")]
        {
            let bin = ["xfreerdp3", "xfreerdp"]
                .iter()
                .find(|b| Command::new(b).arg("/version").output().is_ok())
                .ok_or_else(|| AppError::Operation("xfreerdp is not installed".into()))?;
            Command::new(bin)
                .arg(format!("/v:{publish_ip}:{rdp_port}"))
                .spawn()
                .map_err(|e| AppError::Operation(e.to_string()))?;
        }
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn profile() -> Value {
            json!({
                "schema_version": 1,
                "name": "devvm",
                "uuid": "8cb9f0f6-3d77-4c1c-8d0b-5ab70ac5f201",
                "root": "/home/vm_run/devvm",
                "ssh": {"host": "192.0.2.10", "user": "ops", "port": 22},
                "network": {"guest_ip": "172.30.61.2"},
                "windows": {"admin_user": "vmadmin"}
            })
        }

        #[test]
        fn profile_rejects_inline_credentials() {
            let mut value = profile();
            value["password"] = json!("secret");
            assert!(validate_profile(&value).is_err());
        }

        #[test]
        fn local_profiles_do_not_require_ssh() {
            let value = json!({
                "schema_version": 1,
                "provider": "hyperv-local",
                "name": "localvm",
                "uuid": "8cb9f0f6-3d77-4c1c-8d0b-5ab70ac5f201",
                "root": "C:\\\\VirtOps\\\\localvm",
                "resources": {"vcpus": 4, "memory_gib": 8},
                "network": {"publish_ip": "127.0.0.1", "rdp_port": 3389},
                "windows": {"admin_user": "vmadmin"}
            });
            let summary = validate_profile(&value).unwrap();
            assert_eq!(summary.host, "本机 Hyper-V");
            assert_eq!(summary.provider, "hyperv-local");
            assert_eq!(summary.admin_user.as_deref(), Some("vmadmin"));
        }

        #[test]
        fn profile_rejects_shell_injection_in_ssh_target() {
            let mut value = profile();
            value["ssh"]["host"] = json!("host;rm -rf /");
            assert!(profile_ssh(&value).is_err());
        }

        #[test]
        fn forwarding_arguments_are_loopback_and_strict() {
            let args = forward_args(&profile(), 18123, 18000).unwrap();
            assert!(args
                .windows(2)
                .any(|w| w == ["-o".to_string(), "StrictHostKeyChecking=yes".to_string()]));
            assert!(args
                .windows(2)
                .any(|w| w == ["-o".to_string(), "ExitOnForwardFailure=yes".to_string()]));
            assert!(args.iter().any(|x| x == "127.0.0.1:18123:127.0.0.1:18000"));
            assert!(!args.iter().any(|x| x == "-R" || x == "-g"));
        }

        #[test]
        fn remote_source_decodes_request_before_dispatch() {
            let source =
                build_remote_source(&json!({"action":"unknown","parameters":{},"profile":{}}));
            assert!(source.contains("REQUEST=json.loads(base64.b64decode("));
            assert!(source.contains("dispatch(REQUEST)"));
            assert!(!source.contains("REQUEST_B64 ="));
        }

        #[cfg(target_os = "windows")]
        #[test]
        fn windows_local_source_has_request_prelude_and_iso_module() {
            let source = build_local_powershell_source("eyJhY3Rpb24iOiJpbnNwZWN0X2lzbyJ9");
            assert!(source.contains("FromBase64String"));
            assert!(source.contains("function Inspect-Iso"));
            assert!(source.contains("function Build-BaseImage"));
            assert!(source.contains("switch($action)"));
        }

        #[cfg(target_os = "windows")]
        #[test]
        fn windows_environment_request_executes_through_encoded_bootstrap() {
            let root = std::env::temp_dir().join("virtops-environment-test");
            match run_local(None, "environment".into(), json!({}), &root) {
                Ok(result) => {
                    assert!(result.get("platform").is_some());
                    assert!(result.get("logicalCpus").is_some());
                }
                Err(error) => assert!(error.to_string().contains("status")),
            }
        }

        #[test]
        fn registry_clone_does_not_kill_owned_child() {
            let registry = ForwardRegistry::new();
            let mut child = if cfg!(target_os = "windows") {
                Command::new("cmd")
                    .args(["/C", "ping", "127.0.0.1", "-n", "20"])
                    .spawn()
                    .unwrap()
            } else {
                Command::new("sh").args(["-c", "sleep 20"]).spawn().unwrap()
            };
            let pid = child.id();
            #[cfg(target_os = "windows")]
            let job = attach_kill_on_close(&mut child).unwrap();
            registry.inner.lock().unwrap().insert(
                "test".into(),
                Forward {
                    handle: ForwardHandle::Child {
                        child: Arc::new(Mutex::new(child)),
                        #[cfg(target_os = "windows")]
                        job,
                    },
                    local_port: 19001,
                    signature: vec!["test".into()],
                },
            );
            let clone = registry.clone();
            drop(clone);
            let alive = registry
                .inner
                .lock()
                .unwrap()
                .get("test")
                .unwrap()
                .handle
                .clone();
            let alive = match alive {
                ForwardHandle::Child { child, .. } => {
                    child.lock().unwrap().try_wait().unwrap().is_none()
                }
                _ => false,
            };
            assert!(
                alive,
                "dropping a registry clone must not kill the shared child {pid}"
            );
            registry.shutdown();
        }

        #[cfg(target_os = "windows")]
        #[test]
        fn windows_job_handle_close_terminates_child() {
            let mut child = Command::new("cmd")
                .args(["/C", "ping", "127.0.0.1", "-n", "30"])
                .spawn()
                .unwrap();
            let job = attach_kill_on_close(&mut child).unwrap();
            drop(job);
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            loop {
                if child.try_wait().unwrap().is_some() {
                    break;
                }
                assert!(
                    std::time::Instant::now() < deadline,
                    "job close did not terminate child"
                );
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
        }
    }
}
pub fn run() -> tauri::Result<()> {
    tauri::Builder::default()
        .setup(|app| {
            app.manage(AppState {
                app: app.handle().clone(),
                jobs: Arc::new(Mutex::new(HashMap::new())),
                locks: Arc::new(Mutex::new(HashMap::new())),
                forwards: ForwardRegistry::new(),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::app_info,
            commands::list_profiles,
            commands::import_profile,
            commands::delete_profile,
            commands::list_jobs,
            commands::start_job,
            commands::local_environment,
            commands::local_action,
            commands::open_rdp
        ])
        .build(tauri::generate_context!())?
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                app.state::<AppState>().forwards.shutdown();
            }
        });
    Ok(())
}
