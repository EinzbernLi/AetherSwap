# Reproducible Python Environment Baseline

Issue: #51 (`HARDENING-001`)

This repository uses a frozen verification baseline so GitHub Actions and the
Windows Codex verifier do not silently resolve different Python/package
combinations over time.

## Why this exists

Historical successful CI proves dependency drift occurred even without an
intentional application dependency upgrade:

- TASK-005 / TASK-006: CPython 3.12.13, FastAPI 0.141.1, Starlette 1.4.1,
  pytest 9.1.1;
- TASK-019 / TASK-020: CPython 3.12.13, FastAPI 0.141.1, Starlette 1.6.0,
  pytest 9.1.1.

The project requirement files intentionally express dependency surfaces, but
broad lower bounds alone are not a reproducible verification environment.
`constraints.txt` therefore freezes the concrete versions from the latest fully
passing TASK-020 CI environment.

## Frozen verification baseline

Primary fingerprint:

- CPython 3.12.13
- FastAPI 0.141.1
- Starlette 1.6.0
- pytest 9.1.1
- Pydantic 2.13.4
- SQLModel 0.0.39
- SQLAlchemy 2.0.51
- requests 2.34.2
- urllib3 2.7.0

`constraints.txt` contains the wider resolved package set from the same
successful CI environment.

## GitHub Actions

CI must install through the constraint set:

```bash
python -m pip install -r requirements.txt -c constraints.txt
python -m pip install -r requirements-ci.txt -c constraints.txt
python -m pip check
python .github/scripts/check_environment_baseline.py
```

The workflow pins `actions/setup-python` to CPython `3.12.13`, not a floating
`3.12` patch line.

If the Python patch or a fingerprinted package differs, CI fails before pytest.
A dependency change is therefore explicit repository work rather than an
implicit PyPI resolver change.

## Windows / Codex synchronization

The approved local interpreter remains:

```text
E:\python\python.exe
```

Before changing that shared interpreter, save its current state outside the
repository:

```powershell
E:\python\python.exe -m pip freeze > <external-backup-path>\pip-freeze-before.txt
E:\python\python.exe --version
```

### CPython 3.12.13 provisioning source

Python 3.12.13 is a security-only/source-only CPython release: python.org no
longer publishes Windows binary installers for the 3.12 security-only line.
Therefore Windows verification must not wait for or invent an official 3.12.13
`.exe` installer, and it must not silently substitute 3.12.10 or another patch.

For the Windows verifier only, use this pinned redistributable CPython build:

- upstream: `astral-sh/python-build-standalone`
- immutable release tag: `20260718`
- artifact: `cpython-3.12.13+20260718-x86_64-pc-windows-msvc-install_only.tar.gz`
- SHA-256: `56c9dd9681c4810cb8bfdec277ee2606d8ab17e678e5bc2bd138eb8098e330b6`
- download URL: `https://github.com/astral-sh/python-build-standalone/releases/download/20260718/cpython-3.12.13%2B20260718-x86_64-pc-windows-msvc-install_only.tar.gz`

The selected `x86_64-pc-windows-msvc` distribution is the project's baseline
64-bit Windows verifier runtime. The `install_only` archive is used instead of a
full build-artifact archive. This is an environment-provisioning input only; it
is not an application dependency and must not be vendored into the repository.

Fail closed before replacement if any of these checks fail:

1. the host is not 64-bit Windows;
2. the downloaded artifact hash differs from the pinned SHA-256;
3. extraction does not produce exactly the expected staged `python\python.exe`;
4. the staged interpreter does not report exactly `Python 3.12.13`;
5. the current `E:\python` runtime cannot be backed up/moved safely.

Never disable TLS/certificate verification to obtain the archive. Do not delete
the previous `E:\python` runtime during hardening; keep it as a rollback backup
until HARDENING-001 and TASK-021 verification both pass.

### Replacement and package synchronization

Perform replacement from an external verification/staging directory, not from
`F:\AetherSwap` and not by writing generated files into the repository.
A verifier may use a timestamped staging directory on `E:` so the final runtime
move stays on the same volume.

Reference procedure:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$verificationRoot = 'D:\文档\ChatGPT\AetherSwap改造计划.verification'
$downloadRoot = Join-Path $verificationRoot 'environment-downloads'
$artifactName = 'cpython-3.12.13+20260718-x86_64-pc-windows-msvc-install_only.tar.gz'
$artifactPath = Join-Path $downloadRoot $artifactName
$artifactUrl = 'https://github.com/astral-sh/python-build-standalone/releases/download/20260718/cpython-3.12.13%2B20260718-x86_64-pc-windows-msvc-install_only.tar.gz'
$expectedSha256 = '56c9dd9681c4810cb8bfdec277ee2606d8ab17e678e5bc2bd138eb8098e330b6'
$stageRoot = "E:\python-hardening-stage-$stamp"
$backupRuntime = "E:\python-backup-before-hardening-$stamp"

if (-not [Environment]::Is64BitOperatingSystem) { throw 'WINDOWS_ARCH_MISMATCH' }
New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null
New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null
Invoke-WebRequest -Uri $artifactUrl -OutFile $artifactPath
$actualSha256 = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $expectedSha256) { throw 'PYTHON_ARTIFACT_HASH_MISMATCH' }

tar.exe -xzf $artifactPath -C $stageRoot
$stagedPython = Join-Path $stageRoot 'python\python.exe'
if (-not (Test-Path -LiteralPath $stagedPython -PathType Leaf)) { throw 'PYTHON_ARCHIVE_LAYOUT_MISMATCH' }
$stagedVersion = (& $stagedPython --version 2>&1).ToString().Trim()
if ($stagedVersion -ne 'Python 3.12.13') { throw 'PYTHON_PATCH_MISMATCH' }

if (-not (Test-Path -LiteralPath 'E:\python' -PathType Container)) { throw 'PYTHON_RUNTIME_PATH_MISSING' }
Move-Item -LiteralPath 'E:\python' -Destination $backupRuntime
try {
    Move-Item -LiteralPath (Join-Path $stageRoot 'python') -Destination 'E:\python'
    $installedVersion = (& 'E:\python\python.exe' --version 2>&1).ToString().Trim()
    if ($installedVersion -ne 'Python 3.12.13') { throw 'PYTHON_PATCH_MISMATCH' }
} catch {
    if ((Test-Path -LiteralPath $backupRuntime) -and -not (Test-Path -LiteralPath 'E:\python')) {
        Move-Item -LiteralPath $backupRuntime -Destination 'E:\python'
    }
    throw
}
```

After the exact interpreter is in place, synchronize packages from a fresh
HARDENING-001 checkout:

```powershell
E:\python\python.exe -m pip install --upgrade -r requirements.txt -r requirements-ci.txt -c constraints.txt
E:\python\python.exe -m pip check
E:\python\python.exe .github\scripts\check_environment_baseline.py
```

If package synchronization, `pip check`, the fingerprint gate, or the required
verification suite fails, stop before TASK-021 and report the exact failure. Do
not hide the failure by installing an ad-hoc overlay. Keep the old runtime
backup available for explicit rollback/recovery.

Do not use a temporary Starlette overlay after this baseline is merged. In
particular, the unsupported local combination observed during TASK-021,
FastAPI 0.112.2 + Starlette 1.6.0, must not be treated as the project baseline.

## Updating the baseline later

Do not edit one version opportunistically. A future dependency-baseline change
must:

1. intentionally choose the new versions;
2. update `constraints.txt` and the fingerprint gate together;
3. run the full suite with zero failures/errors/skips;
4. pass the pytest minimum baseline gate;
5. verify both GitHub Actions and the Windows verifier against the same
   baseline;
6. record the reason for the version change.

Application behavior, Auto Offer state contracts, platform transports, and
write permissions are outside this hardening change.
