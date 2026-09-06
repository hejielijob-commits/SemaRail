Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$allArguments = @($args)
if ($allArguments.Count -eq 0) { throw 'Expected one action: prepare, run, evaluate, clean, or test' }
$Action = $allArguments[0]
if ($Action -notin @('prepare', 'run', 'evaluate', 'clean', 'test')) { throw "Unknown HR benchmark action: $Action" }
$ForwardedArguments = @($allArguments | Select-Object -Skip 1)
if ($ForwardedArguments.Count -gt 0 -and $ForwardedArguments[0] -eq '--') {
  $ForwardedArguments = @($ForwardedArguments | Select-Object -Skip 1)
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

$benchmarkDir = Join-Path $repoDir 'benchmarks\hr-enterprise'
switch ($Action) {
  'prepare' { & $pythonPath (Join-Path $benchmarkDir 'prepare.py') 'all' @ForwardedArguments }
  'run' { & $pythonPath (Join-Path $benchmarkDir 'run.py') 'run' @ForwardedArguments }
  'evaluate' {
    if ($ForwardedArguments -contains '--evidence' -and $ForwardedArguments -notcontains '--report') {
      $ForwardedArguments += @('--report', (Join-Path $repoDir '.benchmark-data\hr-enterprise\reports\model-evaluation.json'))
    }
    & $pythonPath (Join-Path $repoDir 'scripts\evaluate-golden.py') '--corpus' (Join-Path $benchmarkDir 'golden-questions.json') @ForwardedArguments
  }
  'clean' { & $pythonPath (Join-Path $benchmarkDir 'run.py') 'clean' @ForwardedArguments }
  'test' { & $pythonPath '-m' 'unittest' 'discover' '-s' (Join-Path $benchmarkDir 'tests') '-v' @ForwardedArguments }
}
exit $LASTEXITCODE
