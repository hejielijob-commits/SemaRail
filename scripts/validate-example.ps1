[CmdletBinding()]
param(
  [string] $PythonPath = 'python',
  [string] $ProjectDir,
  [ValidateSet('postgres')]
  [string] $DataSource = 'postgres'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($ProjectDir)) {
  $ProjectDir = Join-Path $repoDir 'examples\wren-postgres'
}
$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path

$pythonCommand = Get-Command $PythonPath -ErrorAction Stop | Select-Object -First 1
$python = $pythonCommand.Path
$goldenPath = Join-Path $ProjectDir 'golden-questions.json'
$smokePath = Join-Path $ProjectDir 'smoke-cases.json'

$golden = Get-Content -LiteralPath $goldenPath -Raw -Encoding UTF8 | ConvertFrom-Json
$smoke = Get-Content -LiteralPath $smokePath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($golden.schemaVersion -ne 1 -or $golden.questions.Count -ne 20) {
  throw 'golden-questions.json must contain exactly twenty version-one questions'
}
if ($smoke.schemaVersion -ne 1 -or $smoke.cases.Count -lt 4) {
  throw 'smoke-cases.json must contain at least four version-one cases'
}
$requiredFeatures = @('aggregate', 'date_grain', 'join', 'null')
$actualFeatures = @(
    $smoke.cases |
        ForEach-Object { @($_.features) } |
        ForEach-Object { $_ }
)
foreach ($feature in $requiredFeatures) {
  if ($feature -notin $actualFeatures) {
    throw "smoke cases do not cover required feature: $feature"
  }
}

Push-Location -LiteralPath $ProjectDir
try {
  & $python -m wren.cli context validate --path $ProjectDir --level error --verbose
  if ($LASTEXITCODE -ne 0) { throw 'Wren project validation failed' }

  & $python -m wren.cli context build --path $ProjectDir
  if ($LASTEXITCODE -ne 0) { throw 'Wren project build failed' }

  foreach ($case in $smoke.cases) {
    & $python -m wren.cli dry-plan --datasource $DataSource --sql $case.semanticSql | Out-Null
    if ($LASTEXITCODE -ne 0) {
      throw "Wren dry-plan failed for smoke case: $($case.id)"
    }
    Write-Host "[example] PASS $($case.id)"
  }
} finally {
  Pop-Location
}

Write-Host 'EXAMPLE_VALIDATION_PASS'
Write-Host "  Wren project: $ProjectDir"
Write-Host "  Golden questions: $($golden.questions.Count)"
Write-Host "  Smoke dry-plans: $($smoke.cases.Count)"
