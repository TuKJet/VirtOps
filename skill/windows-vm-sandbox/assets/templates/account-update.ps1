[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
$root='C:\ProgramData\VirtOps';$full=[IO.Path]::GetFullPath($RequestPath)
if($full -notmatch '^C:\\ProgramData\\VirtOps\\account-request-([0-9a-fA-F-]{36})\.json$'){throw 'REQUEST_PATH'}
$id=[guid]$Matches[1]
if((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'STATE_ROOT_REPARSE'}
if((Get-Item -LiteralPath $full).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'REQUEST_REPARSE'}
$r=Get-Content -LiteralPath $full -Raw|ConvertFrom-Json
if([string]$r.request_id -ne $id.ToString() -or [string]$r.computer_name -ine $env:COMPUTERNAME){throw 'REQUEST_IDENTITY'}
$old=[string]$r.old_admin_user;$new=[string]$r.admin_user
if($old -notmatch '^[A-Za-z][A-Za-z0-9_.-]{0,19}$' -or $new -notmatch '^[A-Za-z][A-Za-z0-9_.-]{0,19}$'){throw 'ACCOUNT_NAME'}
$stagePath=Join-Path $root ('account-stage-'+$id.ToString()+'.json')
$adminSid=[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
function Save-Stage($state){
    $temporary=$stagePath+'.tmp'
    $state|ConvertTo-Json -Compress|Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $stagePath -Force
}
function Test-Account([string]$Name,[string]$Password,[string]$ExpectedSid){
    $secure=ConvertTo-SecureString $Password -AsPlainText -Force
    $credential=[PSCredential]::new("$env:COMPUTERNAME\$Name",$secure)
    $options=New-PSSessionOption -SkipCACheck -SkipCNCheck -SkipRevocationCheck -OpenTimeout 10000
    $session=$null
    try{
        $session=New-PSSession -ComputerName 127.0.0.1 -UseSSL -Port 5986 -Credential $credential -Authentication Negotiate -SessionOption $options
        $proof=Invoke-Command -Session $session -ScriptBlock {
            $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
            @{sid=$identity.User.Value;admin=([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)}
        }
        return ($proof.sid -eq $ExpectedSid -and $proof.admin)
    }catch{return $false}finally{if($session){Remove-PSSession $session}}
}
try{
    if(Test-Path -LiteralPath $stagePath){
        if((Get-Item -LiteralPath $stagePath).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'STAGE_REPARSE'}
        $state=Get-Content -LiteralPath $stagePath -Raw|ConvertFrom-Json
        if($state.old_user -ne $old -or $state.new_user -ne $new){throw 'STAGE_REQUEST_MISMATCH'}
    }else{
        $user=Get-LocalUser -Name $old
        $collision=Get-LocalUser -Name $new -ErrorAction SilentlyContinue
        if($collision -and $collision.SID.Value -ne $user.SID.Value){throw 'ACCOUNT_NAME_COLLISION'}
        if(-not $user.Enabled -or @((Get-LocalGroupMember -SID $adminSid)|Where-Object {$_.SID.Value -eq $user.SID.Value}).Count -ne 1){throw 'ACCOUNT_NOT_ADMIN'}
        $state=[pscustomobject]@{old_user=$old;new_user=$new;sid=$user.SID.Value;phase='prepared'}
        Save-Stage $state
    }
    $same=Get-LocalUser -SID ([Security.Principal.SecurityIdentifier]$state.sid)
    if($same.Name -notin @($old,$new)){throw 'ACCOUNT_IDENTITY'}
    if($state.phase -eq 'prepared'){
        # A crash can occur after ChangePassword but before saving the stage.
        # Try only the recorded new credential; never reset or guess a password.
        if(-not(Test-Account $same.Name ([string]$r.password) $state.sid)){
            $adsi=[ADSI]("WinNT://./"+$same.Name+",user")
            try{
                $adsi.ChangePassword([string]$r.old_password,[string]$r.password)
            }catch{
                # The WinNT ADSI provider only lets an account change its own
                # password. An administrator changing another account's
                # password is refused with "Access denied"; SetPassword is the
                # administrative path and does not need the old password.
                $adsi.SetPassword([string]$r.password)
            }
        }
        $state.phase='password_changed';Save-Stage $state
    }
    if($state.phase -eq 'password_changed'){
        $collision=Get-LocalUser -Name $new -ErrorAction SilentlyContinue
        if($collision -and $collision.SID.Value -ne $state.sid){throw 'ACCOUNT_NAME_COLLISION'}
        if($same.Name -cne $new){Rename-LocalUser -SID ([Security.Principal.SecurityIdentifier]$state.sid) -NewName $new}
        $state.phase='renamed';Save-Stage $state
    }
    $user=Get-LocalUser -Name $new
    if($user.SID.Value -ne $state.sid -or -not $user.Enabled){throw 'ACCOUNT_IDENTITY'}
    if(-not(Test-Account $new ([string]$r.password) $state.sid)){throw 'WINRM_HTTPS_AUTH_FAILED'}
    $state.phase='verified';Save-Stage $state
    [pscustomobject]@{schema_version=1;state='succeeded';request_id=$id.ToString();old_user=$old;admin_user=$new;sid=$state.sid;enabled=$true;is_admin=$true;winrm_https_authenticated=$true}|ConvertTo-Json -Compress
}catch{
    [pscustomobject]@{state='failed';request_id=$id.ToString();code=if($_.Exception.Message -match '^[A-Z0-9_]+$'){$_.Exception.Message}else{'ACCOUNT_UPDATE_FAILED'}}|ConvertTo-Json -Compress
    exit 1
}finally{Remove-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue}
