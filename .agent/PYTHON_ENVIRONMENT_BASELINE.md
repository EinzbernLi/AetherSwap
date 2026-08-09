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

The interpreter itself must be CPython 3.12.13. If it is a different Python
patch, replace/reinstall that interpreter deliberately rather than pretending a
package-only change makes the environment identical.

Once the interpreter is 3.12.13, synchronize packages from a clean checkout:

```powershell
E:\python\python.exe -m pip install --upgrade -r requirements.txt -r requirements-ci.txt -c constraints.txt
E:\python\python.exe -m pip check
E:\python\python.exe .github\scripts\check_environment_baseline.py
```

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
