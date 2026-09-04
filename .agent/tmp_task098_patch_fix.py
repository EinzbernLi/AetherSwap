from pathlib import Path
p = Path('.agent/tmp_task098_late_binding_patch.py')
s = p.read_text(encoding='utf-8')
old = '''s = replace_once(
    s,
    \'\'\'        self._permit = None
        self._active_integration = None
\'\'\',
    \'\'\'        self._permit = None
        self._active_integration = None
        self._captured_integration = None
        self._build_canary_integration = None
\'\'\',
    "takeover init retained integration",
)
'''
new = '''_takeover_state_old = \'\'\'        self._permit = None
        self._active_integration = None
\'\'\'
_takeover_state_new = \'\'\'        self._permit = None
        self._active_integration = None
        self._captured_integration = None
        self._build_canary_integration = None
\'\'\'
if s.count(_takeover_state_old) < 1:
    raise RuntimeError("takeover init retained integration: missing")
s = s.replace(_takeover_state_old, _takeover_state_new, 1)
'''
if s.count(old) != 1:
    raise SystemExit(f'fix anchor count={s.count(old)}')
p.write_text(s.replace(old, new, 1), encoding='utf-8')
print('executor fixed')
