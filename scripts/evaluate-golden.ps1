Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$forwardedArguments = @($args)
if ($forwardedArguments.Count -gt 0 -and $forwardedArguments[0] -eq '--') {
  $forwardedArguments = @($forwardedArguments | Select-Object -Skip 1)
}

$repoDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = [Environment]::GetEnvironmentVariable('WREN_PYTHON', 'Process')
if ([string]::IsNullOrWhiteSpace($pythonPath)) {
  $venvPython = if ($IsWindows) { Join-Path $repoDir '.venv\Scripts\python.exe' } else { Join-Path $repoDir '.venv/bin/python' }
  if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $pythonPath = (Resolve-Path -LiteralPath $venvPython).Path
  } else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $pythonCommand) { throw 'Python was not found; pass WREN_PYTHON or create the project .venv' }
    $pythonPath = $pythonCommand.Path
  }
}

& $pythonPath (Join-Path $repoDir 'scripts\evaluate-golden.py') @forwardedArguments
exit $LASTEXITCODE
