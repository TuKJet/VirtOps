[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidateSet('inspect','run','upload','download','clipboard','restart')] [string] $Action,
    [Parameter(Mandatory)] [string] $ProfilePath,
    [string] $LocalPath,
    [string] $RemotePath,
    [int] $TimeoutSeconds = 300,
    [ValidateSet('inspect','enable','disable')] [string] $ClipboardMode = 'inspect',
    [switch] $LibraryOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Fail([string] $Message) { throw "guest.ps1: $Message" }
function Read-Profile {
    if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) { Fail "profile not found: $ProfilePath" }
    try { $p = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json } catch { Fail "invalid profile JSON: $($_.Exception.Message)" }
    foreach ($path in @('network.publish_ip','network.winrm_port','windows.computer_name','windows.admin_user','winrm_thumbprint','credentials.local_clixml')) {
        $value = $p; foreach ($part in $path.Split('.')) { $value = $value.$part }
        if ([string]::IsNullOrWhiteSpace([string]$value)) { Fail "profile field is required: $path" }
    }
    if ([string]$p.network.publish_ip -notmatch '^[0-9a-fA-F:.]+$') { Fail 'network.publish_ip must be an IP address' }
    if ([int]$p.network.winrm_port -lt 1 -or [int]$p.network.winrm_port -gt 65535) { Fail 'network.winrm_port is invalid' }
    if ([string]$p.winrm_thumbprint -notmatch '^[0-9A-Fa-f]{40}$') { Fail 'winrm_thumbprint must be 40 hex characters' }
    if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 3600) { Fail 'TimeoutSeconds must be 1..3600' }
    return $p
}
function Get-Endpoint($Profile) { return "https://$($Profile.network.publish_ip):$($Profile.network.winrm_port)/wsman" }
function Assert-SafeRemotePath([string] $Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { Fail 'RemotePath is required' }
    $normalized = $Path.Replace('/','\')
    if ($Path -match '[*?\[\]]' -or $Path -match '[\r\n]' -or ($normalized.Length -gt 2 -and $normalized.Substring(2).Contains(':'))) { Fail 'RemotePath has a wildcard, newline, or alternate data stream' }
    $full = [IO.Path]::GetFullPath($normalized)
    if (-not $full.StartsWith('C:\Work\', [StringComparison]::OrdinalIgnoreCase)) { Fail 'RemotePath must stay below C:\Work' }
    return $full
}
function Get-Thumbprint([string] $EndpointHost, [int] $Port) {
    $tcp = New-Object Net.Sockets.TcpClient
    try {
        $tcp.ReceiveTimeout = 10000; $tcp.SendTimeout = 10000
        $tcp.ConnectAsync($EndpointHost, $Port).Wait(10000) | Out-Null
        if (-not $tcp.Connected) { Fail "WinRM TLS connection timeout: ${EndpointHost}:$Port" }
        $callback = [Net.Security.RemoteCertificateValidationCallback]{ param($sender,$cert,$chain,$errors) return $true }
        $ssl = New-Object -TypeName Net.Security.SslStream -ArgumentList @($tcp.GetStream(), $false, $callback)
        try { $ssl.AuthenticateAsClient($EndpointHost); return $ssl.RemoteCertificate.GetCertHashString().ToUpperInvariant() } finally { $ssl.Dispose() }
    } finally { $tcp.Dispose() }
}
function Assert-PinnedCertificate($Profile) {
    $actual = Get-Thumbprint ([string]$Profile.network.publish_ip) ([int]$Profile.network.winrm_port)
    $expected = ([string]$Profile.winrm_thumbprint).ToUpperInvariant()
    if ($actual -ne $expected) { Fail "WinRM certificate pin mismatch (expected $expected, got $actual)" }
    return $actual
}
function Get-CredentialFromProfile($Profile) {
    $path = [Environment]::ExpandEnvironmentVariables([string]$Profile.credentials.local_clixml)
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Fail "credential file not found: $path (run credentials.ps1 first)" }
    try { $cred = Import-Clixml -LiteralPath $path } catch { Fail "cannot import DPAPI credential file: $($_.Exception.Message)" }
    if ($cred -isnot [Management.Automation.PSCredential]) { Fail 'credential file does not contain a PSCredential' }
    $expectedUser = "$($Profile.windows.computer_name)\$($Profile.windows.admin_user)"
    if ($cred.UserName -ine $expectedUser) { Fail "credential username does not match profile computer/user" }
    return $cred
}
function New-WinRmSession($Profile) {
    $null = Assert-PinnedCertificate $Profile
    $cred = Get-CredentialFromProfile $Profile
    $session = New-PSSession -ComputerName ([string]$Profile.network.publish_ip) -Port ([int]$Profile.network.winrm_port) -UseSSL -Authentication Negotiate -Credential $cred `
        -SessionOption (New-PSSessionOption -SkipCACheck -SkipCNCheck) -ErrorAction Stop
    try {
        $null = Assert-PinnedCertificate $Profile
        $actual = Invoke-Command -Session $session -ScriptBlock {
            $base=Get-Item -LiteralPath 'C:\Work' -ErrorAction Stop
            if (-not $base.PSIsContainer -or ($base.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'C:\Work must be a real directory' }
            [Security.Principal.WindowsIdentity]::GetCurrent().Name
        }
        if ([string]$actual -ine "$($Profile.windows.computer_name)\$($Profile.windows.admin_user)") { Fail 'WinRM identity does not match profile' }
    } catch { Remove-PSSession $session -ErrorAction SilentlyContinue; throw }
    return $session
}
function Invoke-Remote($Session, [scriptblock] $Script, [object[]] $RemoteArguments = @()) {
    return Invoke-Command -Session $Session -ScriptBlock $Script -ArgumentList $RemoteArguments -ErrorAction Stop
}
if ($LibraryOnly) { return }
$profile = Read-Profile
$session = $null
try {
    if ($Action -eq 'restart') {
        $session = New-WinRmSession $profile
        $result = Invoke-Remote $session {
            param($restartReason)
            $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
            & "$env:SystemRoot\System32\shutdown.exe" /r /t 0 /d p:0:0 /c $restartReason
            if ($LASTEXITCODE -ne 0) { throw "Guest restart request failed: $LASTEXITCODE" }
            [pscustomobject]@{restart_requested=$true; previous_boot_utc=$boot; force=$false}
        } @('VirtOps: requested Windows restart')
        $result | ConvertTo-Json -Depth 4
        exit 0
    }
    if ($Action -eq 'clipboard') {
        $session = New-WinRmSession $profile
        $result = Invoke-Remote $session {
            param($clipboardMode)
            $path = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services'
            $names = @('fDisableClip','fDisableCdm')
            function Read-ClipboardPolicy {
                $values = [ordered]@{}
                foreach ($name in $names) {
                    $values[$name] = Get-ItemPropertyValue -LiteralPath $path -Name $name -ErrorAction SilentlyContinue
                }
                [pscustomobject]$values
            }
            $before = Read-ClipboardPolicy
            if ($clipboardMode -ne 'inspect') {
                $value = if ($clipboardMode -eq 'enable') { 0 } else { 1 }
                New-Item -Path $path -Force | Out-Null
                foreach ($name in $names) {
                    New-ItemProperty -LiteralPath $path -Name $name -PropertyType DWord -Value $value -Force | Out-Null
                }
            }
            $after = Read-ClipboardPolicy
            if ($clipboardMode -ne 'inspect' -and ($after.fDisableClip -ne $value -or $after.fDisableCdm -ne $value)) {
                throw 'RDP clipboard policy readback failed'
            }
            $sessions = @()
            $quser = Join-Path $env:SystemRoot 'System32\quser.exe'
            if (Test-Path -LiteralPath $quser) { $sessions = @(& $quser 2>$null) }
            [pscustomobject]@{
                mode = $clipboardMode
                before = $before
                after = $after
                reconnect_required = $clipboardMode -ne 'inspect'
                sessions = $sessions
                clipboard_processes = @(Get-Process -Name rdpclip -ErrorAction SilentlyContinue | Select-Object Id,SessionId)
            }
        } @($ClipboardMode)
        $result | ConvertTo-Json -Depth 6
        exit 0
    }
    if ($Action -eq 'inspect') {
        $session = New-WinRmSession $profile
        $identity = Invoke-Remote $session { [pscustomobject]@{
            computer_name = $env:COMPUTERNAME
            identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            powershell = $PSVersionTable.PSVersion.ToString()
            last_boot_utc = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
            winrm = (Get-Service WinRM).Status.ToString()
            c_work = Test-Path -LiteralPath 'C:\Work'
        } }
        $expected = "$($profile.windows.computer_name)\$($profile.windows.admin_user)"
        if ([string]$identity.identity -ine $expected) { Remove-PSSession $session -ErrorAction SilentlyContinue; Fail "WinRM identity does not match profile" }
        $identity | ConvertTo-Json -Depth 4
        exit 0
    }
    if ($Action -in @('upload','download') -and [string]::IsNullOrWhiteSpace($LocalPath)) { Fail 'LocalPath is required' }
    $remote = $null
    if ($Action -in @('upload','download')) { $remote = Assert-SafeRemotePath $RemotePath }
    if ($Action -eq 'upload') {
        if (-not (Test-Path -LiteralPath $LocalPath -PathType Leaf)) { Fail "local file not found: $LocalPath" }
        $session = New-WinRmSession $profile
        $parent = Split-Path -Parent $remote
        Invoke-Remote $session { param($d) $base=Get-Item -LiteralPath 'C:\Work' -ErrorAction Stop; if($base.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'C:\Work is a reparse point'}; $cur='C:\Work'; foreach($part in ($d.Substring(8).Trim('\').Split('\') | Where-Object {$_})) { $cur=Join-Path $cur $part; $i=Get-Item -LiteralPath $cur -ErrorAction SilentlyContinue; if($i -and ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'remote parent is a reparse point'} }; New-Item -ItemType Directory -Force -Path $d | Out-Null } @($parent) | Out-Null
        Invoke-Remote $session { param($p) $i=Get-Item -LiteralPath $p -ErrorAction SilentlyContinue; if($i -and ($i.PSIsContainer -or ($i.Attributes -band [IO.FileAttributes]::ReparsePoint))) { throw 'upload target must be a regular file path' } } @($remote) | Out-Null
        Copy-Item -LiteralPath $LocalPath -Destination $remote -ToSession $session -Force
        $localHash = (Get-FileHash -LiteralPath $LocalPath -Algorithm SHA256).Hash.ToUpperInvariant()
        $remoteHash = [string](Invoke-Remote $session { param($p) if ((Get-Item -LiteralPath $p).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'remote path is a reparse point' }; (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash } @($remote))
        if ($localHash -ne $remoteHash.ToUpperInvariant()) { Fail 'upload hash verification failed' }
        [pscustomobject]@{ action='upload'; remote_path=$remote; sha256=$localHash } | ConvertTo-Json
        exit 0
    }
    if ($Action -eq 'download') {
        if (Test-Path -LiteralPath $LocalPath) { Fail 'download refuses to overwrite an existing local path' }
        $session = New-WinRmSession $profile
        $remoteInfo = Invoke-Remote $session { param($p) $cur='C:\Work'; foreach($part in ($p.Substring(8).Trim('\').Split('\') | Where-Object {$_})) { $cur=Join-Path $cur $part; $i=Get-Item -LiteralPath $cur -ErrorAction SilentlyContinue; if($i -and ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'remote path is a reparse point'} }; [pscustomobject]@{exists=[bool]$i -and $i.PSIsContainer -eq $false; hash=if($i -and -not $i.PSIsContainer){(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash}else{''}} } @($remote)
        if (-not [bool]$remoteInfo.exists) { Fail "remote file not found: $remote" }
        $parent = Split-Path -Parent $LocalPath
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Copy-Item -FromSession $session -LiteralPath $remote -Destination $LocalPath
        $localHash = (Get-FileHash -LiteralPath $LocalPath -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($localHash -ne ([string]$remoteInfo.hash).ToUpperInvariant()) { Fail 'download hash verification failed' }
        [pscustomobject]@{ action='download'; local_path=$LocalPath; sha256=$localHash } | ConvertTo-Json
        exit 0
    }
    if ($Action -eq 'run') {
        if (-not (Test-Path -LiteralPath $LocalPath -PathType Leaf)) { Fail "local script not found: $LocalPath" }
        if ($LocalPath -notmatch '\.ps1$') { Fail 'run accepts a .ps1 script only' }
        $session = New-WinRmSession $profile
        $token = [guid]::NewGuid().ToString('N'); $dir = "C:\Work\virtops-$($token.Substring(0,12))"; $remoteScript = "$dir\script.ps1"
        Invoke-Remote $session { param($d) New-Item -ItemType Directory -Force -Path $d | Out-Null } @($dir) | Out-Null
        try {
            Copy-Item -LiteralPath $LocalPath -Destination $remoteScript -ToSession $session -Force
            $localHash = (Get-FileHash -LiteralPath $LocalPath -Algorithm SHA256).Hash.ToUpperInvariant()
            $remoteHash = [string](Invoke-Remote $session { param($p) (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash } @($remoteScript))
            if ($localHash -ne $remoteHash.ToUpperInvariant()) { Fail 'run upload hash verification failed' }
            $result = Invoke-Remote $session {
                param($script,$timeout)
                $out = Join-Path $env:TEMP ("virtops-out-" + [guid]::NewGuid().ToString('N') + '.txt')
                $err = Join-Path $env:TEMP ("virtops-err-" + [guid]::NewGuid().ToString('N') + '.txt')
                $process=$null; $outStream=$null; $errStream=$null
                try {
                    $process=[Diagnostics.Process]::new()
                    $info=[Diagnostics.ProcessStartInfo]::new()
                    $info.FileName="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
                    $command="[Console]::OutputEncoding=[Text.UTF8Encoding]::new(`$false);`$ErrorActionPreference='Stop';`$ProgressPreference='SilentlyContinue';`$global:LASTEXITCODE=0;try{& '$script';`$ok=`$?;if(`$LASTEXITCODE -ne 0){exit `$LASTEXITCODE};if(-not `$ok){exit 1};exit 0}catch{[Console]::Error.WriteLine(`$_);exit 1}"
                    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
                    $info.Arguments="-NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -EncodedCommand $encoded"
                    $info.WorkingDirectory=Split-Path -Parent $script
                    $info.UseShellExecute=$false; $info.CreateNoWindow=$true
                    $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
                    $process.StartInfo=$info
                    if(-not $process.Start()){throw 'child did not start'}
                    $outStream=[IO.File]::Create($out); $errStream=[IO.File]::Create($err)
                    $outTask=$process.StandardOutput.BaseStream.CopyToAsync($outStream)
                    $errTask=$process.StandardError.BaseStream.CopyToAsync($errStream)
                    $timedOut=-not $process.WaitForExit($timeout*1000)
                    if($timedOut){& taskkill.exe /PID $process.Id /T /F | Out-Null;if(-not $process.WaitForExit(10000)){throw 'timed out child could not be stopped'}}
                    $code=if($timedOut){124}else{$process.ExitCode}
                    if(-not $outTask.Wait(10000) -or -not $errTask.Wait(10000)){throw 'child output stream did not close'}
                    $outStream.Dispose();$outStream=$null; $errStream.Dispose();$errStream=$null
                    function Read-Capture([string]$path){
                        $reader=[IO.File]::OpenText($path)
                        try{$buffer=New-Object char[] 262144;$n=$reader.Read($buffer,0,$buffer.Length);[string]::new($buffer,0,$n)}finally{$reader.Dispose()}
                    }
                    [pscustomobject]@{exit_code=$code;timed_out=$timedOut;stdout=(Read-Capture $out);stderr=(Read-Capture $err);output_limit_chars=262144}
                } finally {
                    if($process){if(-not $process.HasExited){& taskkill.exe /PID $process.Id /T /F | Out-Null};$process.Dispose()}
                    if($outStream){$outStream.Dispose()};if($errStream){$errStream.Dispose()}
                    Remove-Item -LiteralPath $out,$err -Force -ErrorAction SilentlyContinue
                }
            } @($remoteScript,$TimeoutSeconds)
            $result | Add-Member -NotePropertyName workdir -NotePropertyValue $dir
            $result | Add-Member -NotePropertyName upload_sha256 -NotePropertyValue $localHash
            $result | ConvertTo-Json -Depth 5
            if ([int]$result.exit_code -ne 0) { exit ([int]$result.exit_code) }
        } finally {
            Invoke-Remote $session { param($p,$d) Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue; if ((Test-Path -LiteralPath $d -PathType Container) -and -not (Get-ChildItem -LiteralPath $d -Force | Select-Object -First 1)) { Remove-Item -LiteralPath $d -Force } } @($remoteScript,$dir) | Out-Null
        }
    }
} finally { if ($session) { Remove-PSSession $session -ErrorAction SilentlyContinue } }
