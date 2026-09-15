<#
.SYNOPSIS
    Install the Windows integration, take it away again, and refuse if anything
    of ours is left behind or anything of anybody else's is not.

.DESCRIPTION
    `check-imports.ps1` in each repository checks the artefact the Store
    distributes. This checks the other half: that `install.ps1` and
    `uninstall.ps1` themselves go on and come off cleanly, which nothing else
    reaches. It registers the real ProgIDs under HKCU and takes them away, so
    it is for a build agent or a machine where losing an existing association
    does not matter.

    Two states it guards against, and both have been real.

    A `UserChoice` left behind naming a ProgID that has just been deleted kills
    the extension outright: Windows treats such a choice as no association at
    all rather than falling back to the machine-wide one. Explorer writes a
    *Deny SetValue* rule on that key so no application can quietly take an
    extension over, and both `DeleteSubKeyTree` and `reg delete` open the key
    for writing before deleting it, so both fail against the rule — `reg`
    saying *Access is denied* and .NET reading the same failure as the key
    being missing and returning quietly. So the key is planted here the way
    Explorer writes it, deny rule and all: a check that plants an ordinary key
    passes against the defect it was written for, which is worse than no check.

    And an uninstall that removes a whole tree takes somebody else's offer with
    it. `OpenWithProgids` belongs to the extension and to every application
    that has ever offered to open one, so a neighbour is planted and has to
    survive.

.PARAMETER Associations
    JSON: an array of { extension, progId, contentType, exe }. One entry per
    file type the installer registers.

.PARAMETER OwnsExtension
    The installer writes the extension's default value, rather than only adding
    itself to `OpenWithProgids`. True where the format is this product's own
    and nobody else claims it; false for a shared extension such as `.toml` or
    `.odt`, where taking the default would be taking it from somebody.

.PARAMETER Neighbour
    The ProgID planted to stand for another application's registration and
    choice. Nothing should ever remove it.

.NOTES
    Author: David M. Anderson
    Built with AI assistance (Claude, Anthropic)
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Associations,
    [switch] $OwnsExtension,
    [string] $Neighbour = 'SomeoneElse.Test.Reader',
    [string] $InstallArgs = '-NoBinary'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$apps = $Associations | ConvertFrom-Json
$classes = 'Software\Classes'
$failures = @()

function Expect([bool] $ok, [string] $what) {
    if ($ok) { Write-Host "  ok    $what" }
    else { Write-Host "  FAIL  $what" -ForegroundColor Red; $script:failures += $what }
}

function Get-Key([string] $Path) {
    [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($Path, $false)
}

function Test-Key([string] $Path) {
    $k = Get-Key $Path
    if (-not $k) { return $false }
    $k.Close()
    return $true
}

function Get-Value([string] $Path, [string] $Name) {
    $k = Get-Key $Path
    if (-not $k) { return $null }
    try { return $k.GetValue($Name, $null) } finally { $k.Close() }
}

function Exts([string] $extension) {
    "Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$extension"
}

# Explorer's own key, as far as it can be reproduced: the ProgId value and the
# deny rule that stops it being opened for writing.
function New-ProtectedUserChoice([string] $extension, [string] $chosen) {
    # Taken away first, because the deny rule on a key left by a failed run
    # makes CreateSubKey throw *Access to the registry key is denied*, and that
    # exception arriving in the middle of the checks hides which one failed.
    Remove-UserChoice $extension
    $k = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("$(Exts $extension)\UserChoice")
    $k.SetValue('ProgId', $chosen)
    $acl = $k.GetAccessControl()
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl.AddAccessRule((New-Object System.Security.AccessControl.RegistryAccessRule(
        $me, 'SetValue', 'None', 'None', 'Deny')))
    $k.SetAccessControl($acl)
    $k.Close()
}

function Remove-UserChoice([string] $extension) {
    $parent = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey((Exts $extension), $true)
    if (-not $parent) { return }
    try { $parent.DeleteSubKey('UserChoice', $false) } finally { $parent.Close() }
}

$here = $PSScriptRoot
$install = Join-Path $here '..\..\..\..\packaging\windows\install.ps1'
if (-not (Test-Path -LiteralPath $install)) {
    $install = 'packaging\windows\install.ps1'
}
$uninstall = Join-Path (Split-Path -Parent $install) 'uninstall.ps1'

# A neighbour on every extension, and whatever the defaults were. Both have to
# come back unchanged.
$before = @{}
foreach ($app in $apps) {
    $k = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("$classes\$($app.extension)\OpenWithProgids")
    try { $k.SetValue($Neighbour, '', [Microsoft.Win32.RegistryValueKind]::String) } finally { $k.Close() }
    if (-not $OwnsExtension) { $before[$app.extension] = Get-Value "$classes\$($app.extension)" '' }
}

try {
    Write-Host "check-install: install.ps1 $InstallArgs"
    # Split rather than splatted: splatting an array binds its elements
    # positionally, so @('-NoBinary') would bind the literal string to the
    # first positional parameter and leave the switch false. odox found that.
    $installArgv = $InstallArgs.Split(' ', [StringSplitOptions]::RemoveEmptyEntries)
    & powershell -ExecutionPolicy Bypass -File $install @installArgv | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "install.ps1 exited $LASTEXITCODE" }

    foreach ($app in $apps) {
        Expect (Test-Key "$classes\$($app.progId)") "$($app.progId) is written"
        Expect ($null -ne (Get-Value "$classes\$($app.extension)\OpenWithProgids" $app.progId)) `
            "$($app.extension) offers $($app.progId)"
        if ($app.PSObject.Properties.Name -contains 'contentType' -and $app.contentType) {
            Expect (Test-Key "$classes\MIME\Database\Content Type\$($app.contentType)") `
                "$($app.contentType) is written"
        }
        if ($OwnsExtension) {
            Expect ((Get-Value "$classes\$($app.extension)" '') -eq $app.progId) `
                "$($app.extension) names $($app.progId) by default"
        } else {
            Expect ((Get-Value "$classes\$($app.extension)" '') -eq $before[$app.extension]) `
                "$($app.extension)'s default is left as it was"
        }
    }

    # A person who chose "always open with" before uninstalling, written the way
    # Explorer writes it.
    foreach ($app in $apps) { New-ProtectedUserChoice $app.extension $app.progId }
    foreach ($app in $apps) {
        Expect ((Get-Value "$(Exts $app.extension)\UserChoice" 'ProgId') -eq $app.progId) `
            "a UserChoice naming $($app.progId) is in place"
    }

    Write-Host 'check-install: uninstall.ps1'
    & powershell -ExecutionPolicy Bypass -File $uninstall | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "uninstall.ps1 exited $LASTEXITCODE" }

    foreach ($app in $apps) {
        Expect (-not (Test-Key "$classes\$($app.progId)")) "$($app.progId) is gone"
        Expect ($null -eq (Get-Value "$classes\$($app.extension)\OpenWithProgids" $app.progId)) `
            "$($app.extension) no longer offers $($app.progId)"
        Expect (-not (Test-Key "$(Exts $app.extension)\UserChoice")) `
            "the UserChoice naming $($app.progId) is gone"
        Expect ($null -ne (Get-Value "$classes\$($app.extension)\OpenWithProgids" $Neighbour)) `
            "$($app.extension) still offers $Neighbour"
        if (-not $OwnsExtension) {
            Expect ((Get-Value "$classes\$($app.extension)" '') -eq $before[$app.extension]) `
                "$($app.extension)'s default came back as it was"
        }
    }

    # The other half of the rule: somebody else's choice is theirs to keep.
    foreach ($app in $apps) { New-ProtectedUserChoice $app.extension $Neighbour }
    & powershell -ExecutionPolicy Bypass -File $uninstall | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "uninstall.ps1 exited $LASTEXITCODE on the second run" }
    foreach ($app in $apps) {
        Expect ((Get-Value "$(Exts $app.extension)\UserChoice" 'ProgId') -eq $Neighbour) `
            "a UserChoice naming $Neighbour is left where it is"
    }
} finally {
    foreach ($app in $apps) {
        Remove-UserChoice $app.extension
        $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("$classes\$($app.extension)\OpenWithProgids", $true)
        if ($k) { try { $k.DeleteValue($Neighbour, $false) } finally { $k.Close() } }
    }
}

if ($failures.Count -gt 0) {
    Write-Host "check-install: $($failures.Count) of the checks above failed" -ForegroundColor Red
    exit 1
}
Write-Host 'check-install: the integration goes on and comes off cleanly'
