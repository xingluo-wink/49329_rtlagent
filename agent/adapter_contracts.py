"""Offline adapter boundary. Integrity checks do not authenticate a native host.

There are deliberately no HTTP, subprocess, tokenizer or compiler implementations.
Adapters must retain raw transport/step records, use the passed one-shot permit
immediately before the effect, and implement cancellation/closure themselves.
"""
from dataclasses import dataclass
import math
import re
from pathlib import PurePosixPath
from typing import Protocol
from compiler_repair import ArtifactReader, bind_compile_failure, _identity
from prompt_builder import canonical, digest
from rtl_delivery import strict_json

RUNTIME = '51d3bd1a685ca79f64259de6f9cab5cbfd7d3f3d69b99bc27fea3e207c947669'
TEMPLATE = '55d4931433fe502b794226ee7f4d206a6bdd436ac9f80eb7d8ebb4c639f9ea0c'
WEIGHT = 'ac0e2c1189e055faa36eff361580e79c5bd6f8e76bffb4ce547f167d53e31a61'
MODEL = 'qwen36-35b-a3b'


class BoundaryError(ValueError):
    pass


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def require(ok, message):
    if not ok:
        raise BoundaryError(message)


def object_json(raw):
    require(type(raw) is bytes, 'adapter records must be original JSON bytes')
    row = strict_json(raw)
    require(type(row) is dict, 'adapter record must be an object')
    return row


@dataclass(frozen=True)
class ModelReply:
    body: bytes
    stream: bytes
    receipt: bytes


@dataclass(frozen=True)
class CompileReply:
    manifest: bytes
    attestation: bytes


class HostAdapters(Protocol):
    def preflight(self, *, context: dict) -> bytes: ...
    def tokenize(self, *, messages: bytes, generation: bytes, context: dict) -> bytes: ...
    def generate(self, *, payload: bytes, context: dict, permit) -> ModelReply: ...
    def compile(self, *, source: bytes, body: bytes, artifact_dir, context: dict,
                permit) -> CompileReply: ...


class UnconfiguredAdapters:
    def preflight(self, **unused):
        raise BoundaryError('native adapters are not supplied or accepted')


def runtime_receipt(raw, context):
    row = object_json(raw)
    require(row.get('schema') == 'host-runtime-evidence-v1'
            and row.get('run_id') == context['run_id']
            and row.get('clock_domain') == context['clock_domain']
            and row.get('runtime_sha256') == RUNTIME
            and row.get('template_sha256') == TEMPLATE
            and row.get('weight_sha256') == WEIGHT
            and row.get('context_tokens') == 32768
            and row.get('slots') == 1
            and row.get('automatic_retries') == 0, 'runtime binding mismatch')
    require(row.get('evidence_scope') in ('synthetic', 'trusted_native_adapter'),
            'adapter provenance must be explicit')
    return row


def token_receipt(raw, messages, generation, context):
    row = object_json(raw)
    require(row.get('schema') == 'template-token-evidence-v1'
            and row.get('request_id') == context['request_id']
            and row.get('clock_domain') == context['clock_domain']
            and row.get('runtime_sha256') == RUNTIME
            and row.get('template_sha256') == TEMPLATE
            and row.get('messages_sha256') == digest(messages)
            and row.get('generation_sha256') == digest(generation)
            and row.get('context_tokens') == 32768
            and row.get('method') == 'maximum_actual_token_count_no_clipping',
            'token count must bind exact messages, generation and runtime')
    counts = row.get('special_mode_counts')
    require(type(counts) is list and len(counts) == 2
            and all(type(n) is int and n > 0 for n in counts), 'two exact token counts required')
    require(type(row.get('rendered_prompt_sha256')) is str
            and re.fullmatch('[0-9a-f]{64}', row['rendered_prompt_sha256']) is not None,
            'rendered template fingerprint required')
    return max(counts)


def model_receipt(reply, payload, context, start, now, prompt_upper):
    require(type(reply) is ModelReply and type(reply.body) is bytes
            and type(reply.stream) is bytes, 'original body and stream bytes required')
    row = object_json(reply.receipt)
    require(row.get('schema') == 'model-request-closure-v1'
            and row.get('request_id') == context['request_id']
            and row.get('clock_domain') == context['clock_domain']
            and row.get('runtime_sha256') == RUNTIME
            and row.get('request_sha256') == digest(payload)
            and row.get('body_sha256') == digest(reply.body)
            and row.get('stream_sha256') == digest(reply.stream), 'response binding mismatch')
    require(type(row.get('http_status')) is int and row['http_status'] == 200
            and type(row.get('posts')) is int and row['posts'] == 1
            and row.get('automatic_retries') == 0
            and all(row.get(k) is True for k in ('wire_complete', 'transport_closed', 'backend_idle'))
            and row.get('inflight_requests') == 0, 'request closure is unknown')
    closed = row.get('closed_at_monotonic')
    require(finite(closed) and row.get('started_at_monotonic') == start
            and start <= closed <= now <= context['deadline_monotonic'], 'request completed late or in another clock domain')
    usage = row.get('usage')
    if usage is not None:
        require(type(usage) is dict and set(usage) == {'prompt_tokens', 'completion_tokens'}
                and all(type(n) is int and n >= 0 for n in usage.values()), 'invalid usage evidence')
        request = object_json(payload)
        require(usage['completion_tokens'] <= request['max_tokens']
                and usage['prompt_tokens'] <= prompt_upper
                and usage['prompt_tokens'] + usage['completion_tokens'] + 128 <= 32768,
                'reported usage contradicts the admitted token budget')
    return row


def compiler_receipt(reply, root, source, body, context, start, now):
    """Fresh syntax-only step binding. No testbench, simulation or interface proof.

The host attestation is not a signature. The adapter must establish actual cwd,
policy enforcement, process identities, observer join and descendants natively.
"""
    require(type(reply) is CompileReply, 'compile reply type')
    manifest = object_json(reply.manifest)
    att = object_json(reply.attestation)
    require(set(manifest) == {'scope','public_fields_sha256','compiler_dut_path',
                              'source','body','staged_dut','result','steps'},
            'complete compiler manifest required for pass and failure')
    dut_location = manifest['compiler_dut_path']
    require(type(dut_location) is str, 'compiler DUT path required')
    dut_path = PurePosixPath(dut_location)
    require(dut_path.is_absolute() and dut_path.as_posix() == dut_location
            and dut_path.name == 'dut.sv' and '..' not in dut_path.parts
            and not any(ch in dut_location for ch in ('\\','\r','\n')),
            'canonical absolute POSIX DUT path required')
    require(att.get('schema') == 'fresh-compiler-attestation-v1'
            and att.get('request_id') == context['request_id']
            and att.get('clock_domain') == context['clock_domain']
            and att.get('public_fields_sha256') == context['public_fields_sha256']
            and att.get('source_sha256') == digest(source)
            and att.get('body_sha256') == digest(body)
            and att.get('manifest_sha256') == digest(reply.manifest)
            and att.get('started_at_monotonic') == start
            and att.get('deadline_monotonic') == context['deadline_monotonic'], 'compiler stage is stale or cross-bound')
    closed = att.get('closed_at_monotonic')
    phase_start = att.get('phase_started_at_monotonic')
    phase_end = att.get('phase_deadline_monotonic')
    require(all(finite(v) for v in (closed, phase_start, phase_end))
            and phase_start <= start <= closed <= now <= phase_end <= context['deadline_monotonic']
            and phase_end == min(phase_start+90, context['deadline_monotonic']),
            'compiler phase/dispatch/chain deadline mismatch')
    require(att.get('policy_status') == 'allowed'
            and att.get('policy_source_sha256') == digest(source)
            and att.get('policy_public_sha256') == context['public_fields_sha256'], 'source policy not bound or rejected')
    require(manifest.get('scope') == 'trusted_host_public_compiler'
            and manifest.get('public_fields_sha256') == context['public_fields_sha256'], 'compiler public origin mismatch')
    reader = ArtifactReader(root)
    require(reader.read(manifest['source'], 200000) == source
            and reader.read(manifest['staged_dut'], 200000) == source
            and reader.read(manifest['body'], 1048576) == body, 'source/body/staged DUT mismatch')
    result = object_json(reader.read(manifest['result'], 2097152))
    require(result.get('source_sha256') == digest(source)
            and result.get('staged_dut_sha256') == digest(source)
            and result.get('compile_executed') is True
            and result.get('simulation_executed') is False
            and finite(result.get('started')) and finite(result.get('closed'))
            and result['started'] <= result['closed']
            and result.get('late_receipt_publication') is not True, 'not a completed compiler stage')
    require(type(result.get('steps')) is list and len(result['steps']) == 1
            and type(manifest.get('steps')) is list and len(manifest['steps']) == 1,
            'formal compile scope is one xvlog frontend step, without a testbench')
    step = result['steps'][0]
    require(att.get('cwd') == step.get('cwd') == dut_path.parent.as_posix(),
            'declared execution cwd and staged DUT location disagree')
    refs = manifest['steps'][0]
    require(object_json(reader.read(refs['record'], 2097152)) == step, 'original step differs from summary')
    log = reader.read(refs['log'], 2097152)
    require(step.get('argv') == ['/tools/Xilinx/2026.1/Vivado/bin/xvlog', '--sv', 'dut.sv']
            and step.get('executed') is True and step.get('timed_out') is False
            and step.get('observer_joined') is True and step.get('remaining_live') == []
            and step.get('resource_closure_unknown') is not True
            and step.get('late_receipt_publication') is not True
            and step.get('log_sha256') == digest(log) and step.get('log_bytes') == len(log), 'compiler step incomplete')
    identity = _identity(step.get('identity'))
    require(identity[2] == context['clock_domain']['boot_id'], 'compiler identity belongs to another boot')
    exitrow = step.get('exit_observation', {})
    rc = step.get('returncode')
    observed = exitrow.get('observed_at_monotonic')
    step_start = step.get('started_at_monotonic')
    step_end = step.get('process_deadline_monotonic')
    accounting = step.get('completion_accounting', {})
    require(type(rc) is int and rc >= 0 and _identity(exitrow.get('identity')) == identity
            and type(exitrow.get('returncode')) is int and exitrow['returncode'] == rc
            and exitrow.get('error_type') is None
            and all(finite(v) for v in (step_start, step_end, observed))
            and phase_start <= step_start <= start <= observed <= step_end <= phase_end
            and observed <= closed
            and step.get('phase_deadline_monotonic') == phase_end
            and accounting.get('verdict_eligible') is True and accounting.get('issues') == []
            and accounting.get('process_status') == 'observed_exit_within_process_deadline'
            and finite(accounting.get('process_observed_elapsed_s'))
            and abs(accounting['process_observed_elapsed_s'] - (observed-step_start)) < 1e-6
            and finite(accounting.get('phase_elapsed_s'))
            and observed-step_start <= accounting['phase_elapsed_s'] <= closed-step_start+1e-6,
            'fresh identity/exit/deadline accounting mismatch')
    require(type(step.get('resource_snapshot_sha256')) is str
            and re.fullmatch('[0-9a-f]{64}', step['resource_snapshot_sha256']) is not None,
            'resource snapshot digest required')
    snapshot_raw = reader.read(att['resource_snapshot'], 2097152)
    snapshot = object_json(snapshot_raw)
    derivation = att.get('r1_derivation')
    require(type(derivation) is dict and set(derivation) == {'original_step','original_rows','original_log','source_pins'},
            'R1 original derivation references required')
    from compiler_adapter import PINS
    require(derivation['source_pins'] == PINS, 'R1 source identities changed')
    original_raw = reader.read(derivation['original_step'], 2097152)
    rows_raw = reader.read(derivation['original_rows'], 2097152)
    original = object_json(original_raw)
    rows = strict_json(rows_raw)
    require(type(rows) is list and reader.read(derivation['original_log'], 2097152) == log,
            'original R1 rows or full log missing')
    import json
    require(rows_raw == json.dumps(rows, sort_keys=True).encode()
            and original.get('resource_snapshot_sha256') == digest(rows_raw)
            and step.get('r1_original_step_sha256') == digest(original_raw)
            and step.get('r1_original_rows_sha256') == digest(rows_raw),
            'R1 original rows/step digest mismatch')
    unwrapped = dict(step)
    for name in ('cwd','r1_original_step_sha256','r1_original_rows_sha256'):
        unwrapped.pop(name)
    unwrapped['resource_snapshot_sha256'] = digest(rows_raw)
    require(unwrapped == original and snapshot.get('registered_identities') == rows
            and snapshot.get('original_rows_sha256') == digest(rows_raw)
            and snapshot.get('original_step_sha256') == digest(original_raw),
            'derived compiler record rewrote original R1 evidence')
    registered = snapshot.get('registered_identities')
    require(type(registered) is list and bool(registered), 'registered resource identities required')
    identities = [_identity(i) for i in registered]
    require(len(identities) == len(set(identities))
            and all(i[2] == context['clock_domain']['boot_id'] for i in identities),
            'registered resource identities must be unique and from this boot')
    require(digest(snapshot_raw) == step['resource_snapshot_sha256']
            and snapshot.get('clock_domain') == context['clock_domain']
            and snapshot.get('request_id') == context['request_id']
            and snapshot.get('remaining_live') == []
            and snapshot.get('observer_joined') is True
            and identity in identities,
            'resource snapshot must be an actual bound artifact')
    if rc == 0:
        require(result.get('status') == 'compile_pass'
                and re.search(rb'\b(?:ERROR|FATAL)\s*:', log, re.I) is None, 'inconsistent compiler pass')
        return {'status': 'compile_pass', 'failure': None, 'scope': 'xvlog_frontend_only'}
    require(result.get('status') == 'compile_fail', 'nonzero exit is not a syntax trigger')
    # Process, identity and complete artifact closure have already passed.
    # Diagnostic prompt eligibility is a separate decision.
    from compiler_repair import EvidenceError
    try:
        failure = bind_compile_failure(root, manifest)
        reason = None
    except EvidenceError as error:
        failure = None
        reason = str(error)[:240]
    return {'status': 'compile_fail', 'failure': failure, 'repair_eligible':failure is not None,
            'repair_unavailable_reason':reason, 'scope': 'xvlog_frontend_only'}
