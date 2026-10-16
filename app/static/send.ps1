param(
    [Parameter(Mandatory = $true)][string]$Node,
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination,
    [ValidateRange(1, 2147483647)][int]$ConnectTimeout = 180,
    [ValidateSet('p2p', 'onedrive')][string]$Transport = 'p2p',
    [switch]$Overwrite
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$hub = '__HUB_URL__'
$uvVersion = '0.13.1'
$archiveHash = '3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672'
$target = 'x86_64-pc-windows-msvc'
$envNames = @('FLEET_TOKEN','FLEET_CLIENT_HOME','UV_CACHE_DIR','UV_PYTHON_INSTALL_DIR','UV_PYTHON_BIN_DIR',
    'UV_PYTHON_INSTALL_BIN','UV_PYTHON_NO_REGISTRY','UV_NO_MODIFY_PATH','UV_PYTHON_INSTALL_MIRROR','PYTHONUTF8')
$savedEnvironment = @{}
foreach ($name in $envNames) { $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$savedTls = [Net.ServicePointManager]::SecurityProtocol
$scratch = $null
$clientRoot = $null
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User

function Set-PrivatePath([string]$Path, [bool]$Directory = $false) {
    if ($Directory) {
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($currentSid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    } else {
        $acl = New-Object Security.AccessControl.FileSecurity
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($currentSid, 'FullControl', 'Allow')
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule($rule)
    # Persist only the access rules. Windows PowerShell Set-Acl can request
    # audit privileges when replacing an already protected directory ACL.
    if ($PSVersionTable.PSEdition -eq 'Core') {
        if ($Directory) { [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]$Path, $acl) }
        else { [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]$Path, $acl) }
    } else {
        if ($Directory) { [IO.Directory]::SetAccessControl($Path, $acl) }
        else { [IO.File]::SetAccessControl($Path, $acl) }
    }
}

function Write-Utf8([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Download-File([string]$Url, [string]$Path, [int]$Timeout = 300) {
    $oldProgress = $ProgressPreference
    try {
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Path -TimeoutSec $Timeout
    } finally { $ProgressPreference = $oldProgress }
}

function Update-TransferScript([string]$Path, [string]$TemporaryPath) {
    $headers = @{}
    if ((Test-Path -LiteralPath $Path -PathType Leaf) -and
        -not ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        $headers['If-None-Match'] = '"' + (Get-Sha256 $Path) + '"'
    }
    $oldProgress = $ProgressPreference
    try {
        $ProgressPreference = 'SilentlyContinue'
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri ($hub + '/transfer.py') -Headers $headers -TimeoutSec 60
            $status = [int]$response.StatusCode
        } catch {
            # Windows PowerShell reports 304 as an exception; PowerShell Core
            # may return it normally. Both retain the stable cached script path.
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 304) { $status = 304 }
            else { throw }
        }
        if ($status -eq 200) {
            # Do not combine -OutFile and -PassThru: older PowerShell versions
            # can leave an empty file. Preserve the small script's original bytes.
            $output = [IO.File]::Create($TemporaryPath)
            try {
                $response.RawContentStream.Position = 0
                $response.RawContentStream.CopyTo($output)
            } finally { $output.Dispose() }
            if ((Get-Item -LiteralPath $TemporaryPath).Length -eq 0) { throw '中枢返回了空上传组件。' }
            Move-Item -LiteralPath $TemporaryPath -Destination $Path -Force
        } elseif ($status -eq 304) {
            if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or (Get-Item -LiteralPath $Path).Length -eq 0 -or
                ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw '本地上传组件缓存不可用，请重试。'
            }
        } else { throw ('中枢上传组件响应异常: ' + $status) }
    } finally { $ProgressPreference = $oldProgress }
}

try {
    if ($hub -notmatch '^https://' -and $hub -notmatch '^http://(?:127\.0\.0\.1|localhost):') {
        throw '启动脚本缺少有效中枢地址。'
    }
    if (-not $Node.Trim() -or -not $Destination) { throw '节点和远端路径不能为空。' }
    $sourceItem = Get-Item -LiteralPath $Source
    if ($sourceItem.PSProvider.Name -ne 'FileSystem') { throw '源路径必须是本地文件或目录。' }
    $sourcePath = $sourceItem.FullName
    $architecture = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $architecture = $env:PROCESSOR_ARCHITEW6432 }
    if ($architecture -ne 'AMD64') { throw '当前 Windows 上传客户端需要 x64 Windows。' }
    [Net.ServicePointManager]::SecurityProtocol = $savedTls -bor [Net.SecurityProtocolType]::Tls12
    $clientRoot = $env:FLEET_CLIENT_HOME
    if (-not $clientRoot) { $clientRoot = Join-Path $env:LOCALAPPDATA 'fleet-hub\client' }
    $clientRoot = [IO.Path]::GetFullPath($clientRoot)
    if (Test-Path -LiteralPath $clientRoot) {
        if ((Get-Item -LiteralPath $clientRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw '客户端目录不能是目录链接。'
        }
    } else { New-Item -ItemType Directory -Path $clientRoot -Force | Out-Null }
    Set-PrivatePath $clientRoot $true
    $env:FLEET_CLIENT_HOME = $clientRoot
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $hubId = ([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($hub)))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose() }
    $configDir = Join-Path $clientRoot ('hubs\' + $hubId)
    $binDir = Join-Path $clientRoot 'bin'
    New-Item -ItemType Directory -Path $configDir, $binDir -Force | Out-Null
    Set-PrivatePath $configDir $true
    $tokenPath = Join-Path $configDir 'token.dpapi'
    $token = $env:FLEET_TOKEN
    if (-not $token) {
        if (Test-Path -LiteralPath $tokenPath) {
            if ((Get-Item -LiteralPath $tokenPath).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw '本地鉴权配置不能是文件链接。'
            }
            Set-PrivatePath $tokenPath
            $secureToken = ConvertTo-SecureString ([IO.File]::ReadAllText($tokenPath))
        } else {
            $secureToken = Read-Host '首次使用，请输入中枢访问令牌（输入不回显）' -AsSecureString
            if ($secureToken.Length -eq 0) { throw '访问令牌不能为空。' }
            $tokenTemp = Join-Path $configDir ('.token-' + [Guid]::NewGuid().ToString('N'))
            Write-Utf8 $tokenTemp (ConvertFrom-SecureString $secureToken)
            Set-PrivatePath $tokenTemp
            Move-Item -LiteralPath $tokenTemp -Destination $tokenPath -Force
        }
        $token = (New-Object Net.NetworkCredential('', $secureToken)).Password
    }
    $env:FLEET_TOKEN = $token
    $token = $null
    $scratch = Join-Path $clientRoot ('.prepare-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $scratch | Out-Null
    $uvBinary = Join-Path $binDir ('uv-' + $uvVersion + '-' + $target + '.exe')
    $uvHashPath = $uvBinary + '.sha256'
    $reuse = (Test-Path -LiteralPath $uvBinary) -and (Test-Path -LiteralPath $uvHashPath)
    if ($reuse) { $reuse = (Get-Sha256 $uvBinary) -eq ([IO.File]::ReadAllText($uvHashPath).Trim()) }
    if (-not $reuse) {
        Write-Host '正在自动准备上传组件，首次运行需要下载。'
        $original = 'uv-' + $target + '.zip'
        $mirror = 'https://gh-proxy.org/https://github.com/astral-sh/uv/releases/download/' + $uvVersion + '/' + $original
        $fallback = $hub + '/downloads/uv-' + $uvVersion + '-' + $target + '.zip'
        $archive = Join-Path $scratch 'uv.zip'
        try { Download-File $mirror $archive }
        catch { Download-File $fallback $archive }
        if ((Get-Sha256 $archive) -ne $archiveHash) { throw '上传组件校验失败，已停止执行。' }
        $expanded = Join-Path $scratch 'expanded'
        Expand-Archive -LiteralPath $archive -DestinationPath $expanded
        $executables = @(Get-ChildItem -LiteralPath $expanded -Recurse -File -Filter 'uv.exe')
        if ($executables.Count -ne 1) { throw '上传组件压缩包格式不正确。' }
        Move-Item -LiteralPath $executables[0].FullName -Destination $uvBinary -Force
        Write-Utf8 $uvHashPath (Get-Sha256 $uvBinary)
    }
    $scriptTemp = Join-Path $scratch 'transfer.py'
    $scriptPath = Join-Path $configDir 'transfer.py'
    Update-TransferScript $scriptPath $scriptTemp
    $env:UV_CACHE_DIR = Join-Path $clientRoot 'cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $clientRoot 'python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $clientRoot 'python-bin'
    $env:UV_PYTHON_INSTALL_BIN = '0'
    $env:UV_PYTHON_NO_REGISTRY = '1'
    $env:UV_NO_MODIFY_PATH = '1'
    $env:UV_PYTHON_INSTALL_MIRROR = 'https://gh-proxy.org/https://github.com/astral-sh/python-build-standalone/releases/download'
    $env:PYTHONUTF8 = '1'
    $runArguments = @('run','--no-config','--no-project','--managed-python','--python','3.12','--script',
        $scriptPath,'--hub',$hub,'--node',$Node,'--source',$sourcePath,'--destination',$Destination,'--connect-timeout',"$ConnectTimeout",'--transport',$Transport.ToLowerInvariant())
    if ($Overwrite) { $runArguments += '--overwrite' }
    Write-Host '正在连接目标服务器并上传；运行环境和依赖会自动准备。'
    & $uvBinary @runArguments
    if ($LASTEXITCODE -ne 0) { throw ('上传未完成，退出码 ' + $LASTEXITCODE + '。') }
} finally {
    foreach ($name in $envNames) { [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process') }
    [Net.ServicePointManager]::SecurityProtocol = $savedTls
    if ($scratch -and (Test-Path -LiteralPath $scratch)) {
        $resolvedScratch = [IO.Path]::GetFullPath($scratch)
        $resolvedRoot = [IO.Path]::GetFullPath($clientRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if (-not $resolvedScratch.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw '临时目录超出客户端目录，已取消清理。'
        }
        Remove-Item -LiteralPath $resolvedScratch -Recurse -Force
    }
}
