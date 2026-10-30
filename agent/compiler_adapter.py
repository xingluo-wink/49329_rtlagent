"""Compiler bridge with exact source, diagnostics and process-exit binding."""
import builtins
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import types
from adapter_contracts import BoundaryError, CompileReply, require, finite
from prompt_builder import canonical, digest
from rtl_delivery import inspect_body, strict_json
import host_completion
from phase_timing import PhaseTimings
from source_advisories import inspect_source
from source_policy import source_allowed

PINS = {'common.py':'db4811f8e1b0f04c96cf74874ac40bdc30a6f2d0e96ddfbf83c7191b5e4882cb',
        'eda_runner.py':'7a5d5a94b4df7b0cad310793e380f6e511306d1c8758737cccce3c7b2ee6284d',
        'host_completion.py':'9c1f64a0d2e582a1f55d401ebff960ec5f10fa284e5d5357a1c9e4881092a58a'}
ARGV = ['/tools/Xilinx/2026.1/Vivado/bin/xvlog','--sv','dut.sv']


def pinned_sources():
    root = Path(__file__).resolve().parent
    found = {}
    for name, expected in PINS.items():
        path = root/name
        require(not path.is_symlink(), 'linked R1 source rejected')
        raw = path.read_bytes()
        require(digest(raw) == expected, 'R1 source fingerprint mismatch: '+name)
        found[name] = raw
    require(Path(host_completion.__file__).resolve() == root/'host_completion.py',
            'observer imported from another candidate')
    return found


def isolated_eda(common, clock, sources=None):
    """Execute pinned R1 source with a module-local import hook, not sys.modules.

    Tests may supply a synthetic common facade. The R1 command and completion
    accounting remain the actual reviewed source. stdlib modules are not edited.
    """
    sources = sources or pinned_sources()
    require(type(common) is types.ModuleType, 'private common module required')
    scope = types.ModuleType('private_eda')
    real_import = builtins.__import__
    def local_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'common' and level == 0:
            return common
        return real_import(name, globals, locals, fromlist, level)
    scope.__dict__.update(__file__=str(Path(__file__).with_name('eda_runner.py')),
                          __builtins__={**vars(builtins), '__import__':local_import})
    exec(compile(sources['eda_runner.py'], scope.__file__, 'exec'), scope.__dict__)
    # The original observer's default argument binds time.monotonic at import;
    # always make the selected clock explicit, without changing its source.
    scope.ExitObserver = lambda process, identity: host_completion.ExitObserver(
        process, identity, clock=clock.monotonic)
    return scope


class R1Backend:
    """One private registry/namespace for one compile stage in a serial worker."""
    def __init__(self, common, clock, logical_cwd, *, synthetic=False):
        self.common = common
        self.clock = clock
        self.logical_cwd = logical_cwd
        self.synthetic = synthetic
        self.captures = []
        self.invoked = False
        self.timings = None
        # The original pinned common.py stays unchanged. Instrument only this
        # backend's private namespace; never edit the shared fcntl module.
        self.registry_timing_probes = {
            'aggregate_capture_installed': True,
            'lock_call_installed': False,
            'snapshot_scan_installed': False,
            'snapshot_publication_installed': False,
            'installation_is_not_proof_a_probe_was_observed': True,
            'nested_intervals_must_not_be_summed': True,
            'no_lock_scan_or_publication_is_skipped': True,
            'performance_benefit_verified': False,
        }
        def measured(name, callback, *args, **kwargs):
            if self.timings is None:
                return callback(*args, **kwargs)
            with self.timings.span(name):
                return callback(*args, **kwargs)
        lock_module = getattr(common, 'fcntl', None)
        if lock_module is not None and callable(getattr(lock_module, 'flock', None)):
            original_flock = lock_module.flock
            facade = types.SimpleNamespace(**vars(lock_module))
            def flock(*args, **kwargs):
                return measured('registry_flock_call_wait', original_flock, *args, **kwargs)
            facade.flock = flock
            common.fcntl = facade
            self.registry_timing_probes['lock_call_installed'] = True
        original_snapshot = getattr(common, 'snapshot_owned', None)
        if callable(original_snapshot):
            def snapshot(*args, **kwargs):
                return measured('registry_snapshot_read_scan_lineage', original_snapshot, *args, **kwargs)
            common.snapshot_owned = snapshot
            self.registry_timing_probes['snapshot_scan_installed'] = True
        original_save = common.save
        def save(path, value):
            if Path(path) == common.ROOT/'process-descendants.json':
                return measured('registry_snapshot_publish_json_flush_fsync_replace',
                                original_save, path, value)
            return original_save(path, value)
        common.save = save
        self.registry_timing_probes['snapshot_publication_installed'] = True
        original_capture = common.capture_owned
        def capture(*args, **kwargs):
            if self.timings is None:
                rows = original_capture(*args, **kwargs)
            else:
                with self.timings.span('registry_capture_including_lock_scan_publish'):
                    rows = original_capture(*args, **kwargs)
            self.captures.append(json.loads(json.dumps(rows)))
            return rows
        # Only this private namespace is changed. Functions imported by the R1
        # module also retain this namespace for registry, stop and cleanup.
        common.capture_owned = capture
        self.module = isolated_eda(common, clock)
        require(self.module.ROOT == common.ROOT and self.module.REG == common.REG,
                'R1 wildcard imports captured a different registry root')

    def invoke(self, folder, context, permit, phase_end):
        require(not self.invoked, 'R1 backend cannot be replayed')
        self.invoked = True
        original = self.module.subprocess
        request_bytes = canonical(context)
        def popen(argv, **kwargs):
            require(argv == ARGV and Path(kwargs.get('cwd')).resolve() == Path(folder).resolve(),
                    'compiler effect changed argv or cwd')
            if self.clock.monotonic() >= phase_end:
                raise TimeoutError('tool phase expired before permit')
            permit.enter(request_bytes)
            if self.clock.monotonic() >= phase_end:
                raise TimeoutError('tool phase expired during permit publication')
            return original.Popen(argv, **kwargs)
        # Do not mutate common.subprocess or the global subprocess module.
        self.module.subprocess = types.SimpleNamespace(Popen=popen, DEVNULL=original.DEVNULL,
                                                       STDOUT=original.STDOUT)
        return self.module.command(ARGV, folder, 30, 'compiler', phase_deadline=phase_end)


def native_backend(registry_root, folder, context, clock=time, *, allow_native=False):
    """Not called during offline validation. No automatic native activation."""
    require(not Path(__file__).with_name('offline-only.json').exists(), 'R7 native compiler disabled')
    require(allow_native, 'native compiler execution is disabled pending runtime acceptance')
    require(sys.platform == 'linux' and threading.active_count() == 1,
            'a dedicated single-threaded Linux job worker is required')
    sources = pinned_sources()
    common = types.ModuleType('private_compiler_common')
    common.__dict__.update(__file__=str(Path(__file__).with_name('common.py')),
                           __builtins__=dict(vars(builtins)))
    exec(compile(sources['common.py'], common.__file__, 'exec'), common.__dict__)
    registry_root = Path(registry_root)
    registry_root.mkdir(exist_ok=False)
    (registry_root/'processes').mkdir()
    # These must be set BEFORE executing 'from common import *' in R1.
    common.ROOT, common.REG, common.time = registry_root, registry_root/'processes', clock
    current = common.ident(os.getpid())
    domain = context['clock_domain']
    require(current is not None and all(current[k] == domain[k] for k in ('pid','start_ticks','boot_id')),
            'native worker clock identity mismatch')
    require(common.BOOT == domain['boot_id'], 'native worker boot changed')
    children = [row for row in common.descendants(current) if row['pid'] != current['pid'] and common.same(row)]
    require(not children, 'dedicated compiler worker has residual children')
    common.become_subreaper()
    common.own('supervisor', os.getpid())
    # Kernel adoption belongs to this dedicated worker, not the Python namespace.
    return R1Backend(common, clock, Path(folder).resolve().as_posix())


class CompilerBridge:
    def __init__(self, backend_factory=None, clock=time):
        self.clock = clock
        self.factory = backend_factory or native_backend

    def compile(self, *, source, body, artifact_dir, context, permit):
        started = self.clock.monotonic()
        timings = PhaseTimings(self.clock)
        root = Path(artifact_dir)
        phase_end = min(started+90, context['deadline_monotonic'])
        def publish_timing():
            raw = canonical({'context':context,'phase_started_at_monotonic':started,
                'phase_deadline_monotonic':phase_end,'observations':timings.snapshot(),
                'timing_only_not_verdict':True})
            with (root/'phase-timing.json').open('xb') as f:
                f.write(raw);f.flush();os.fsync(f.fileno())
        try:
            with timings.span('compiler_bridge_before_timing_publication'):
                reply = self._compile(source=source, body=body, artifact_dir=artifact_dir,
                    context=context, permit=permit, started=started, timings=timings)
        except BaseException:
            # Keep the original exception. A timing failure cannot fabricate a
            # successful tool verdict or a complete process-closure record.
            try: publish_timing()
            except (OSError,ValueError,TypeError): pass
            raise
        publish_timing()
        if self.clock.monotonic() >= phase_end:
            raise TimeoutError('timing publication exceeded original tool phase')
        return reply

    def _compile(self, *, source, body, artifact_dir, context, permit, started, timings):
        chain_end = context['deadline_monotonic']
        require(finite(started) and finite(chain_end), 'finite tool/chain clock required')
        end = min(started+90, chain_end)
        root = Path(artifact_dir)
        require(root.is_dir() and not root.is_symlink(), 'dedicated artifact directory required')
        def write(name, raw):
            path = root/name
            with timings.span('bridge_artifact_publish'):
                with path.open('xb') as fh:
                    fh.write(raw); fh.flush(); os.fsync(fh.fileno())
            return {'path':name,'sha256':digest(raw),'bytes':len(raw)}
        def now():
            value = self.clock.monotonic()
            require(finite(value) and value >= started, 'tool clock regressed')
            if value >= end:
                raise TimeoutError('absolute tool phase deadline reached')
            return value
        write('bridge-intent.json', canonical({'context':context,'phase_started_at_monotonic':started,
              'phase_deadline_monotonic':end,'no_replay':True,'native_runtime_accepted':False}))
        now()
        require(type(source) is bytes and type(body) is bytes
                and inspect_body(body).candidate == source
                and context['source_sha256'] == digest(source)
                and context['body_sha256'] == digest(body), 'compiler input/body binding mismatch')
        src_ref = write('source.sv',source)
        body_ref = write('body.bin',body)
        # Observations are retained for review; they never admit repair, select a
        # candidate or substitute for the original compiler's executed verdict.
        write('source-advisories.json', canonical(inspect_source(source)))
        work = root/'work';work.mkdir()
        dut_ref = write('work/dut.sv',source)
        with timings.span('backend_prepare'):
            backend = self.factory(root/'registry', work, context, self.clock)
        backend.timings = timings
        write('registry-timing-probes.json', canonical(backend.registry_timing_probes))
        now()
        policy = source_allowed(source.decode('utf-8'))
        write('policy.json',canonical({'allowed':policy,'source_sha256':digest(source),
              'public_fields_sha256':context['public_fields_sha256'],'policy_source_sha256':digest(Path(__file__).with_name('source_policy.py').read_bytes()),
              'not_full_SystemVerilog_support':True}))
        if not policy:
            write('bridge-status.json',canonical({'status':'policy_rejected','executed':False,
                  'new_model_request_allowed':False,'reason':'R1 conservative source policy; not a syntax verdict'}))
            raise BoundaryError('policy_rejected; no syntax rescue')
        now()
        try:
            with timings.span('R1_command_including_registry_wait_and_receipts'):
                row, ignored_text = backend.invoke(work, context, permit, end)
            # Keep originals returned by the R1 native command, including failure
            # and emergency records. Do not construct a normal row from a flag.
            write('returned-step.json',canonical(row))
            require((work/'compiler.json').is_file(), 'R1 original step receipt missing')
            original_raw = (work/'compiler.json').read_bytes()
            require(strict_json(original_raw) == row, 'R1 returned and original step receipts disagree')
            original_ref = write('original-step.json',original_raw)
            log = (work/'compiler.log').read_bytes()
            log_ref = write('original-compiler.log',log)
            require(row.get('executed') is True and permit.started is not None,
                    'compiler did not execute through the dispatch permit')
            require(row.get('completion_accounting',{}).get('verdict_eligible') is True
                    and row.get('observer_joined') is True and row.get('remaining_live') == []
                    and not row.get('resource_closure_unknown')
                    and not row.get('late_receipt_publication'), 'R1 completion is unknown')
            require(backend.captures, 'R1 final captured rows missing')
            rows_raw = json.dumps(backend.captures[-1],sort_keys=True).encode()
            require(digest(rows_raw) == row['resource_snapshot_sha256'],
                    'original R1 final rows do not match; no replacement scan allowed')
            rows_ref = write('original-final-rows.json',rows_raw)
            rc = row.get('returncode')
            require(type(rc) is int and rc >= 0, 'compiler signal/unknown exit')
            if re.search(rb'license|cannot execute|command not found|segmentation|internal error|No such file|Permission denied|out of memory|resource temporarily|killed',log,re.I):
                raise BoundaryError('compiler infrastructure log, not syntax')
            now()
            resource = {'schema':'r1-resource-envelope-v1','request_id':context['request_id'],
                'clock_domain':context['clock_domain'],'registered_identities':backend.captures[-1],
                'remaining_live':row['remaining_live'],'observer_joined':row['observer_joined'],
                'scope':'R1 tool processes and descendants; the dedicated supervisor worker is excluded',
                'original_rows_sha256':digest(rows_raw),'original_step_sha256':digest(original_raw)}
            resources_raw = canonical(resource)
            resource_ref = write('resources.json',resources_raw)
            derived = dict(row)
            require(not {'cwd','r1_original_step_sha256','r1_original_rows_sha256'} & set(derived),
                    'R1 original row already contains derived fields')
            derived.update(cwd=backend.logical_cwd,resource_snapshot_sha256=digest(resources_raw),
                           r1_original_step_sha256=digest(original_raw),r1_original_rows_sha256=digest(rows_raw))
            step_ref = write('bound-step.json',canonical(derived))
            result = {'started':started,'closed':now(),'time_basis':'monotonic','source_sha256':digest(source),
                'staged_dut_sha256':digest((work/'dut.sv').read_bytes()),'compile_executed':True,
                'simulation_executed':False,'steps':[derived], 'status':'compile_pass' if rc==0 else 'compile_fail'}
            result_ref = write('bound-result.json',canonical(result))
            manifest = {'scope':'trusted_host_public_compiler','public_fields_sha256':context['public_fields_sha256'],
                'compiler_dut_path':backend.logical_cwd+'/dut.sv','source':src_ref,'body':body_ref,
                'staged_dut':dut_ref,'result':result_ref,'steps':[{'record':step_ref,'log':log_ref}]}
            manifest_raw = canonical(manifest)
            write('bridge-manifest.json',manifest_raw)
            attestation = {'schema':'fresh-compiler-attestation-v1','request_id':context['request_id'],
                'clock_domain':context['clock_domain'],'public_fields_sha256':context['public_fields_sha256'],
                'source_sha256':digest(source),'body_sha256':digest(body),'manifest_sha256':digest(manifest_raw),
                'started_at_monotonic':permit.started,'phase_started_at_monotonic':started,
                'phase_deadline_monotonic':end,'closed_at_monotonic':now(),'deadline_monotonic':chain_end,
                'cwd':backend.logical_cwd,'policy_status':'allowed','policy_source_sha256':digest(source),
                'policy_public_sha256':context['public_fields_sha256'],'resource_snapshot':resource_ref,
                'r1_derivation':{'original_step':original_ref,'original_rows':rows_ref,
                                 'original_log':log_ref,'source_pins':PINS},
                'evidence_scope':'synthetic' if backend.synthetic else 'unaccepted_native_adapter'}
            att_raw = canonical(attestation)
            write('bridge-attestation.json',att_raw)
            now()  # Wrapper publication is part of the tool phase.
            return CompileReply(manifest_raw,att_raw)
        except BaseException as error:
            # R1 owns post-Popen identity-qualified cleanup. Do not upgrade its
            # emergency record to normal completion or send an unbound PID signal.
            status = {'status':'tool_unknown','error_type':type(error).__name__,
                      'permit_used':permit.used,'dispatch_at':permit.started,
                      'phase_started_at_monotonic':started,'phase_deadline_monotonic':end,
                      'original_artifacts_retained':True,'normal_closure_asserted':False}
            try:write('bridge-status.json',canonical(status))
            except OSError:pass
            raise


class CompilerConnectedAdapters:
    """Connect the real compiler bridge into the R3 four-method adapter interface.

    No default model transport is supplied and no native service is started.
    """
    def __init__(self, model_adapter, compiler_bridge):
        self.model = model_adapter
        self.compiler = compiler_bridge
    def preflight(self, **kw):return self.model.preflight(**kw)
    def tokenize(self, **kw):return self.model.tokenize(**kw)
    def generate(self, **kw):return self.model.generate(**kw)
    def compile(self, **kw):return self.compiler.compile(**kw)
