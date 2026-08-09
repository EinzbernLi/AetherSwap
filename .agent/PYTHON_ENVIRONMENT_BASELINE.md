# Reproducible Python Environment Baseline

Issue: #51 (`HARDENING-001`)

This repository uses a frozen dependency baseline so GitHub Actions and the
Windows Codex verifier do not silently resolve incompatible Python/package
combinations over time.

## Why this exists

Historical successful CI proves dependency drift occurred without an intentional
application dependency upgrade:

- TASK-005 / TASK-006: CPython 3.12.13, FastAPI 0.141.1, Starlette 1.4.1,
  pytest 9.1.1;
- TASK-019 / TASK-020: CPython 3.12.13, FastAPI 0.141.1, Starlette 1.6.0,
  pytest 9.1.1.

The Windows verifier was independently observed at CPython 3.12.5 with
FastAPI 0.112.2 + Starlette 1.6.0. That mixed old-direct/new-transitive package
combination broke existing FastAPI/Starlette tests. The root problem is package
resolution drift, not the fact that Windows uses a different CPython 3.12 patch.

`constraints.txt` therefore freezes the concrete dependency versions from the
latest fully passing TASK-020 CI environment.

## Python policy

The project verification policy distinguishes the supported interpreter line
from the canonical CI patch:

- supported interpreter: CPython 3.12.x;
- current Windows verifier: CPython 3.12.5 at `E:\python\python.exe`;
- canonical GitHub Actions verifier: CPython 3.12.13.

Windows verification MUST NOT replace a healthy CPython 3.12.x installation
merely to match the CI patch. A CPython patch difference inside the supported
3.12 minor line is accepted only when the same frozen dependency set and the
full repository test suite pass.

`.python-version` records the canonical CI/tooling patch (`3.12.13`). It is not
an instruction to replace the existing Windows verifier when that verifier is a
supported CPython 3.12.x runtime.

If a verifier is not CPython 3.12.x, fail closed and handle that as a deliberate
interpreter-baseline change rather than silently substituting another runtime.

## Frozen dependency fingerprint

Critical versions are exact:

- FastAPI 0.141.1
- Starlette 1.6.0
- pytest 9.1.1
- Pydantic 2.13.4
- SQLModel 0.0.39
- SQLAlchemy 2.0.51
- requests 2.34.2
- urllib3 2.7.0

`constraints.txt` contains the wider resolved package set from the same
successful TASK-020 CI environment.

## GitHub Actions

CI is the canonical clean-room verifier and pins:

```text
CPython 3.12.13
```

CI must install through the constraint set:

```bash
python -m pip install -r requirements.txt -c constraints.txt
python -m pip install -r requirements-ci.txt -c constraints.txt
python -m pip check
python .github/scripts/check_environment_baseline.py
```

The workflow pins `actions/setup-python` to `3.12.13`, so the CI patch cannot
float over time. The fingerprint gate additionally requires CPython 3.12.x and
all critical package versions above.

## Windows / Codex synchronization

The approved local verifier remains:

```text
E:\python\python.exe
```

For the current machine, CPython 3.12.5 is retained. Do not provision a separate
3.12.13 runtime and do not use a temporary Starlette/PYTHONPATH overlay.

Before changing packages, save the current environment outside the repository:

```powershell
E:\python\python.exe --version
E:\python\python.exe -m pip freeze > <external-backup-path>\pip-freeze-before.txt
```

Require the interpreter to report CPython 3.12.x. Then synchronize only the
packages from a fresh HARDENING-001 checkout:

```powershell
E:\python\python.exe -m pip install --upgrade -r requirements.txt -r requirements-ci.txt -c constraints.txt
E:\python\python.exe -m pip check
E:\python\python.exe .github\scripts\check_environment_baseline.py
```

The expected critical Windows fingerprint after synchronization is therefore:

```text
Python      3.12.5  (supported CPython 3.12.x)
FastAPI     0.141.1
Starlette   1.6.0
pytest      9.1.1
Pydantic    2.13.4
SQLModel    0.0.39
SQLAlchemy  2.0.51
requests    2.34.2
urllib3     2.7.0
```

A later Windows 3.12 patch is acceptable only under the same rules: it must be
CPython 3.12.x, install the exact constrained dependency set, pass `pip check`,
pass the fingerprint gate, and pass the full test suite.

## Verification acceptance

A dependency baseline is accepted only when both environments are green:

1. canonical GitHub Actions CPython 3.12.13 clean install;
2. Windows Codex CPython 3.12.x using the same `constraints.txt`;
3. `pip check` succeeds;
4. environment fingerprint succeeds;
5. full pytest has zero failures/errors/collection errors/skips;
6. the pytest minimum baseline gate passes.

For TASK-021 specifically, the current Windows CPython 3.12.5 verifier must also
pass the TASK-021 target, historical regression, full suite, and baseline gate
without an overlay.

## Updating the baseline later

Do not edit one version opportunistically. A future dependency or interpreter
baseline change must:

1. intentionally choose the new supported Python minor/canonical CI patch or
   dependency versions;
2. update `constraints.txt`, workflow policy, and fingerprint gate together as
   applicable;
3. run the full suite with zero failures/errors/skips;
4. pass the pytest minimum baseline gate;
5. verify GitHub Actions and Windows against the documented policy;
6. record the reason for the version change.

Application behavior, Auto Offer state contracts, platform transports, and
write permissions are outside this hardening change.
