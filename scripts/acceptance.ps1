[CmdletBinding()]
param(
  [string] $HarnessDir,
  [string] $TempDir,
  [string] $NodePath,
  [string] $PnpmPath,
  [string] $PythonPath,
  [int] $Port = 0,
  [ValidateRange(5, 300)]
  [int] $StartupTimeoutSeconds = 60,
  [switch] $SkipBuild,
  [switch] $PauseAfterServerReady,
  [switch] $KeepTemp
)

# This script deliberately uses a fresh DSH_HOME for every run. The only files
# it installs into that home are package-manager metadata and the locally packed
# plugin tarballs; no Harness source or installation-owned package is copied.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:RunDir = $null
$script:RunDirParent = $null
$script:ServerProcess = $null
$script:ServerStdoutTask = $null
$script:ServerStderrTask = $null
$script:OldDshHome = [Environment]::GetEnvironmentVariable('DSH_HOME', 'Process')
$script:OldPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
$script:OldPnpmConfigRegistry = [Environment]::GetEnvironmentVariable('PNPM_CONFIG_REGISTRY', 'Process')
$script:OldNpmConfigRegistry = [Environment]::GetEnvironmentVariable('npm_config_registry', 'Process')
$script:Succeeded = $false

function Write-Utf8NoBom {
  param(
    [Parameter(Mandatory)] [string] $Path,
    [Parameter(Mandatory)] [AllowEmptyString()] [string] $Content
  )

  $encoding = [System.Text.UTF8Encoding]::new($false)
  [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Resolve-RequiredDirectory {
  param(
    [Parameter(Mandatory)] [string] $Path,
    [Parameter(Mandatory)] [string] $Label
  )

  if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    throw "$Label does not exist or is not a directory: $Path"
  }
  return (Resolve-Path -LiteralPath $Path).Path
}

function Resolve-RequiredExecutable {
  param(
    [string] $Path,
    [Parameter(Mandatory)] [string] $CommandName,
    [Parameter(Mandatory)] [string] $Label
  )

  if (-not [string]::IsNullOrWhiteSpace($Path)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
      throw "$Label does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
  }

  $command = Get-Command $CommandName -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($null -eq $command -or [string]::IsNullOrWhiteSpace($command.Path)) {
    throw "$Label was not found on PATH; pass -$($Label -replace ' ', '') explicitly"
  }
  return (Resolve-Path -LiteralPath $command.Path).Path
}

function Invoke-Checked {
  param(
    [Parameter(Mandatory)] [string] $Executable,
    [Parameter(Mandatory)] [string[]] $Arguments,
    [Parameter(Mandatory)] [string] $WorkingDirectory,
    [Parameter(Mandatory)] [string] $Label
  )

  Write-Host "[acceptance] $Label"
  Push-Location -LiteralPath $WorkingDirectory
  try {
    & $Executable @Arguments
    $exitCode = $LASTEXITCODE
  } finally {
    Pop-Location
  }
  if ($exitCode -ne 0) {
    throw "$Label failed with exit code $exitCode"
  }
}

function Invoke-Captured {
  param(
    [Parameter(Mandatory)] [string] $Executable,
    [Parameter(Mandatory)] [string[]] $Arguments,
    [Parameter(Mandatory)] [string] $WorkingDirectory
  )

  $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $startInfo.FileName = $Executable
  $startInfo.WorkingDirectory = $WorkingDirectory
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  foreach ($argument in $Arguments) {
    [void] $startInfo.ArgumentList.Add([string] $argument)
  }

  $process = [System.Diagnostics.Process]::new()
  $process.StartInfo = $startInfo
  try {
    if (-not $process.Start()) {
      throw "could not start $Executable"
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    return [pscustomobject]@{
      ExitCode = $process.ExitCode
      Stdout = $stdoutTask.GetAwaiter().GetResult()
      Stderr = $stderrTask.GetAwaiter().GetResult()
    }
  } finally {
    $process.Dispose()
  }
}

function Write-JsonFile {
  param(
    [Parameter(Mandatory)] [string] $Path,
    [Parameter(Mandatory)] [object] $Value
  )

  Write-Utf8NoBom -Path $Path -Content (($Value | ConvertTo-Json -Depth 20) + [Environment]::NewLine)
}

function Quote-YamlScalar {
  param([Parameter(Mandatory)] [string] $Value)
  return "'$(($Value -replace "'", "''"))'"
}

function Get-FreeLoopbackPort {
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
  try {
    $listener.Start()
    return ([System.Net.IPEndPoint] $listener.LocalEndpoint).Port
  } finally {
    $listener.Stop()
  }
}

function Stop-ProcessTree {
  param([System.Diagnostics.Process] $Process)

  if ($null -eq $Process) {
    return
  }
  try {
    if (-not $Process.HasExited) {
      try {
        $Process.Kill($true)
      } catch {
        # Process.Kill(entireProcessTree) is available on supported .NET, but
        # taskkill is a safe Windows fallback for a PID created by this script.
        if ($IsWindows) {
          & taskkill.exe /PID $Process.Id /T /F *> $null
        } else {
          $Process.Kill()
        }
      }
      [void] $Process.WaitForExit(10000)
    }
  } catch {
    Write-Warning "Could not stop Harness process $($Process.Id): $($_.Exception.Message)"
  }
}

function Assert-Contains {
  param(
    [Parameter(Mandatory)] [string] $Text,
    [Parameter(Mandatory)] [string] $Needle,
    [Parameter(Mandatory)] [string] $EvidenceLabel
  )
  if (-not $Text.Contains($Needle, [System.StringComparison]::Ordinal)) {
    throw "$EvidenceLabel did not contain expected text: $Needle"
  }
}

function Invoke-HttpGet {
  param(
    [Parameter(Mandatory)] [string] $Uri,
    [int] $TimeoutSeconds = 5
  )

  try {
    return Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec $TimeoutSeconds -MaximumRedirection 0
  } catch {
    $response = $null
    if ($_.Exception.PSObject.Properties.Name -contains 'Response') {
      $response = $_.Exception.Response
    }
    if ($null -ne $response -and $response.PSObject.Properties.Name -contains 'StatusCode') {
      $statusCode = [int] $response.StatusCode
      throw "HTTP $statusCode for $Uri"
    }
    throw "HTTP request failed for ${Uri}: $($_.Exception.Message)"
  }
}

try {
  $repoDir = Resolve-RequiredDirectory -Path (Join-Path $PSScriptRoot '..') -Label 'Plugin repository'
  if ([string]::IsNullOrWhiteSpace($HarnessDir)) {
    $HarnessDir = Join-Path (Split-Path -Parent $repoDir) 'deepseek-harness'
  }
  $HarnessDir = Resolve-RequiredDirectory -Path $HarnessDir -Label 'HarnessDir'

  $NodePath = Resolve-RequiredExecutable -Path $NodePath -CommandName 'node' -Label 'Node executable'
  $PnpmPath = Resolve-RequiredExecutable -Path $PnpmPath -CommandName 'pnpm' -Label 'Pnpm executable'
  if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $repoPython = Join-Path $repoDir '.venv\Scripts\python.exe'
    $PythonPath = if (Test-Path -LiteralPath $repoPython -PathType Leaf) { $repoPython } else { $null }
  }
  $PythonPath = Resolve-RequiredExecutable -Path $PythonPath -CommandName 'python' -Label 'Python executable'

  $harnessManifestPath = Join-Path $HarnessDir 'package.json'
  if (-not (Test-Path -LiteralPath $harnessManifestPath -PathType Leaf)) {
    throw "HarnessDir does not contain package.json: $HarnessDir"
  }
  $harnessManifest = Get-Content -LiteralPath $harnessManifestPath -Raw | ConvertFrom-Json
  if ($harnessManifest.version -ne '0.1.0-rc.7') {
    throw "Harness baseline must be 0.1.0-rc.7, found $($harnessManifest.version) in $harnessManifestPath"
  }

  $harnessCli = Join-Path $HarnessDir 'apps\cli\lib\bin.js'
  if (-not (Test-Path -LiteralPath $harnessCli -PathType Leaf)) {
    throw "Built Harness CLI not found at $harnessCli; run 'pnpm run build' in HarnessDir first"
  }

  $tempParent = if ([string]::IsNullOrWhiteSpace($TempDir)) {
    [System.IO.Path]::GetTempPath()
  } else {
    $TempDir
  }
  if (Test-Path -LiteralPath $tempParent -PathType Leaf) {
    throw "TempDir is a file, not a directory: $tempParent"
  }
  New-Item -ItemType Directory -Path $tempParent -Force | Out-Null
  $script:RunDirParent = (Resolve-Path -LiteralPath $tempParent).Path
  $runName = 'dsh-wren-data-agent-acceptance-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0, 8))
  $script:RunDir = Join-Path $script:RunDirParent $runName
  New-Item -ItemType Directory -Path $script:RunDir -Force | Out-Null

  $oldDshHome = $script:OldDshHome
  $oldPath = $script:OldPath
  $env:DSH_HOME = Join-Path $script:RunDir 'home'
  New-Item -ItemType Directory -Path $env:DSH_HOME -Force | Out-Null
  # The Harness plugin command hard-codes `pnpm`; the acceptance flow invokes
  # our explicit PnpmPath directly, but keeping its directory first makes any
  # child package-manager lookup deterministic too.
  $pnpmDirectory = Split-Path -Parent $PnpmPath
  $env:PATH = "$pnpmDirectory$([System.IO.Path]::PathSeparator)$oldPath"
  # pnpm's recursive scripts can spawn a second pnpm process. The command-line
  # registry flag is not inherited by that child on all pnpm 11 builds, while
  # these environment settings are; set both spellings and restore them below.
  $env:PNPM_CONFIG_REGISTRY = 'https://registry.npmjs.org'
  $env:npm_config_registry = 'https://registry.npmjs.org'

  $packsDir = Join-Path $script:RunDir 'packs'
  New-Item -ItemType Directory -Path $packsDir -Force | Out-Null
  $registryArg = '--config.registry=https://registry.npmjs.org'

  if (-not $SkipBuild) {
    Invoke-Checked -Executable $PnpmPath -Arguments @($registryArg, 'build') -WorkingDirectory $repoDir -Label 'Build plugin packages'
  }

  $packageOrder = @('contract', 'host', 'client', 'bundle')
  $tarballs = [ordered] @{}
  foreach ($packageName in $packageOrder) {
    $packageDir = Join-Path $repoDir "packages\$packageName"
    $manifestPath = Join-Path $packageDir 'package.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    Invoke-Checked -Executable $PnpmPath -Arguments @($registryArg, 'pack', '--pack-destination', $packsDir) -WorkingDirectory $packageDir -Label "Pack $($manifest.name)"
    $packedName = "$($manifest.name.TrimStart('@').Replace('/', '-'))-$($manifest.version).tgz"
    $packedPath = Join-Path $packsDir $packedName
    if (-not (Test-Path -LiteralPath $packedPath -PathType Leaf)) {
      throw "Expected tarball was not produced: $packedPath"
    }
    $tarballs[$manifest.name] = (Resolve-Path -LiteralPath $packedPath).Path
  }

  $profileDir = Join-Path $env:DSH_HOME 'profiles\web'
  New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
  $bundleName = '@hejielijob/dsh-wren-data-agent'
  $hostName = '@hejielijob/dsh-wren-data-agent-host'
  $clientName = '@hejielijob/dsh-wren-data-agent-client'
  $contractName = '@hejielijob/dsh-wren-data-agent-contract'
  $subprocessPeerName = '@deepseek-ai/dsh-subprocess'
  $cordisPeerName = '@deepseek-ai/cordis'
  $invariantsPeerName = '@deepseek-ai/dsh-invariants'
  $bundleSpec = 'file:' + ($tarballs[$bundleName] -replace '\\', '/')
  $hostSpec = 'file:' + ($tarballs[$hostName] -replace '\\', '/')
  $clientSpec = 'file:' + ($tarballs[$clientName] -replace '\\', '/')
  $contractSpec = 'file:' + ($tarballs[$contractName] -replace '\\', '/')

  $profileManifest = [ordered]@{
    name = 'dsh-profile-web'
    private = $true
    dependencies = [ordered]@{
      $bundleName = $bundleSpec
      $hostName = $hostSpec
      $clientName = $clientSpec
      $contractName = $contractSpec
      # The Harness loader normally supplies Host peers. The direct installed-
      # artifact probe below imports Host outside that loader, so its temporary
      # profile explicitly supplies the same rc.7 peer.
      $subprocessPeerName = '0.1.0-rc.7'
      $cordisPeerName = '^4.0.1'
      $invariantsPeerName = '0.1.0-rc.7'
    }
    dsh = [ordered]@{
      profile = [ordered]@{
        bundles = @('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', $bundleName)
      }
    }
  }
  Write-JsonFile -Path (Join-Path $profileDir 'package.json') -Value $profileManifest
  Write-Utf8NoBom -Path (Join-Path $profileDir 'cordis.patch.yml') -Content "# Isolated acceptance profile; no Harness source is modified.`n[]`n"

  $workspaceYaml = @(
    'packages:',
    '  - .',
    '',
    'nodeLinker: hoisted',
    'autoInstallPeers: false',
    '',
    'overrides:',
    "  $((Quote-YamlScalar $contractName)): $((Quote-YamlScalar $contractSpec))",
    "  $((Quote-YamlScalar $hostName)): $((Quote-YamlScalar $hostSpec))",
    "  $((Quote-YamlScalar $clientName)): $((Quote-YamlScalar $clientSpec))"
  ) -join [Environment]::NewLine
  Write-Utf8NoBom -Path (Join-Path $profileDir 'pnpm-workspace.yaml') -Content ($workspaceYaml + [Environment]::NewLine)

  Invoke-Checked -Executable $PnpmPath -Arguments @($registryArg, 'install', '--ignore-scripts') -WorkingDirectory $profileDir -Label 'Install packed plugin into isolated Harness profile'
  foreach ($packageName in @($bundleName, $hostName, $clientName, $contractName)) {
    $installedManifest = Join-Path $profileDir "node_modules\$($packageName.Replace('/', '\'))\package.json"
    if (-not (Test-Path -LiteralPath $installedManifest -PathType Leaf)) {
      throw "Installed package is missing from the isolated profile: $packageName"
    }
  }

  # Exercise the installed Host artifact against the Sidecar staged inside its
  # npm package. This proves the runtime does not fall back to this checkout's
  # Host build or python/sidecar source tree.
  $installedHostDir = Join-Path $profileDir "node_modules\$($hostName.Replace('/', '\'))"
  $packagedSidecarDir = Join-Path $installedHostDir 'python\sidecar'
  $installedHostModule = Join-Path $installedHostDir 'lib\sidecar.js'
  $exampleProject = Join-Path $repoDir 'examples\wren-postgres'
  Invoke-Checked -Executable $NodePath -Arguments @(
    (Join-Path $repoDir 'scripts\probe-sidecar.mjs'),
    $PythonPath,
    $exampleProject,
    $packagedSidecarDir,
    $installedHostModule
  ) -WorkingDirectory $profileDir -Label 'Probe installed Host and packaged Python sidecar'

  $dumpPath = Join-Path $script:RunDir 'dump-config.txt'
  $dumpErrorPath = Join-Path $script:RunDir 'dump-config.stderr.log'
  $dump = Invoke-Captured -Executable $NodePath -Arguments @($harnessCli, '--profile', 'web', '--dump-config') -WorkingDirectory $HarnessDir
  Write-Utf8NoBom -Path $dumpPath -Content $dump.Stdout
  Write-Utf8NoBom -Path $dumpErrorPath -Content $dump.Stderr
  if ($dump.ExitCode -ne 0) {
    throw "Harness dump-config failed with exit code $($dump.ExitCode); see $dumpErrorPath"
  }
  Assert-Contains -Text $dump.Stdout -Needle $hostName -EvidenceLabel 'dump-config'
  Assert-Contains -Text $dump.Stdout -Needle $clientName -EvidenceLabel 'dump-config'
  Write-Host "[acceptance] dump-config contains Host and Client bundle rows"

  if ($Port -eq 0) {
    $Port = Get-FreeLoopbackPort
  }
  if ($Port -lt 1 -or $Port -gt 65535) {
    throw "Port must be in 1..65535, or 0 for an automatically selected loopback port"
  }
  $baseUri = "http://127.0.0.1:$Port/"
  $serverStdoutPath = Join-Path $script:RunDir 'harness.stdout.log'
  $serverStderrPath = Join-Path $script:RunDir 'harness.stderr.log'
  $serverStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $serverStartInfo.FileName = $NodePath
  $serverStartInfo.WorkingDirectory = $HarnessDir
  $serverStartInfo.UseShellExecute = $false
  $serverStartInfo.CreateNoWindow = $true
  $serverStartInfo.RedirectStandardOutput = $true
  $serverStartInfo.RedirectStandardError = $true
  foreach ($argument in @($harnessCli, '--profile', 'web', '--host', '127.0.0.1', '--port', [string] $Port)) {
    [void] $serverStartInfo.ArgumentList.Add($argument)
  }
  $script:ServerProcess = [System.Diagnostics.Process]::new()
  $script:ServerProcess.StartInfo = $serverStartInfo
  if (-not $script:ServerProcess.Start()) {
    throw 'Could not start the built Harness web profile'
  }
  $script:ServerStdoutTask = $script:ServerProcess.StandardOutput.ReadToEndAsync()
  $script:ServerStderrTask = $script:ServerProcess.StandardError.ReadToEndAsync()

  $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
  $indexResponse = $null
  while ((Get-Date) -lt $deadline) {
    if ($script:ServerProcess.HasExited) {
      $earlyStderr = $script:ServerStderrTask.GetAwaiter().GetResult()
      throw "Harness web process exited early with code $($script:ServerProcess.ExitCode): $earlyStderr"
    }
    try {
      $indexResponse = Invoke-HttpGet -Uri $baseUri -TimeoutSeconds 3
      if ([int] $indexResponse.StatusCode -eq 200) {
        break
      }
    } catch {
      # Startup is expected to take a few seconds while Cordis mounts the tree.
    }
    Start-Sleep -Milliseconds 250
  }
  if ($null -eq $indexResponse -or [int] $indexResponse.StatusCode -ne 200) {
    throw "Harness web profile did not become ready within $StartupTimeoutSeconds seconds on $baseUri"
  }

  if ($PauseAfterServerReady) {
    Write-Host 'ACCEPTANCE_PAUSED_FOR_REPLAY'
    Write-Host "ACCEPTANCE_REPLAY_DSH_HOME=$env:DSH_HOME"
    Write-Host "ACCEPTANCE_REPLAY_BASE_URL=$baseUri"
    Write-Host 'Press Enter after replay seeding/browser checks are complete.'
    [void] [Console]::ReadLine()
  }

  $indexBody = [string] $indexResponse.Content
  Assert-Contains -Text $indexBody -Needle $clientName -EvidenceLabel 'web index boot manifest'
  Write-Host "[acceptance] web index returned HTTP 200 and advertises Client plugin"

  $clientUri = "$baseUri`plugins/$clientName/client.js"
  $clientResponse = Invoke-HttpGet -Uri $clientUri -TimeoutSeconds 5
  if ([int] $clientResponse.StatusCode -ne 200) {
    throw "Client bundle did not return HTTP 200: $clientUri"
  }
  $clientBody = [string] $clientResponse.Content
  Assert-Contains -Text $clientBody -Needle 'window.__ModuleLoader__.load' -EvidenceLabel 'client.js response'
  Assert-Contains -Text $clientBody -Needle $clientName -EvidenceLabel 'client.js response'
  Write-Host "[acceptance] $clientUri returned HTTP 200 with the generated Client artifact"

  $result = [ordered]@{
    status = 'passed'
    harnessVersion = [string] $harnessManifest.version
    harnessDir = $HarnessDir
    profile = $profileDir
    port = $Port
    bundles = @($bundleName, $hostName, $clientName, $contractName)
    dumpConfig = $dumpPath
    clientUrl = $clientUri
  }
  Write-JsonFile -Path (Join-Path $script:RunDir 'result.json') -Value $result
  Write-Host ''
  Write-Host 'ACCEPTANCE_PASS'
  Write-Host "  Harness: $HarnessDir (0.1.0-rc.7)"
  Write-Host "  Profile: $profileDir"
  Write-Host "  dump-config: Host + Client rows detected"
  Write-Host "  HTTP: GET $baseUri -> 200"
  Write-Host "  HTTP: GET $clientUri -> 200"
  $script:Succeeded = $true
} catch {
  Write-Error $_
  if ($null -ne $script:RunDir) {
    Write-Host "ACCEPTANCE_FAIL_WORKDIR=$script:RunDir"
  }
  exit 1
} finally {
  Stop-ProcessTree -Process $script:ServerProcess
  if ($null -ne $script:ServerProcess) {
    try {
      $stdout = $script:ServerStdoutTask.GetAwaiter().GetResult()
      $stderr = $script:ServerStderrTask.GetAwaiter().GetResult()
      if ($null -ne $script:RunDir) {
        Write-Utf8NoBom -Path (Join-Path $script:RunDir 'harness.stdout.log') -Content $stdout
        Write-Utf8NoBom -Path (Join-Path $script:RunDir 'harness.stderr.log') -Content $stderr
      }
    } catch {
      Write-Warning "Could not collect Harness process logs: $($_.Exception.Message)"
    }
  }
  if ($null -ne $script:OldDshHome) {
    $env:DSH_HOME = $script:OldDshHome
  } else {
    Remove-Item Env:DSH_HOME -ErrorAction SilentlyContinue
  }
  if ($null -ne $script:OldPath) {
    $env:PATH = $script:OldPath
  }
  if ($null -ne $script:OldPnpmConfigRegistry) {
    $env:PNPM_CONFIG_REGISTRY = $script:OldPnpmConfigRegistry
  } else {
    Remove-Item Env:PNPM_CONFIG_REGISTRY -ErrorAction SilentlyContinue
  }
  if ($null -ne $script:OldNpmConfigRegistry) {
    $env:npm_config_registry = $script:OldNpmConfigRegistry
  } else {
    Remove-Item Env:npm_config_registry -ErrorAction SilentlyContinue
  }
  if ($null -ne $script:RunDir -and ((-not $script:Succeeded) -or $KeepTemp)) {
    Write-Host "Acceptance artifacts kept at: $script:RunDir"
  } elseif ($null -ne $script:RunDir) {
    $runDirFull = (Resolve-Path -LiteralPath $script:RunDir -ErrorAction SilentlyContinue).Path
    $parentFull = (Resolve-Path -LiteralPath $script:RunDirParent -ErrorAction SilentlyContinue).Path
    $parentPrefix = if ($null -eq $parentFull) {
      $null
    } else {
      $parentFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    }
    $runLeaf = if ($null -eq $runDirFull) { $null } else { Split-Path -Leaf $runDirFull }
    if (
      $null -ne $runDirFull -and
      $null -ne $parentPrefix -and
      $runDirFull.StartsWith($parentPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
      $runLeaf.StartsWith('dsh-wren-data-agent-acceptance-', [System.StringComparison]::Ordinal)
    ) {
      Remove-Item -LiteralPath $runDirFull -Recurse -Force -ErrorAction SilentlyContinue
    } else {
      Write-Warning "Refusing to remove unexpected acceptance workdir path: $script:RunDir"
    }
  }
}
