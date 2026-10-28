# Build ESP-IDF fixture projects prepared by prepare.py.
#   .\scripts\fixtures\build_idf.ps1 -WorkDir D:\work -Version v5.5.5 -Samples esp32_hello,esp32s3_hello
# -Version picks the EIM activation profile C:\Espressif\tools\Microsoft.<Version>.PowerShell_profile.ps1
# (or pass -Profile). The flash command is captured with a port that does not exist,
# so esptool stops before it opens any board.
param(
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory)][string[]]$Samples,
    [string]$Profile = "C:\Espressif\tools\Microsoft.$Version.PowerShell_profile.ps1"
)
$ErrorActionPreference = "Continue"
. $Profile *> $null
foreach ($s in $Samples) {
    $dir = Join-Path (Join-Path $WorkDir "idf") $s
    $target = ($s -split "_")[0]
    Push-Location $dir
    Write-Output "=== $s ($target) on $Version"
    idf.py set-target $target *> set-target.log
    idf.py build *> build.log
    Write-Output "build exit: $LASTEXITCODE"
    idf.py -p COM99 flash *> flash_cmd.log
    if ($Version -like "v6*") { idf.py merge-bin *> merge.log; Write-Output "merge exit: $LASTEXITCODE" }
    Pop-Location
}
