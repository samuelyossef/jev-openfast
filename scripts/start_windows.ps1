$ErrorActionPreference = "Stop"

$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$profile = Join-Path $repo "artifacts\chrome-profile"
$activePort = Join-Path $profile "DevToolsActivePort"
$chrome = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $chrome) { throw "Google Chrome is required for the local Jev demo." }
if (-not (Test-Path -LiteralPath (Join-Path $repo ".env"))) {
    throw "Copy .env.example to .env before starting Jev."
}

New-Item -ItemType Directory -Path $profile -Force | Out-Null

function Get-CdpPort {
    if (-not (Test-Path -LiteralPath $activePort)) { return $null }
    try {
        $port = [int](Get-Content -LiteralPath $activePort -TotalCount 1)
        $version = Invoke-RestMethod -Uri "http://127.0.0.1:$port/json/version" -TimeoutSec 2
        if ($version.webSocketDebuggerUrl) { return $port }
    } catch { }
    return $null
}

function Get-ManagedChrome {
    @(Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" | Where-Object {
        $_.CommandLine -like "*--user-data-dir=*" -and
        $_.CommandLine -like "*$profile*" -and
        $_.CommandLine -notlike "*--type=*"
    })
}

$managedChrome = Get-ManagedChrome
$port = if ($managedChrome) { Get-CdpPort } else { $null }
for ($attempt = 0; $attempt -lt 20 -and $managedChrome -and -not $port; $attempt++) {
    Start-Sleep -Milliseconds 250
    $managedChrome = Get-ManagedChrome
    $port = if ($managedChrome) { Get-CdpPort } else { $null }
}
if ($managedChrome -and -not $port) { throw "The Jev Chrome profile is open but its CDP endpoint is unavailable." }

if (-not $port) {
    if (Test-Path -LiteralPath $activePort) { Remove-Item -LiteralPath $activePort -Force }
    # Created through WMI so Chrome is not part of this launcher's job: stopping or restarting the launcher
    # (or the terminal that ran it) must not kill the browser that a running Jev server still points to.
    $arguments = @(
        "--headless=new",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        "--user-data-dir=`"$profile`"",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank"
    ) -join " "
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = "`"$chrome`" $arguments"
    }
    if ($created.ReturnValue -ne 0) { throw "Could not start the isolated Chrome (code $($created.ReturnValue))." }
    for ($attempt = 0; $attempt -lt 40 -and -not $port; $attempt++) {
        Start-Sleep -Milliseconds 250
        $port = Get-CdpPort
    }
    if (-not $port) { throw "The isolated Chrome did not expose a CDP endpoint." }
}

$env:BU_CDP_URL = "http://127.0.0.1:$port"
$env:BU_NAME = "jev-demo"
$env:JEV_HEADLESS_BROWSER = "1"
Remove-Item Env:OPENROUTER_API_KEY -ErrorAction SilentlyContinue
Push-Location $repo
try {
    & uv run --env-file .env --locked jev
    if ($LASTEXITCODE -ne 0) { throw "Jev exited with code $LASTEXITCODE." }
} finally {
    Pop-Location
}
