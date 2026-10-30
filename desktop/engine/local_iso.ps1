# ISO-first Hyper-V template builder. This file is a function module; the Rust
# provider loads it before local_hyperv.ps1 and never passes secrets on a
# command line. It does not enable Hyper-V or change host networking.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:VirtOpsIsoOwned = @{}

function Iso-Absolute([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path) -or $Path.StartsWith('\\') -or $Path.Contains("`r") -or $Path.Contains("`n")) { throw 'ISO path must be a local absolute path' }
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.PSIsContainer -or $item.Attributes -band [IO.FileAttributes]::ReparsePoint -or [IO.Path]::GetExtension($item.FullName).ToLowerInvariant() -ne '.iso') { throw 'Select a local ISO file' }
    return $item.FullName
}

function Get-IsoDrive([string]$Path) {
    $existing = @(Get-DiskImage -ImagePath $Path -ErrorAction SilentlyContinue); $attached = $existing.Count -gt 0 -and [bool]$existing[0].Attached
    if (-not $attached) { Mount-DiskImage -ImagePath $Path -Access ReadOnly -PassThru | Out-Null; $script:VirtOpsIsoOwned[$Path] = $true }
    try {
        $volumes = @(Get-DiskImage -ImagePath $Path | Get-Volume | Where-Object DriveLetter)
        if ($volumes.Count -ne 1) { throw 'ISO must expose exactly one readable volume' }
        return ([string]$volumes[0].DriveLetter + ':')
    } catch {
        if ($script:VirtOpsIsoOwned.ContainsKey($Path)) { Dismount-DiskImage -ImagePath $Path -ErrorAction SilentlyContinue; $script:VirtOpsIsoOwned.Remove($Path) }
        throw
    }
}

function Dismount-Iso([string]$Path) { if ($script:VirtOpsIsoOwned.ContainsKey($Path)) { Dismount-DiskImage -ImagePath $Path -ErrorAction SilentlyContinue; $script:VirtOpsIsoOwned.Remove($Path) } }

function Select-WindowsImage([object[]]$Images, [int]$Index = 0) {
    if ($Index -gt 0) { $selected = @($Images | Where-Object { [int]$_.ImageIndex -eq $Index }) }
    else {
        $selected = @($Images | Where-Object {
            $candidateArchitecture = $_.Architecture
            $numericArchitecture = 0; if ([int]::TryParse([string]$candidateArchitecture, [ref]$numericArchitecture) -and $numericArchitecture -eq 9) { $candidateArchitecture = 'amd64' }
            [string]$_.ImageName -match '(?i)Windows 1[01].*(Pro|Enterprise)' -and [string]$candidateArchitecture -match '(?i)amd64|x64'
        })
    }
    if ($selected.Count -ne 1) { throw 'ISO must contain exactly one selected Windows Pro/Enterprise x64 image; choose image_index explicitly when needed' }
    $architecture = $selected[0].Architecture
    $numericArchitecture = 0; if ([int]::TryParse([string]$architecture, [ref]$numericArchitecture) -and $numericArchitecture -eq 9) { $architecture = 'amd64' }
    if ([string]$selected[0].ImageName -notmatch '(?i)\b(Pro|Enterprise)\b' -or [string]$architecture -notmatch '(?i)amd64|x64') { throw 'Selected image must be Windows Pro or Enterprise x64' }
    return $selected[0]
}

function Get-InstallImages([string]$Root) {
    $install = Join-Path $Root 'sources\install.wim'
    if (-not (Test-Path -LiteralPath $install)) { $install = Join-Path $Root 'sources\install.esd' }
    if (-not (Test-Path -LiteralPath $install)) { throw 'ISO has no sources/install.wim or install.esd' }
    $dism = Get-Command Get-WindowsImage -ErrorAction SilentlyContinue
    if (-not $dism) { throw 'Get-WindowsImage is required; install the Windows DISM PowerShell module' }
    $images=@(foreach($entry in @(Get-WindowsImage -ImagePath $install -ErrorAction Stop)) {
        Get-WindowsImage -ImagePath $install -Index $entry.ImageIndex -ErrorAction Stop
    })
    return @{ path = $install; images = $images }
}

function Get-OfflineInstallPlan([string]$InstallImage, [int]$Index, [string]$WindowsRoot, [string]$EfiRoot) {
    if (-not [IO.Path]::IsPathRooted($InstallImage) -or -not [IO.Path]::IsPathRooted($WindowsRoot) -or -not [IO.Path]::IsPathRooted($EfiRoot)) { throw 'Offline install paths must be absolute' }
    if ($Index -lt 1) { throw 'Offline image index must be positive' }
    if($EfiRoot-notmatch'^[A-Za-z]:$'){throw 'BCDBoot requires the explicitly assigned EFI volume letter'}
    return [pscustomobject]@{ dism = @('/Apply-Image',('/ImageFile:' + $InstallImage),('/Index:' + $Index),('/ApplyDir:' + $WindowsRoot)); bcdboot = @((Join-Path $WindowsRoot 'Windows'),('/s'),$EfiRoot,'/f','UEFI'); target_disk = 0; efi_partition = 1; msr_partition = 2; windows_partition = 3 }
}

function Inspect-Iso([string]$Store, $Parameters) {
    $path = Iso-Absolute ([string](Value $Parameters 'iso_path' ''))
    $drive = $null
    try {
        $drive = Get-IsoDrive $path
        $catalog = Get-InstallImages $drive
        $images = @(foreach ($image in $catalog.images) {
            [pscustomobject]@{ index = [int]$image.ImageIndex; name = [string]$image.ImageName; edition = [string]$image.ImageName; architecture = [string]$image.Architecture; languages = @($image.Languages) }
        })
        return [pscustomobject]@{ status = 'ok'; path = $path; install_image = $catalog.path; images = $images }
    } finally { if ($drive) { Dismount-Iso $path } }
}

function Xml-Escape([string]$Value) { return [Security.SecurityElement]::Escape([string]$Value) }

function New-TemplateUnattend([string]$ComputerName, [string]$UserName, [string]$Password, [int]$ImageIndex = 1, [string]$Language = 'en-US') {
    if ($ComputerName -notmatch '^[A-Za-z][A-Za-z0-9-]{0,14}$' -or $UserName -notmatch '^[A-Za-z][A-Za-z0-9_.-]{0,19}$') { throw 'Bootstrap names are invalid' }
    if ($Password.Length -lt 12 -or $Password -match '[\r\n]') { throw 'Bootstrap password must be at least 12 characters without line breaks' }
    $name = Xml-Escape $ComputerName; $user = Xml-Escape $UserName; $password = Xml-Escape $Password; $lang = Xml-Escape $Language
    return @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
 <settings pass="windowsPE"><component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><SetupUILanguage><UILanguage>$lang</UILanguage></SetupUILanguage><InputLocale>$lang</InputLocale><SystemLocale>$lang</SystemLocale><UILanguage>$lang</UILanguage><UserLocale>$lang</UserLocale></component><component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><ImageInstall><OSImage><InstallFrom><MetaData wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"><Key>/IMAGE/INDEX</Key><Value>$ImageIndex</Value></MetaData></InstallFrom><InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo><WillShowUI>OnError</WillShowUI></OSImage></ImageInstall><DiskConfiguration><WillShowUI>OnError</WillShowUI><Disk wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"><DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk><CreatePartitions><CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>100</Size></CreatePartition><CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition><CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition></CreatePartitions><ModifyPartitions><ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition><ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition></ModifyPartitions></Disk></DiskConfiguration></component></settings>
 <settings pass="specialize"><component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><ComputerName>$name</ComputerName></component></settings>
 <settings pass="oobeSystem"><component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><OOBE><HideEULAPage>true</HideEULAPage><HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE><ProtectYourPC>3</ProtectYourPC></OOBE><UserAccounts><LocalAccounts><LocalAccount xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" wcm:action="add"><Name>$user</Name><Group>Administrators</Group><Password><Value>$password</Value><PlainText>true</PlainText></Password></LocalAccount></LocalAccounts></UserAccounts><AutoLogon><Username>$user</Username><Enabled>true</Enabled><LogonCount>1</LogonCount><Password><Value>$password</Value><PlainText>true</PlainText></Password></AutoLogon></component></settings>
</unattend>
"@
}

function New-BuilderVhd([string]$Path, [int]$SizeGiB) {
    if ($SizeGiB -lt 64 -or $SizeGiB -gt 2048) { throw 'Template disk size must be 64..2048 GiB' }
    New-VHD -Path $Path -SizeBytes ([long]$SizeGiB * 1GB) -Dynamic -ErrorAction Stop | Out-Null
}

function Add-BootstrapAcl([string]$VmName) {
    if (@(Get-VMNetworkAdapter -VMName $VmName -ErrorAction Stop).Count -eq 0) { return }
    Add-VMNetworkAdapterExtendedAcl -VMName $VmName -Action Deny -Direction Inbound -Weight 1
    Add-VMNetworkAdapterExtendedAcl -VMName $VmName -Action Deny -Direction Outbound -Weight 1
}

function Test-LocalIsoModule { [pscustomobject]@{ status = 'ok'; functions = @('Inspect-Iso','Build-BaseImage','New-TemplateUnattend','Select-WindowsImage') } }

# The implementation below is the executable ISO -> sealed base-image path.
function Quote-WindowsArgument([string]$Value) {
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}
function Invoke-Checked([string]$File, [string[]]$Arguments, [int]$TimeoutSeconds = 900) {
    $psi = [Diagnostics.ProcessStartInfo]::new(); $psi.FileName = $File; $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.Arguments = (($Arguments | ForEach-Object { Quote-WindowsArgument ([string]$_) }) -join ' ')
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $psi
    if (-not $process.Start()) { throw "Unable to start $File" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync(); $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) { $process.Kill();$process.WaitForExit(); throw "$File timed out" }
    $stdoutTask.Wait(); $stderrTask.Wait(); $stdout = $stdoutTask.Result; $stderr = $stderrTask.Result
    if ($process.ExitCode -ne 0) { throw "$File failed ($($process.ExitCode)): $($stderr.Trim())" }
    return $stdout
}

function New-OfflineWindowsDisk([string]$Disk, [string]$InstallImage, [int]$Index, [string]$Unattend, [DateTime]$Deadline) {
    $mounted = $null; $efi = $null; $windows = $null; $diskNumber = $null; $efiPartition = $null; $windowsPartition = $null
    try {
        $mounted = Mount-VHD -Path $Disk -NoDriveLetter -Passthru
        $diskNumber = $mounted.DiskNumber
        Set-Disk -Number $diskNumber -IsOffline $false -ErrorAction Stop
        Initialize-Disk -Number $diskNumber -PartitionStyle GPT -Confirm:$false
        $efiPartition = New-Partition -DiskNumber $diskNumber -Size 260MB -GptType '{C12A7328-F81F-11D2-BA4B-00A0C93EC93B}'
        $null = New-Partition -DiskNumber $diskNumber -Size 16MB -GptType '{E3C9E316-0B5C-4DB8-817D-F92DF00215AE}'
        $windowsPartition = New-Partition -DiskNumber $diskNumber -UseMaximumSize -GptType '{EBD0A0A2-B9E5-4433-87C0-68B6B72699C7}'
        $efiLetter = @('X','W','V','U','T','S','R') | Where-Object { -not (Get-Volume -DriveLetter $_ -ErrorAction SilentlyContinue) } | Select-Object -First 1
        if (-not $efiLetter) { throw 'No temporary EFI drive letter is available' }
        Format-Volume -Partition $efiPartition -FileSystem FAT32 -NewFileSystemLabel 'System' -Confirm:$false | Out-Null
        Format-Volume -Partition $windowsPartition -FileSystem NTFS -NewFileSystemLabel 'Windows' -Confirm:$false | Out-Null
        Set-Partition -DiskNumber $diskNumber -PartitionNumber $efiPartition.PartitionNumber -NewDriveLetter $efiLetter -ErrorAction Stop
        $efi = ([string]$efiLetter + ':'); $windows = Join-Path ([IO.Path]::GetTempPath()) ('virtops-win-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $windows | Out-Null
        Add-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $windowsPartition.PartitionNumber -AccessPath ($windows + '\')
        $plan = Get-OfflineInstallPlan $InstallImage $Index $windows $efi
        Invoke-Checked 'dism.exe' $plan.dism ((Remaining-Minutes $Deadline)*60) | Out-Null
        $panther = Join-Path $windows 'Windows\Panther'; New-Item -ItemType Directory -Path $panther -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $panther 'Unattend.xml'), $Unattend, [Text.UTF8Encoding]::new($false))
        Invoke-Checked 'bcdboot.exe' $plan.bcdboot 180 | Out-Null
    } finally {
        try {
            if ($diskNumber -ne $null) {
                if ($efi -and $efiPartition) { Remove-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $efiPartition.PartitionNumber -AccessPath ($efi + '\') -ErrorAction Stop }
                if ($windows -and $windowsPartition) { Remove-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $windowsPartition.PartitionNumber -AccessPath ($windows + '\') -ErrorAction Stop }
            }
        } finally {if ($mounted) { Dismount-VHD -Path $Disk -ErrorAction Stop }}
        if ($windows -and (Test-Path -LiteralPath $windows)) { [IO.Directory]::Delete($windows,$false) }
    }
}

function New-RandomBootstrapPassword {
    $bytes = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes) + '!aA9')
}

function Template-SanitizeScript([string]$Account) {
    if($Account -cne 'VirtOpsBootstrap'){throw 'Unknown builder account'}
    return @'
$ErrorActionPreference='Stop'
Start-Sleep -Seconds 15
$account='VirtOpsBootstrap'
$identity=$env:COMPUTERNAME+'\'+$account
$sessionIds=@(Get-Process -IncludeUserName -ErrorAction SilentlyContinue | Where-Object {$_.UserName -eq $identity -and $_.SessionId -gt 0} | Select-Object -ExpandProperty SessionId -Unique)
foreach($sessionId in $sessionIds){& logoff.exe $sessionId;if($LASTEXITCODE -ne 0){throw 'Bootstrap logoff failed'}}
$deadline=[DateTime]::UtcNow.AddSeconds(90)
do {
    $profile=Get-CimInstance Win32_UserProfile|Where-Object {$_.LocalPath -eq 'C:\Users\VirtOpsBootstrap'}
    if(-not $profile -or -not $profile.Loaded){break}
    Start-Sleep -Seconds 2
}while([DateTime]::UtcNow-lt$deadline)
if($profile -and $profile.Loaded){throw 'Bootstrap profile is still loaded'}
Disable-LocalUser -Name $account
if($profile){$profile|Remove-CimInstance}
Remove-LocalUser -Name $account
if(Get-LocalUser -Name $account -ErrorAction SilentlyContinue){throw 'Bootstrap account remains'}
if(Test-Path -LiteralPath 'C:\Users\VirtOpsBootstrap'){throw 'Bootstrap profile remains'}
$winlogon='HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
foreach($property in @('AutoAdminLogon','AutoLogonCount','DefaultUserName','DefaultPassword','DefaultDomainName')) {
    if((Get-ItemProperty -LiteralPath $winlogon).PSObject.Properties.Name -contains $property){Remove-ItemProperty -LiteralPath $winlogon -Name $property}
}
foreach($path in @('C:\Windows\Panther\Unattend.xml','C:\Windows\Panther\Unattend\Unattend.xml','C:\Windows\System32\Sysprep\Unattend.xml')){
    if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Force}
}
Unregister-ScheduledTask -TaskName VirtOpsTemplateSeal -Confirm:$false
Remove-Item -LiteralPath $PSCommandPath -Force
[IO.File]::WriteAllText('C:\Windows\Temp\VirtOps-Template-Sanitized.marker','account_and_profile_removed')
$process=Start-Process -FilePath 'C:\Windows\System32\Sysprep\Sysprep.exe' -ArgumentList '/generalize','/oobe','/shutdown','/quiet' -PassThru -Wait -WindowStyle Hidden
if($process.ExitCode-ne0){throw 'Sysprep failed'}
'@
}
function Invoke-BuilderSeal($Vm, [string]$User, [PSCredential]$Credential, [int]$TimeoutMinutes = 45) {
    $deadline = [DateTime]::UtcNow.AddMinutes($TimeoutMinutes); $session = $null
    try {
        while ([DateTime]::UtcNow -lt $deadline -and -not $session) {
            try { $session = New-PSSession -VMId $Vm.Id -Credential $Credential -ErrorAction Stop } catch { Start-Sleep -Seconds 5 }
        }
        if (-not $session) { throw 'PowerShell Direct bootstrap did not become available within 45 minutes' }
        $body=Template-SanitizeScript $User
        Invoke-Command -Session $session -ScriptBlock {
            param($body)
            $ErrorActionPreference='Stop'
            $path='C:\Windows\Temp\VirtOps-Template-Sanitize.ps1'
            [IO.File]::WriteAllText($path,$body,[Text.UTF8Encoding]::new($false))
            $action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\Windows\Temp\VirtOps-Template-Sanitize.ps1'
            $principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
            Register-ScheduledTask -TaskName VirtOpsTemplateSeal -Action $action -Principal $principal -Force|Out-Null
            Start-ScheduledTask -TaskName VirtOpsTemplateSeal
        } -ArgumentList $body | Out-Null
    } finally { if ($session) { Remove-PSSession $session -ErrorAction SilentlyContinue } }
}

function Wait-VMOff([Guid]$VmId, [int]$TimeoutMinutes = 45) {
    $deadline = [DateTime]::UtcNow.AddMinutes($TimeoutMinutes)
    while ([DateTime]::UtcNow -lt $deadline) {
        $vm = Get-VM -Id $VmId -ErrorAction Stop
        if ([string]$vm.State -eq 'Off') { return }
        Start-Sleep -Seconds 5
    }
    throw 'Builder did not shut down within the 45 minute limit'
}

function Remaining-Minutes([DateTime]$Deadline) {
    $seconds = ($Deadline - [DateTime]::UtcNow).TotalSeconds
    if ($seconds -le 0) { throw 'ISO template build exceeded the 45 minute overall limit' }
    return [Math]::Max(1, [int][Math]::Ceiling($seconds / 60))
}

function Verify-GeneralizedDisk([string]$Disk) {
    $mount = $null; $access = $null; $partition=@()
    try {
        $mount = Mount-VHD -Path $Disk -ReadOnly -Passthru
        $partition = @(Get-Partition -DiskNumber $mount.DiskNumber | Where-Object Size -gt 10GB | Select-Object -First 1)
        if ($partition.Count -ne 1) { throw 'Generalized disk has no Windows partition' }
        $access = Join-Path ([IO.Path]::GetTempPath()) ('virtops-verify-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $access | Out-Null
        Add-PartitionAccessPath -DiskNumber $mount.DiskNumber -PartitionNumber $partition.PartitionNumber -AccessPath ($access + '\')
        $statePath = Join-Path $access 'Windows\Setup\State\State.ini'
        if (-not (Test-Path -LiteralPath $statePath) -or (Get-Content -LiteralPath $statePath -Raw) -notmatch 'IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE') { throw 'Installed Windows image is not generalized' }
        if ((Get-Content -LiteralPath (Join-Path $access 'Windows\Temp\VirtOps-Template-Sanitized.marker') -Raw) -ne 'account_and_profile_removed') { throw 'Guest sanitation proof is missing' }
        foreach($relative in @('Users\VirtOpsBootstrap','Windows\Panther\Unattend.xml','Windows\Temp\VirtOps-Template-Sanitize.ps1','Windows\System32\Tasks\VirtOpsTemplateSeal')) {
            if(Test-Path -LiteralPath (Join-Path $access $relative)){throw ('Builder artifact remains: '+$relative)}
        }
    } finally {
        try {if ($mount -and $access -and $partition.Count -eq 1) { Remove-PartitionAccessPath -DiskNumber $mount.DiskNumber -PartitionNumber $partition[0].PartitionNumber -AccessPath ($access + '\') -ErrorAction Stop }}
        finally {if ($mount) { Dismount-VHD -Path $Disk -ErrorAction Stop }}
        if ($access -and (Test-Path -LiteralPath $access)) { [IO.Directory]::Delete($access,$false) }
    }
}

function Publish-SealedImage([string]$Store, [string]$Disk, $Parameters, $Record) {
    $hash = (Get-FileHash -LiteralPath $Disk -Algorithm SHA256).Hash.ToLowerInvariant(); $directory = Image-Directory $Store; $id = $hash.Substring(0,24); $target = Store-Child $directory ($id + '.vhdx'); $manifestPath = Store-Child $directory ($id + '.json')
    if (Test-Path -LiteralPath $target) { throw 'A base image with this hash already exists' }
    Copy-Item -LiteralPath $Disk -Destination $target
    try {
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) { throw 'Published image hash differs' }
        (Get-Item -LiteralPath $target).IsReadOnly = $true
        $manifest = [pscustomobject]@{ id = $id; name = [string](Value $Parameters 'name' $Record.name); path = $target; sha256 = $hash; source_iso = $Record.iso_path; image_index = $Record.image_index; image_name = $Record.image_name; template_state = 'generalized'; virtualBytes = (Get-VHD -Path $target).Size; published_utc = [DateTime]::UtcNow.ToString('o') }
        Save-Json $manifestPath $manifest; return $manifest
    } catch { if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }; throw }
}

function Remove-OwnedBuilder($Record) {
    if (-not $Record.store -or $Record.id -notmatch '^[0-9a-fA-F-]{36}$') { throw 'Builder journal has no valid store identity' }
    $expectedRoot = [IO.Path]::GetFullPath((Store-Child (Store-Child ([string]$Record.store) 'builders') ([string]$Record.id))).TrimEnd('\')
    $actualRoot = [IO.Path]::GetFullPath([string]$Record.root).TrimEnd('\')
    if ($actualRoot -ne $expectedRoot) { throw 'Builder root identity mismatch during cleanup' }
    $expectedDisk = [IO.Path]::GetFullPath((Store-Child $expectedRoot 'builder.vhdx'))
    if ([IO.Path]::GetFullPath([string]$Record.disk) -ne $expectedDisk) { throw 'Builder disk identity mismatch during cleanup' }
    $vm = $null
    if ($Record.vm_id) { $vm = Get-VM -Id ([Guid]$Record.vm_id) -ErrorAction SilentlyContinue }
    if ($vm) {
        if ($vm.Name -ne $Record.vm_name) { throw 'Builder identity mismatch during cleanup' }
        $drives=@(Get-VMHardDiskDrive -VM $vm)
        if($drives.Count-gt1 -or ($drives.Count-eq1 -and $drives[0].Path-ne$expectedDisk)){throw 'Builder disk attachment differs'}
        if ([string]$vm.State -ne 'Off') { Stop-VM -VM $vm -TurnOff -Force -Confirm:$false }
        Remove-VM -VM $vm -Force
    }
    if (Test-Path -LiteralPath $expectedDisk) {
        $vhd = Get-VHD -Path $expectedDisk -ErrorAction SilentlyContinue
        if ($vhd -and [string]$vhd.Attached -eq 'True') { Dismount-VHD -Path $expectedDisk -ErrorAction Stop }
    }
    if (Test-Path -LiteralPath $actualRoot) {
        if(@(Get-ChildItem -LiteralPath $actualRoot -Recurse -Force|Where-Object {$_.Attributes-band[IO.FileAttributes]::ReparsePoint}).Count){throw 'Builder contains a reparse point; cleanup refused'}
        Remove-Item -LiteralPath $actualRoot -Recurse -Force -ErrorAction Stop
    }
}

function Build-BaseImage([string]$Store, $Parameters) {
    Assert-Engine
    $iso = Iso-Absolute ([string](Value $Parameters 'iso_path' '')); $name = [string](Value $Parameters 'name' 'windows-base')
    $cpu = [int](Value $Parameters 'vcpus' 2); $memory = [int](Value $Parameters 'memory_gib' 4); $diskGiB = [int](Value $Parameters 'disk_gib' 80)
    if ($name -notmatch '^[a-z][a-z0-9-]{1,23}$') { throw 'Base image name is invalid' }
    $env = Environment-Info; if ($cpu -lt 2 -or $memory -lt 4 -or $cpu -gt $env.logicalCpus -or [long]$memory * 1GB -gt $env.availableMemoryBytes - 2GB) { throw 'Builder resources exceed host budget' }
    $builderId = [guid]::NewGuid().ToString(); $root = Store-Child (Store-Child $Store 'builders') $builderId; New-Item -ItemType Directory -Path $root -Force | Out-Null; Protect-Directory $root
    $record = [ordered]@{ schema_version = 2; id = $builderId; store = $Store; name = $name; stage = 'allocating'; iso_path = $iso; root = $root; vm_name = 'VirtOps-Builder-' + $builderId.Substring(0,8); computer_name = 'VOBase-' + $builderId.Substring(0,8); vm_id = ''; disk = Join-Path $root 'builder.vhdx'; created_utc = [DateTime]::UtcNow.ToString('o') }; $recordPath = Join-Path $root 'build.json'; Save-Json $recordPath $record
    $drive = $null; $credential = $null; $deadline = [DateTime]::UtcNow.AddMinutes(45)
    try {
        $drive = Get-IsoDrive $iso; $catalog = Get-InstallImages $drive; $selected = Select-WindowsImage $catalog.images ([int](Value $Parameters 'image_index' 0)); $record.image_index = [int]$selected.ImageIndex; $record.image_name = [string]$selected.ImageName; $record.stage = 'image_selected'; Save-Json $recordPath $record
        New-BuilderVhd $record.disk $diskGiB; $password = New-RandomBootstrapPassword; $record.bootstrap_user = 'VirtOpsBootstrap'; $record.stage = 'disk_ready'; Save-Json $recordPath $record
        $language = @($selected.Languages | Select-Object -First 1)[0]; if ([string]::IsNullOrWhiteSpace([string]$language)) { $language = 'en-US' }
        New-OfflineWindowsDisk $record.disk $catalog.path ([int]$selected.ImageIndex) (New-TemplateUnattend $record.computer_name $record.bootstrap_user $password ([int]$selected.ImageIndex) ([string]$language)) $deadline
        $credential = [PSCredential]::new($record.bootstrap_user, (ConvertTo-SecureString $password -AsPlainText -Force)); $password = $null; $record.stage = 'installed'; Save-Json $recordPath $record
        $vm = New-VM -Name $record.vm_name -Path $root -Generation 2 -MemoryStartupBytes ([long]$memory * 1GB) -NoVHD -ErrorAction Stop; $record.vm_id = $vm.Id.ToString(); Save-Json $recordPath $record; Add-VMHardDiskDrive -VM $vm -Path $record.disk -ControllerType SCSI -ControllerNumber 0 -ControllerLocation 0 | Out-Null
        Set-VMProcessor -VM $vm -Count $cpu; Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows; Set-VMKeyProtector -VM $vm -NewLocalKeyProtector; Enable-VMTPM -VM $vm
        @(Get-VMNetworkAdapter -VM $vm -ErrorAction Stop) | Remove-VMNetworkAdapter -Confirm:$false -ErrorAction Stop
        if (@(Get-VMNetworkAdapter -VM $vm -ErrorAction Stop).Count -ne 0) { throw 'Builder VM network adapter removal was not verified' }
        $record.stage = 'vm_defined'; Save-Json $recordPath $record; Start-VM -VM $vm | Out-Null; $record.stage = 'booting'; Save-Json $recordPath $record
        Invoke-BuilderSeal $vm $record.bootstrap_user $credential (Remaining-Minutes $deadline); $record.stage = 'sealing'; Save-Json $recordPath $record; Wait-VMOff $vm.Id (Remaining-Minutes $deadline); Verify-GeneralizedDisk $record.disk; $record.stage = 'generalized'; Save-Json $recordPath $record
        $manifest = Publish-SealedImage $Store $record.disk $Parameters $record
        try { Remove-OwnedBuilder $record } catch { $record.stage = 'cleanup_required'; $record.cleanup_error = $_.Exception.Message; Save-Json $recordPath $record; return [pscustomobject]@{ status = 'imported'; image = $manifest; cleanup_warning = $record.cleanup_error } }
        return [pscustomobject]@{ status = 'imported'; image = $manifest }
    } catch {
        $caught = $_; $record.stage = 'cleaning_failed_build'; $record.error = $caught.Exception.Message; $cleanupError = $null
        try { Remove-OwnedBuilder $record } catch { $cleanupError = $_ }
        if ($cleanupError) { $record.stage = 'cleanup_required'; $record.cleanup_error = $cleanupError.Exception.Message; Save-Json $recordPath $record; throw $cleanupError }
        throw $caught
    }
    finally { if ($drive) { Dismount-Iso $iso } }
}
