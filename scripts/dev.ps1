param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet('setup', 'dev', 'stop', 'migrate', 'seed', 'reset-preview', 'reset', 'backup', 'restore', 'backup-recover', 'restore-cleanup', 'lint', 'typecheck', 'test', 'build')]
  [string]$Task,
  [string]$PytestTarget,
  [string]$E2eTarget,
  [string]$BackupPath,
  [ValidateRange(30, 3600)]
  [int]$DrainTimeout = 600,
  [switch]$KeepIsolated,
  [string]$OperationId,
  [string]$ProjectId,
  [string]$Workspace,
  [string]$ConfirmFingerprint
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $repo
try {
  function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    <#
    .SYNOPSIS
    Run an external program and fail the current task when it exits unsuccessfully.

    .PARAMETER Program
    Executable name or path to invoke.

    .PARAMETER Arguments
    Argument vector passed to the executable without joining it into a command string.

    .OUTPUTS
    Forwards output emitted by the external program.

    .NOTES
    Throws when the native process exit code is nonzero.
    #>
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
  }

  function New-Secret {
    <#
    .SYNOPSIS
    Generate a 48-byte cryptographically random secret encoded as unpadded Base64URL.

    .OUTPUTS
    System.String
    #>
    $bytes = [byte[]]::new(48)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
  }

  function Get-ComposeArgs {
    <#
    .SYNOPSIS
    Return the base and development Compose file arguments used by local stack commands.

    .OUTPUTS
    System.String[]
    #>
    return @('-f', 'docker-compose.yml', '-f', 'docker-compose.dev.yml')
  }

  function Get-FreePort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
  }

  if ($Workspace -and $Workspace -notmatch '^bbd-os-ws-[0-9a-f]{12}$') {
    throw 'Workspace must match bbd-os-ws- followed by exactly 12 lowercase hexadecimal characters'
  }
  if ($Workspace -and $Task -notin @('dev', 'stop', 'seed', 'reset-preview', 'reset')) {
    throw '-Workspace applies only to named development-workspace commands'
  }

  switch ($Task) {
    'setup' {
      if (-not (Test-Path '.env')) {
        $dbPassword = New-Secret
        $config = @"
PUBLIC_ORIGIN=http://localhost:3000
SECURE_COOKIES=false
SETUP_TOKEN=$(New-Secret)
CSRF_SIGNING_SECRET=$(New-Secret)
SESSION_LIFETIME_HOURS=24
POSTGRES_PASSWORD=$dbPassword
DATABASE_URL=postgresql+asyncpg://bbd:${dbPassword}@postgres:5432/bbd
REDIS_URL=redis://redis:6379/0
DATA_DIR=/data
WEB_PORT=3000
N8N_ENCRYPTION_KEY=$(New-Secret)
BACKUP_AGE_RECIPIENT=
BACKUP_AGE_IDENTITY_PATH=
OMNIROUTE_BASE_URL=
OMNIROUTE_API_KEY=
OMNIROUTE_MODELS={}
"@
        [System.IO.File]::WriteAllText((Join-Path $repo '.env'), $config, [System.Text.UTF8Encoding]::new($false))
        Write-Output 'Created local .env with generated secrets. Keep it private.'
      } else {
        Write-Output 'Kept existing .env unchanged.'
      }
      Invoke-Checked 'uv' @('sync', '--frozen')
      Invoke-Checked 'npm' @('ci')
    }
    'dev' {
      if ($Workspace) { Invoke-Checked 'python' @('scripts/dev_workspace.py', 'up', '--name', $Workspace) }
      else { Invoke-Checked 'docker' (@('compose') + (Get-ComposeArgs) + @('up', '-d', '--build')) }
    }
    'stop' {
      if ($Workspace) { Invoke-Checked 'python' @('scripts/dev_workspace.py', 'stop', '--name', $Workspace) }
      else { Invoke-Checked 'docker' (@('compose') + (Get-ComposeArgs) + @('stop')) }
    }
    'migrate' { Invoke-Checked 'docker' (@('compose') + (Get-ComposeArgs) + @('run', '--rm', 'migrate')) }
    'seed' {
      if ($Workspace) { Invoke-Checked 'python' @('scripts/dev_workspace.py', 'seed', '--name', $Workspace) }
      else { Invoke-Checked 'docker' @('compose', '-f', 'docker-compose.yml', 'run', '--rm', '--build', 'api', 'python', '-m', 'modules.knowledge.documents.seed') }
    }
    'reset-preview' {
      if (-not $Workspace) { throw 'Specify -Workspace bbd-os-ws-<12 lowercase hex characters>' }
      Invoke-Checked 'python' @('scripts/dev_workspace.py', 'reset-preview', '--name', $Workspace)
    }
    'reset' {
      if (-not $Workspace -or -not $ConfirmFingerprint) { throw 'Specify -Workspace and the fingerprint shown by reset-preview' }
      Invoke-Checked 'python' @('scripts/dev_workspace.py', 'reset', '--name', $Workspace, '--confirm', $ConfirmFingerprint)
    }
    'backup' {
      if ([string]::IsNullOrWhiteSpace($BackupPath)) { throw 'Use -BackupPath to choose a new archive destination.' }
      Invoke-Checked 'uv' @('run', 'python', 'scripts/backup.py', '--output', $BackupPath, '--drain-timeout', [string]$DrainTimeout)
    }
    'restore' {
      if ([string]::IsNullOrWhiteSpace($BackupPath)) { throw 'Use -BackupPath to choose an encrypted archive.' }
      $arguments = @('run', 'python', 'scripts/restore.py', $BackupPath)
      if ($KeepIsolated) { $arguments += '--keep-isolated' }
      Invoke-Checked 'uv' $arguments
    }
    'backup-recover' {
      if ([string]::IsNullOrWhiteSpace($OperationId)) { throw 'Use -OperationId to resume the stored backup operation.' }
      $arguments = @('run', 'python', 'scripts/restore.py', '--recover-operation', $OperationId)
      if (-not [string]::IsNullOrWhiteSpace($BackupPath)) { $arguments += $BackupPath }
      Invoke-Checked 'uv' $arguments
    }
    'restore-cleanup' {
      if ([string]::IsNullOrWhiteSpace($ProjectId)) { throw 'Use -ProjectId to remove a retained isolated restore.' }
      Invoke-Checked 'uv' @('run', 'python', 'scripts/restore.py', '--cleanup-project', $ProjectId)
    }
    'lint' {
      Invoke-Checked 'uv' @('run', 'ruff', 'check', 'core', 'apps', 'modules', 'tests', 'infrastructure/postgres/migrations')
      Invoke-Checked 'npm' @('run', 'lint')
    }
    'typecheck' {
      Invoke-Checked 'uv' @('run', 'mypy', 'core', 'apps', 'modules')
      Invoke-Checked 'npm' @('run', 'typecheck')
    }
    'test' {
      if ($PytestTarget -and $E2eTarget) { throw 'Choose only one of -PytestTarget or -E2eTarget.' }
      if ($PytestTarget -and $PytestTarget -notlike 'tests/integration/*') {
        Invoke-Checked 'uv' @('run', 'pytest', $PytestTarget, '-q')
        break
      }
      if (-not $PytestTarget -and -not $E2eTarget) {
        Invoke-Checked 'uv' @('run', 'pytest', '-q')
      }
      $project = 'bbd-os-test-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
      $compose = @('compose', '-p', $project, '-f', 'docker-compose.yml', '-f', 'docker-compose.test.yml')
      $previousWebPort = $env:WEB_PORT
      $previousApiTestPort = $env:API_TEST_PORT
      $previousTestPostgresPort = $env:TEST_POSTGRES_PORT
      $previousTestDatabaseUrl = $env:TEST_DATABASE_URL
      $previousIntegration = $env:BBD_INTEGRATION
      $previousApiUrl = $env:BBD_API_URL
      $previousTestOrigin = $env:TEST_PUBLIC_ORIGIN
      $previousE2eToken = $env:E2E_SETUP_TOKEN
      $previousPlaywrightUrl = $env:PLAYWRIGHT_BASE_URL
      $previousExternalServer = $env:PLAYWRIGHT_EXTERNAL_SERVER
      $webPort = Get-FreePort
      $apiPort = Get-FreePort
      while ($apiPort -eq $webPort) { $apiPort = Get-FreePort }
      $postgresPort = Get-FreePort
      while ($postgresPort -eq $webPort -or $postgresPort -eq $apiPort) {
        $postgresPort = Get-FreePort
      }
      try {
        $env:WEB_PORT = [string]$webPort
        $env:API_TEST_PORT = [string]$apiPort
        $env:TEST_POSTGRES_PORT = [string]$postgresPort
        $env:TEST_DATABASE_URL = "postgresql+asyncpg://bbd_test:bbd-os-test-db-only@127.0.0.1:$postgresPort/bbd_test"
        $env:TEST_PUBLIC_ORIGIN = "http://localhost:$webPort"
        Invoke-Checked 'docker' ($compose + @('up', '-d', '--build'))
        # `up` waits for migration completion; this second run proves the revision is idempotent.
        Invoke-Checked 'docker' ($compose + @('run', '--rm', 'migrate'))
        Invoke-Checked 'docker' ($compose + @('run', '--rm', '--no-deps', 'api', 'python', '-c', 'import apps.api.main, core.auth.dependencies, modules'))
        $env:BBD_INTEGRATION = '1'
        $env:BBD_API_URL = "http://localhost:$apiPort"
        $identity = & docker @($compose + @('exec', '-T', 'postgres', 'psql', '-U', 'bbd_test', '-d', 'bbd_test', '-At', '-c', "SELECT current_database() || '|' || current_user"))
        if ($LASTEXITCODE -ne 0) { throw 'Could not verify disposable PostgreSQL identity' }
        if (($identity -join '').Trim() -ne 'bbd_test|bbd_test') { throw 'Refusing integration tests against a non-test database' }
        Invoke-Checked 'uv' @('run', 'pytest', 'tests/integration/test_auth_race.py', '-q')
        if ($PytestTarget) {
          if ($PytestTarget -ne 'tests/integration/test_auth_race.py') {
            Invoke-Checked 'uv' @('run', 'pytest', $PytestTarget, '-q', '--ignore=tests/integration/test_auth_race.py')
          }
        } else {
          Invoke-Checked 'uv' @('run', 'pytest', 'tests/integration', '-q', '--ignore=tests/integration/test_auth_race.py')
        }
        if (-not $PytestTarget -or $E2eTarget) {
          if ($project -notmatch '^bbd-os-test-[0-9a-f]{10}$') { throw 'Refusing browser reset outside unique disposable project' }
          $reset = (Get-Content -Raw (Join-Path $PSScriptRoot 'sql/e2e-reset.sql')) -replace '\r?\n', ' '
          Invoke-Checked 'docker' ($compose + @('exec', '-T', 'postgres', 'psql', '-U', 'bbd_test', '-d', 'bbd_test', '-v', 'ON_ERROR_STOP=1', '-c', $reset))
        $env:E2E_SETUP_TOKEN = 'bbd-os-disposable-test-token'
        $env:PLAYWRIGHT_BASE_URL = "http://localhost:$webPort"
        $env:PLAYWRIGHT_EXTERNAL_SERVER = '1'
          if ($E2eTarget) { Invoke-Checked 'npm' @('run', 'test:e2e', '--', $E2eTarget) }
          elseif (-not $PytestTarget) { Invoke-Checked 'npm' @('run', 'test:e2e') }
        }
      } finally {
        & docker @($compose + @('down', '--volumes', '--remove-orphans'))
        if ($null -eq $previousWebPort) { Remove-Item Env:WEB_PORT -ErrorAction SilentlyContinue }
        else { $env:WEB_PORT = $previousWebPort }
        if ($null -eq $previousApiTestPort) { Remove-Item Env:API_TEST_PORT -ErrorAction SilentlyContinue }
        else { $env:API_TEST_PORT = $previousApiTestPort }
        if ($null -eq $previousTestPostgresPort) { Remove-Item Env:TEST_POSTGRES_PORT -ErrorAction SilentlyContinue }
        else { $env:TEST_POSTGRES_PORT = $previousTestPostgresPort }
        if ($null -eq $previousTestDatabaseUrl) { Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue }
        else { $env:TEST_DATABASE_URL = $previousTestDatabaseUrl }
        if ($null -eq $previousIntegration) { Remove-Item Env:BBD_INTEGRATION -ErrorAction SilentlyContinue }
        else { $env:BBD_INTEGRATION = $previousIntegration }
        if ($null -eq $previousApiUrl) { Remove-Item Env:BBD_API_URL -ErrorAction SilentlyContinue }
        else { $env:BBD_API_URL = $previousApiUrl }
        if ($null -eq $previousTestOrigin) { Remove-Item Env:TEST_PUBLIC_ORIGIN -ErrorAction SilentlyContinue }
        else { $env:TEST_PUBLIC_ORIGIN = $previousTestOrigin }
        if ($null -eq $previousE2eToken) { Remove-Item Env:E2E_SETUP_TOKEN -ErrorAction SilentlyContinue }
        else { $env:E2E_SETUP_TOKEN = $previousE2eToken }
        if ($null -eq $previousPlaywrightUrl) { Remove-Item Env:PLAYWRIGHT_BASE_URL -ErrorAction SilentlyContinue }
        else { $env:PLAYWRIGHT_BASE_URL = $previousPlaywrightUrl }
        if ($null -eq $previousExternalServer) { Remove-Item Env:PLAYWRIGHT_EXTERNAL_SERVER -ErrorAction SilentlyContinue }
        else { $env:PLAYWRIGHT_EXTERNAL_SERVER = $previousExternalServer }
      }
    }
    'build' {
      Invoke-Checked 'npm' @('run', 'build')
      Invoke-Checked 'docker' @('compose', '-f', 'docker-compose.yml', 'build')
    }
  }
} finally {
  Pop-Location
}
