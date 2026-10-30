"""Independent native-trial deadline/owner-death cleanup. No model requests."""
import builtins
import json
import os
from pathlib import Path
import sys
import time
import types
from adapter_contracts import require
from compiler_adapter import pinned_sources


def main(root):
    root=Path(root);sources=pinned_sources()
    common=types.ModuleType('watchdog_private_common')
    common.__dict__.update(__file__=str(Path(__file__).with_name('common.py')),__builtins__=dict(vars(builtins)))
    exec(compile(sources['common.py'],common.__file__,'exec'),common.__dict__)
    common.ROOT=root;common.REG=root/'processes'
    owner=common.read(common.REG/'supervisor.json');require(common.same(owner),'owner missing before watchdog ready')
    common.own('watchdog',os.getpid())
    common.save(root/'watchdog-ready.json',{'identity':common.ident(os.getpid()),'owner':owner,'epoch':time.time()})
    reason=None
    while reason is None:
        common.capture_owned()
        if not common.same(owner):reason='owner_nonrunning'
        elif (root/'watchdog-stop.json').exists():reason='requested_stop'
        elif (root/'monitor-failed.json').exists():reason='owner_monitor_failed'
        else:time.sleep(.5)
    # All known non-owner/non-watchdog resources are exclusive to this trial.
    for _ in range(3):
        rows=common.capture_owned()
        targets=[q for q in rows if q['pid'] not in (os.getpid(),owner['pid']) and common.same(q)]
        common.stop_rows(targets,.2)
        time.sleep(.1)
    rows=common.capture_owned()
    live=[q for q in rows if q['pid'] not in (os.getpid(),owner['pid']) and common.same(q)]
    common.save(root/'watchdog-ended.json',{'identity':common.ident(os.getpid()),'owner':owner,
        'reason':reason,'remaining_live':live,'cleanup_verified':not live,'epoch':time.time(),
        'scope':'all registered and captured owned identities; not a claim about unseen processes',
        'normal_owner_end':reason=='requested_stop','model_requests':0})
    return 0 if not live else 1


if __name__=='__main__':raise SystemExit(main(sys.argv[1]))
