$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script = Join-Path (Split-Path -Parent $PSScriptRoot) 'desktop/engine/local_iso.ps1'
$errors = $null; $tokens = $null
[Management.Automation.Language.Parser]::ParseFile($script, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count) { throw ('local_iso.ps1 parse failed: ' + ($errors | ForEach-Object Message -join '; ')) }
. $script

$xml = New-TemplateUnattend 'Base-01' 'VirtOpsBootstrap' 'p<&"safePassword!Aa1' 9 'zh-CN'
$xmlDocument = New-Object System.Xml.XmlDocument; $xmlDocument.LoadXml($xml)
if ($xml -notmatch '&lt;|&amp;|&quot;') { throw 'Unattend values were not XML escaped' }
if ($xml -notmatch 'DiskID>0</DiskID>' -or $xml -notmatch 'InstallTo') { throw 'Unattend must target disk 0' }
if ($xml -match 'FirstLogonCommands|Sysprep.exe') { throw 'Unattend must not run Sysprep as the bootstrap user' }
if ($xml -notmatch '<Value>9</Value>' -or $xml -notmatch '<UILanguage>zh-CN</UILanguage>') { throw 'Unattend did not preserve the selected image index/language' }
$moduleSource = Get-Content -LiteralPath $script -Raw
if ($moduleSource -notmatch 'Sysprep.exe.*generalize.*shutdown') { throw 'SYSTEM seal path is missing' }
if (([regex]::Matches($moduleSource, '(?m)^function Build-BaseImage')).Count -ne 1) { throw 'Build-BaseImage must have one implementation' }

$images = @([pscustomobject]@{ImageIndex=1;ImageName='Windows 11 Pro';Architecture=9;Languages=@('zh-CN')}, [pscustomobject]@{ImageIndex=2;ImageName='Windows 11 Home';Architecture='amd64';Languages=@('zh-CN')})
$selected = Select-WindowsImage $images 0
if ($selected.ImageIndex -ne 1) { throw 'Pro image was not selected' }
$failed = $false
try { Select-WindowsImage @([pscustomobject]@{ImageIndex=1;ImageName='Windows 11 Home';Architecture='amd64'}) 0 } catch { $failed = $true }
if (-not $failed) { throw 'Home image must be rejected' }
$failed = $false
try { Select-WindowsImage @($images[0]) 3 } catch { $failed = $true }
if (-not $failed) { throw 'Unknown image index must be rejected' }
$plan = Get-OfflineInstallPlan 'D:\sources\install.wim' 9 'C:\virtops-win' 'X:'
if ($plan.target_disk -ne 0 -or $plan.windows_partition -ne 3 -or $plan.dism -notcontains '/Index:9' -or $plan.bcdboot -notcontains '/f' -or $plan.bcdboot -notcontains '/s' -or $plan.bcdboot -notcontains 'X:') { throw 'Offline DISM/UEFI plan is not bound to disk 0' }
if ($moduleSource -match 'RandomNumberGenerator::GetBytes\(18\)') { throw 'Module must remain Windows PowerShell 5.1 compatible' }

if ((Test-LocalIsoModule).status -ne 'ok') { throw 'module self-test failed' }
$sanitize=Template-SanitizeScript 'VirtOpsBootstrap'
[Management.Automation.Language.Parser]::ParseInput($sanitize,[ref]$tokens,[ref]$errors)|Out-Null
if($errors.Count){throw 'Sanitation script is not valid PowerShell'}
if($sanitize.IndexOf('Remove-LocalUser')-gt$sanitize.IndexOf('Sanitized.marker') -or $sanitize.IndexOf('Unregister-ScheduledTask')-gt$sanitize.IndexOf('Sanitized.marker')){throw 'Sanitation marker precedes successful cleanup'}
$failed=$false;try{Get-OfflineInstallPlan 'D:\sources\install.wim' 9 'C:\virtops-win' 'C:\efi'|Out-Null}catch{$failed=$true}
if(-not $failed){throw 'BCDBoot must reject directory mount targets'}
# Exercise both redirected pipes using a real, harmless child process.
$childSource="[Console]::Out.Write(('x'*131072));[Console]::Error.Write(('y'*131072))"
$encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childSource))
$output=Invoke-Checked 'powershell.exe' @('-NoProfile','-NonInteractive','-EncodedCommand',$encoded) 20
if($output.Length-ne131072){throw 'Process pipe drain lost output'}
# Existing ISO mounts must survive inspection. These replace only cmdlets,
# leaving the actual ownership functions under test.
$script:attached=$true;$script:mountCount=0;$script:unmountCount=0
function Get-DiskImage {param($ImagePath) [pscustomobject]@{Attached=$script:attached}}
function Mount-DiskImage {param($ImagePath,$Access,[switch]$PassThru) $script:mountCount++;$script:attached=$true}
function Dismount-DiskImage {param($ImagePath) $script:unmountCount++;$script:attached=$false}
function Get-Volume {param([Parameter(ValueFromPipeline=$true)]$Image) process{[pscustomobject]@{DriveLetter='Z'}}}
Get-IsoDrive 'C:\owned-test.iso'|Out-Null;Dismount-Iso 'C:\owned-test.iso'
if($script:mountCount-ne0 -or $script:unmountCount-ne0){throw 'Inspection changed an existing ISO mount'}
$script:attached=$false
Get-IsoDrive 'C:\owned-test.iso'|Out-Null;Dismount-Iso 'C:\owned-test.iso'
if($script:mountCount-ne1 -or $script:unmountCount-ne1){throw 'Inspection did not release its own ISO mount'}
Write-Output 'local ISO module tests: PASS (selection, XML, EFI scope, sanitation, child pipes, mount ownership)'
