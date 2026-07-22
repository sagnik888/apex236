param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$rootDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $rootDir ".venv\Scripts\python.exe"
$backendEntry = Join-Path $rootDir "artifacts\api-server\python_scanner\main.py"
$frontendVite = Join-Path $rootDir "artifacts\trading-dashboard\node_modules\vite\bin\vite.js"
$logDir = Join-Path $rootDir "logs"

function Get-ProcessInfo([int]$ProcessId) {
    Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Stop-ApexListener([int]$Port, [string]$ExpectedCommandPattern) {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $owner = Get-ProcessInfo $listener.OwningProcess
        if (-not $owner) {
            continue
        }

        if ($owner.CommandLine -notmatch $ExpectedCommandPattern) {
            throw "Port $Port is occupied by unrelated process $($owner.ProcessId) ($($owner.Name)). Stop it or change the Apex port."
        }

        # Include consecutive matching ancestors. This is essential for old
        # Uvicorn reload supervisors, which otherwise respawn their child.
        $processIds = [System.Collections.Generic.List[int]]::new()

        # Include the complete child tree as well. The optimized scanner uses
        # worker processes; stopping only the port owner would orphan them.
        $descendants = [System.Collections.Generic.List[int]]::new()
        $queue = [System.Collections.Generic.Queue[int]]::new()
        $queue.Enqueue([int]$owner.ProcessId)
        while ($queue.Count -gt 0) {
            $parentId = $queue.Dequeue()
            $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$parentId" -ErrorAction SilentlyContinue)
            foreach ($child in $children) {
                $childId = [int]$child.ProcessId
                if (-not $descendants.Contains($childId)) {
                    $descendants.Add($childId)
                    $queue.Enqueue($childId)
                }
            }
        }

        for ($index = $descendants.Count - 1; $index -ge 0; $index--) {
            Stop-Process -Id $descendants[$index] -Force -ErrorAction SilentlyContinue
        }

        $current = $owner
        while ($current -and $current.CommandLine -match $ExpectedCommandPattern) {
            if (-not $processIds.Contains([int]$current.ProcessId)) {
                $processIds.Add([int]$current.ProcessId)
            }
            $current = Get-ProcessInfo $current.ParentProcessId
        }

        foreach ($processId in $processIds) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }

    $deadline = (Get-Date).AddSeconds(10)
    do {
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    throw "Port $Port did not become available after stopping the previous Apex process."
}

function Wait-ForHttp([string]$Name, [string]$Url, $Process, [string]$ErrorLog) {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        if ($Process.HasExited) {
            $details = if (Test-Path $ErrorLog) { (Get-Content $ErrorLog -Tail 30) -join [Environment]::NewLine } else { "No error log was created." }
            throw "$Name exited during startup.`n$details"
        }

        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                Write-Host "[OK] $Name is ready at $Url"
                return
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)

    throw "$Name did not become healthy within 30 seconds. See $ErrorLog"
}

if (-not (Test-Path $pythonExe)) {
    throw "Python virtual environment not found at $pythonExe"
}
$pnpmCommand = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
if (-not $pnpmCommand) {
    throw "pnpm is not installed or is not available on PATH."
}
$pnpmExe = $pnpmCommand.Source

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

Write-Host "[1/5] Checking dashboard dependencies..."
if (-not (Test-Path $frontendVite)) {
    Write-Host "Dashboard dependencies are missing or refer to an old project location. Repairing them..."
    & $pnpmExe install --frozen-lockfile --force
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $frontendVite)) {
        throw "Could not repair the dashboard dependencies. Run 'pnpm install --frozen-lockfile --force' in $rootDir and try again."
    }
}

Write-Host "[2/5] Stopping previous Apex instances..."
Stop-ApexListener -Port 8080 -ExpectedCommandPattern "python_scanner[\\/]main\.py"
Stop-ApexListener -Port 5173 -ExpectedCommandPattern "(?:vite|trading-dashboard)"

Write-Host "[3/5] Starting Python backend..."
$backendOut = Join-Path $logDir "backend.log"
$backendErr = Join-Path $logDir "backend-error.log"
$backendProcess = Start-Process -FilePath $pythonExe `
    -ArgumentList @("`"$backendEntry`"") `
    -WorkingDirectory $rootDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $backendOut `
    -RedirectStandardError $backendErr `
    -PassThru
Wait-ForHttp -Name "Backend" -Url "http://127.0.0.1:8080/api/healthz" -Process $backendProcess -ErrorLog $backendErr

try {
    Write-Host "[4/5] Starting React dashboard..."
    $frontendOut = Join-Path $logDir "frontend.log"
    $frontendErr = Join-Path $logDir "frontend-error.log"
    $frontendProcess = Start-Process -FilePath $pnpmExe `
        -ArgumentList @("--filter", "@workspace/trading-dashboard", "run", "dev") `
        -WorkingDirectory $rootDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $frontendOut `
        -RedirectStandardError $frontendErr `
        -PassThru
    Wait-ForHttp -Name "Dashboard" -Url "http://127.0.0.1:5173" -Process $frontendProcess -ErrorLog $frontendErr
} catch {
    if ($backendProcess -and -not $backendProcess.HasExited) {
        Stop-Process -Id $backendProcess.Id -Force -ErrorAction SilentlyContinue
    }
    throw
}

Write-Host "[5/5] Apex is running. Backend PID: $($backendProcess.Id); frontend PID: $($frontendProcess.Id)"
if (-not $NoBrowser) {
    Start-Process "http://localhost:5173"
}
