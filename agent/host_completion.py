"""Completion accounting component; no launch, kill, network, filesystem or grading.

The caller owns process identity, bounded termination and native descendant proofs.
An observed exit time is an upper bound from wait(), not a kernel exit timestamp.
"""
from dataclasses import dataclass
import math
import re
import threading
import time


@dataclass(frozen=True)
class Identity:
    pid: int
    start_ticks: str
    boot_id: str

    def __post_init__(self):
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError('positive integer PID required')
        if not isinstance(self.start_ticks, str) or not self.start_ticks.isdecimal():
            raise ValueError('native start ticks required')
        if not isinstance(self.boot_id, str) or not self.boot_id:
            raise ValueError('boot identity required')


@dataclass(frozen=True)
class ExitObservation:
    identity: Identity
    returncode: int | None
    observed_at: float | None
    error_type: str | None = None


@dataclass(frozen=True)
class ResourceClosure:
    completed_at: float
    complete: bool
    live_or_unknown: tuple
    identity_snapshot_sha256: str


class ExitObserver:
    """Wait independently of registry snapshots; caller must join before finalizing.

    A timeout never kills anything here. The owner must stop only the matched
    process, close its descendants, and join this observer using bounded waits.
    """
    def __init__(self, process, identity, clock=time.monotonic):
        if type(identity) is not Identity:
            raise ValueError('validated process Identity required')
        self.process = process
        self.identity = identity
        self.clock = clock
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._observation = None
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError('observer already started')
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        try:
            code = self.process.wait()
            observed = self.clock()
            if type(code) is not int or not _finite(observed):
                raise ValueError('invalid process wait observation')
            value = ExitObservation(self.identity, code, observed)
        except BaseException as error:
            value = ExitObservation(self.identity, None, None, type(error).__name__)
        with self._lock:
            self._observation = value
        self._event.set()

    def snapshot(self):
        with self._lock:
            return self._observation

    def wait(self, timeout):
        _timeout(timeout)
        if self._thread is None:
            raise RuntimeError('observer not started')
        self._event.wait(timeout)
        return self.snapshot()

    def join(self, timeout):
        _timeout(timeout)
        if self._thread is None:
            raise RuntimeError('observer not started')
        self._thread.join(timeout)
        return not self._thread.is_alive()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _timeout(value):
    if not _finite(value) or value < 0:
        raise ValueError('finite nonnegative timeout required')


def assess_completion(expected, observation, *, started_at, process_deadline,
                      phase_deadline, closure, metadata_finished_at,
                      observer_joined, termination_requested=False):
    """Return timing/closure eligibility only, never pass/fail a DUT.

    Native identity-snapshot contents must already be independently validated by
    the adapter. A SHA string here is a reference, not proof of those contents.
    """
    issues = []
    process_status = 'unknown'
    process_elapsed = None
    if type(expected) is not Identity:
        issues.append('expected_identity_missing_or_invalid')
    if not all(_finite(x) for x in (started_at, process_deadline, phase_deadline, metadata_finished_at)):
        issues.append('nonfinite_or_missing_time')
    elif not started_at <= process_deadline <= phase_deadline:
        issues.append('invalid_deadline_order')
    if observation is None:
        issues.append('exit_observation_missing')
    elif type(observation) is not ExitObservation or type(observation.identity) is not Identity:
        issues.append('exit_identity_missing_or_invalid')
    elif observation.identity != expected:
        issues.append('exit_identity_mismatch')
    elif observation.error_type or type(observation.returncode) is not int or not _finite(observation.observed_at):
        issues.append('exit_observation_invalid')
    elif _finite(started_at) and _finite(process_deadline):
        process_elapsed = observation.observed_at - started_at
        if process_elapsed < 0:
            issues.append('exit_precedes_launch')
        elif observation.observed_at > process_deadline:
            process_status = 'observed_after_process_deadline'
            issues.append('process_deadline_not_proven')
        elif observation.returncode < 0:
            process_status = 'signal_termination'
            issues.append('signal_termination')
        else:
            process_status = 'observed_exit_within_process_deadline'
    if observer_joined is not True:
        issues.append('observer_not_joined')
    if termination_requested is not False:
        issues.append('termination_requested')
    closure_valid = (type(closure) is ResourceClosure and closure.complete is True
                     and closure.live_or_unknown == ()
                     and isinstance(closure.identity_snapshot_sha256, str)
                     and re.fullmatch(r'[0-9a-f]{64}', closure.identity_snapshot_sha256) is not None
                     and _finite(closure.completed_at))
    if not closure_valid:
        issues.append('resource_closure_missing_or_unknown')
    elif (type(observation) is not ExitObservation or not _finite(observation.observed_at)
          or closure.completed_at < observation.observed_at):
        issues.append('closure_not_after_observed_exit')
    if closure_valid and _finite(metadata_finished_at) and metadata_finished_at < closure.completed_at:
        issues.append('metadata_precedes_closure')
    if _finite(metadata_finished_at) and _finite(phase_deadline) and metadata_finished_at > phase_deadline:
        issues.append('full_phase_budget_exceeded')
    return {
        'process_status': process_status,
        'process_observed_elapsed_s': process_elapsed,
        'phase_elapsed_s': metadata_finished_at-started_at if _finite(metadata_finished_at) and _finite(started_at) else None,
        'verdict_eligible': not issues and process_status == 'observed_exit_within_process_deadline',
        'issues': issues,
        'scope': 'timing and resource-closure eligibility; not compile, functional, native-adapter or model acceptance',
    }
