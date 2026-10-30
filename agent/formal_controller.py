"""Formal-file vertical slice with injected, unaccepted native adapter interfaces.

No experiment suites, scores, task IDs, historical winners or hidden artifacts
are read. Compile-only selection is delivery of a syntax-checked candidate,
never a claim of functional correctness. Native operations default to disabled.
"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
from artifact_store import EvidenceStore, _publish
from compiler_repair import load_public_input, build_compiler_prompt
from prompt_builder import PromptBuilder, canonical, digest
from rtl_delivery import DeliveryStore, inspect_body
from generation_policy import GenerationPolicy
from source_policy import source_allowed
from adapter_contracts import (BoundaryError, ModelReply, CompileReply,
    UnconfiguredAdapters, MODEL, RUNTIME, finite, require, runtime_receipt,
    token_receipt, model_receipt, compiler_receipt)


class Budget:
    def __init__(self, clock, domain, queued_at=None, outer_deadline=None):
        self.clock = clock
        require(type(domain) is dict and set(domain) == {'boot_id', 'pid', 'start_ticks'}
                and type(domain['pid']) is int and domain['pid'] > 0
                and all(type(domain[k]) is str and domain[k] for k in ('boot_id', 'start_ticks')),
                'monotonic domain needs boot and process identity')
        self.domain = json.loads(canonical(domain))
        self.last = clock()
        require(finite(self.last), 'finite monotonic clock required')
        self.started = self.last if queued_at is None else queued_at
        require(finite(self.started) and self.started <= self.last, 'queue start must be in this clock domain')
        self.end = self.started + 900.0
        if outer_deadline is not None:
            require(finite(outer_deadline), 'absolute outer deadline required')
            self.end = min(self.end, outer_deadline)

    def check(self):
        now = self.clock()
        require(finite(now) and now >= self.last, 'monotonic clock regressed or invalid')
        self.last = now
        if now >= self.end:
            raise TimeoutError('absolute chain deadline reached')
        return now


class Permit:
    """One use only; a trusted adapter must call this immediately before effect."""
    def __init__(self, budget, payload_sha, emit):
        self.budget = budget
        self.payload_sha = payload_sha
        self.emit = emit
        self.used = False
        self.started = None

    def enter(self, payload):
        require(not self.used, 'permit reuse is prohibited')
        self.used = True  # Even a failed attempt cannot be replayed.
        require(type(payload) is bytes and digest(payload) == self.payload_sha, 'effect payload changed')
        self.budget.check()
        self.emit('dispatch_permit_requested', payload_sha256=self.payload_sha)
        # Evidence publication may take time; check again immediately before handoff.
        self.started = self.budget.check()
        return self.started


class Controller:
    def __init__(self, input_dir, output_dir, *, adapters=None, clock=time.monotonic,
                 clock_domain, queued_at=None, outer_deadline=None, run_id=None):
        # Create the sole absolute budget before input, knowledge or file I/O.
        self.budget = Budget(clock, clock_domain, queued_at, outer_deadline)
        self.generation_policy = GenerationPolicy.load()
        self.budget.check()
        self.run_id = run_id or uuid.uuid4().hex
        require(type(self.run_id) is str and re.fullmatch('[a-zA-Z0-9_-]{1,64}', self.run_id), 'safe run ID required')
        self.input = Path(input_dir).absolute()
        self.output = Path(output_dir).absolute()
        require(not self.input.is_symlink() and self.input.is_dir()
                and not self.output.is_symlink(), 'real input and output directories required')
        a, b = self.input.resolve(), self.output.resolve()
        require(a != b and a not in b.parents and b not in a.parents, 'input/output trees must be disjoint')
        self.output.mkdir(parents=True, exist_ok=True)
        require(not any(self.output.iterdir()), 'output directory must be empty; no replay or stale solution')
        self.store = EvidenceStore(self.output)
        self.store.claim('chain', {'run_id':self.run_id, 'clock_domain':self.budget.domain,
            'started_at_monotonic':self.budget.started, 'deadline_monotonic':self.budget.end,
            'max_model_attempts':3, 'resume_allowed':False, 'runtime_accepted':False})
        self.adapters = adapters if adapters is not None else UnconfiguredAdapters()
        self.trace = []
        self.store.record('chain', 'generation-policy.json', self.generation_policy.audit())
        self.claims = 0
        self.handoffs = 0
        self.posts_reported = 0
        self.posts_unknown = 0
        self.tool_handoffs = 0
        self.tools_reported_closed = 0
        self.tools_unknown = 0
        self.public_sha = None
        self.public = None
        self.selected = None
        self.selected_stage = None
        self.selection_evidence = None
        self.phase = 'created'
        self.routing_decision = None

    def context(self, request_id=None):
        return {'run_id':self.run_id, 'request_id':request_id,
                'clock_domain':dict(self.budget.domain),
                'deadline_monotonic':self.budget.end,
                'public_fields_sha256':self.public_sha}

    @staticmethod
    def adapter_context(context):
        # Adapters cannot mutate the controller's expected nested bindings.
        return json.loads(canonical(context))

    def emit(self, tool, rc=0, usage=None, **extra):
        row = {'ts':datetime.now(timezone.utc).isoformat(), 'tool':tool, 'rc':rc,
               'excerpt':'', 'tokens_in':None if usage is None else usage['prompt_tokens'],
               'tokens_out':None if usage is None else usage['completion_tokens'],
               'usage_known':usage is not None, 'sequence':len(self.trace), **extra}
        self.store.record('chain', 'trace-%03d.json' % len(self.trace), row)
        self.trace.append(row)

    def snapshot(self):
        self.budget.check()
        directory = self.output/'public'
        directory.mkdir()
        refs = {}
        # Never enumerate input contents or read test, grade or metadata files.
        for name in ('prompt.txt', 'interface.txt'):
            path = self.input/name
            if name == 'interface.txt' and not path.exists():
                continue
            require(not path.is_symlink() and path.is_file(), 'public input file missing or linked')
            with path.open('rb') as fh:
                data = fh.read(200001)
            require(len(data) <= 200000, 'public file limit; no clipping')
            with (directory/name).open('xb') as fh:
                fh.write(data)
            refs[name] = {'path':name, 'bytes':len(data), 'sha256':digest(data)}
        self.public = load_public_input(directory, refs)
        audit = json.loads(self.public.audit_json)
        self.public_sha = audit['public_fields_sha256']
        self.store.record('chain', 'public-snapshot.json', audit)
        self.budget.check()
        return {'public_spec':self.public.spec, 'interface':self.public.interface}

    def model(self, stage, prepared):
        require((stage, self.phase) in (('route','runtime_checked'), ('initial','route_closed'),
                                       ('compiler_repair','compile_fail_bound')),
                'invalid model stage transition')
        require(self.claims < 3, 'model attempt ceiling')
        self.budget.check()
        request_id = self.run_id + '.' + stage
        context = self.context(request_id)
        if stage == 'initial' and prepared.audit().get('kind') == 'direct_solve':
            context['solve_mode'] = 'direct'
        messages = canonical(prepared.to_chat_messages())
        self.generation_policy.require_current()
        generation = self.generation_policy.generation_bytes
        token_raw = self.adapters.tokenize(messages=messages, generation=generation, context=self.adapter_context(context))
        self.store.record('chain', stage+'-tokens.json', token_raw)
        count = token_receipt(token_raw, messages, generation, context)
        self.budget.check()
        seconds = min(180 if stage == 'route' else (840 if context.get('solve_mode') == 'direct' else 540) if stage == 'initial' else 720,
                      self.budget.end-self.budget.check()-90)
        cap = min(4096 if stage == 'route' else 16384, 32768-count-128,
                  max(0, math.floor((seconds-12)*35)))
        require(cap >= 1, 'insufficient context; no truncation or extra request')
        payload = canonical({'model':MODEL, 'messages':prepared.to_chat_messages(),
            'temperature':0, 'top_p':1, 'max_tokens':cap, 'stream':True,
            'stream_options':{'include_usage':True},
            **self.generation_policy.fields()})
        self.store.claim(stage, {'request_id':request_id, 'payload_sha256':digest(payload),
            'public_fields_sha256':self.public_sha, 'deadline_monotonic':self.budget.end,
            'clock_domain':self.budget.domain, 'automatic_retries':0,
            'generation_policy':self.generation_policy.audit()})
        self.claims += 1
        self.store.record(stage, 'request.json', payload)
        self.store.record(stage, 'prompt-audit.json', prepared.audit())
        deliveries = DeliveryStore(self.output/'deliveries')
        if stage != 'route':
            deliveries.begin(stage, request_id)
        permit = Permit(self.budget, digest(payload), lambda tool, **kw:self.emit(tool, stage=stage, **kw))
        self.budget.check()
        self.generation_policy.require_current()
        self.budget.check()
        self.handoffs += 1
        self.posts_unknown += 1
        try:
            reply = self.adapters.generate(payload=payload, context=self.adapter_context(context), permit=permit)
            require(type(reply) is ModelReply, 'model reply type')
            # Retain originals even when the supplied closure record is invalid.
            self.store.record(stage, 'body.bin', reply.body)
            self.store.record(stage, 'stream.bin', reply.stream)
            self.store.record(stage, 'receipt.json', reply.receipt)
            require(permit.started is not None, 'adapter bypassed the dispatch permit')
            row = model_receipt(reply, payload, context, permit.started, self.budget.check(), count)
            self.posts_unknown -= 1
            self.posts_reported += 1
            self.emit('llm', usage=row.get('usage'), stage=stage, claimed=True,
                adapter_handoff=True, adapter_reported_posts=1, adapter_reported_closed=True,
                native_closure_verified=False, cap=cap, request_sha256=digest(payload))
            if stage != 'route':
                # R2 is a second, evidence-only delivery boundary; no selection here.
                wire = canonical({'schema':'transport-body-closure-v1','request_id':request_id,
                    'body_sha256':digest(reply.body),'http_status':200,'wire_complete':True,
                    'transport_closed':True,'backend_idle':True,
                    'origin':'validated injected adapter receipt; native verification not established'})
                delivery = deliveries.record(stage, reply.body, wire)
                require(delivery['candidate'] is not None, 'RTL delivery '+delivery['body_kind'])
            self.phase = stage+'_closed'
            return reply.body
        except Exception as exc:
            self.emit('llm_unknown_or_rejected', rc=1, stage=stage,
                error_type=type(exc).__name__, permit_used=permit.used,
                effect_may_have_started=permit.started is not None)
            raise

    def compile(self, stage, body):
        require(self.phase == stage+'_closed', 'compile without closed source generation')
        candidate = inspect_body(body).candidate
        require(candidate is not None, 'no complete RTL')
        require(source_allowed(candidate.decode('utf-8')), 'source policy rejected before tool claim; no repair')
        self.budget.check()
        event = stage+'-compile'
        context = self.context(self.run_id+'.'+event)
        context['body_sha256'] = digest(body)
        context['source_sha256'] = digest(candidate)
        job = canonical(context)
        self.store.claim(event, {'request_id':context['request_id'], 'job_sha256':digest(job),
                                'clock_domain':self.budget.domain,'deadline_monotonic':self.budget.end})
        self.store.record(event, 'job.json', job)
        directory = self.output/(event+'-artifacts')
        directory.mkdir()
        permit = Permit(self.budget, digest(job), lambda tool, **kw:self.emit(tool, stage=event, **kw))
        self.budget.check()
        self.tool_handoffs += 1
        self.tools_unknown += 1
        reply = self.adapters.compile(source=candidate, body=body, artifact_dir=directory,
                                      context=self.adapter_context(context), permit=permit)
        require(type(reply) is CompileReply, 'compile reply type')
        self.store.record(event, 'manifest.json', reply.manifest)
        self.store.record(event, 'attestation.json', reply.attestation)
        require(permit.started is not None, 'compiler adapter bypassed permit')
        bound = compiler_receipt(reply, directory, candidate, body, context,
                                 permit.started, self.budget.check())
        self.tools_unknown -= 1
        self.tools_reported_closed += 1
        self.emit('compiler', rc=0 if bound['status']=='compile_pass' else 1, status=bound['status'], scope=bound['scope'],
            source_sha256=digest(candidate), interface_compatibility='unknown',
            functional_status='unknown', native_runtime_accepted=False)
        self.phase = 'compile_fail_bound' if bound['status'] == 'compile_fail' else 'compile_pass_bound'
        if bound['status'] == 'compile_pass':
            self.selected = candidate
            self.selected_stage = stage
            self.selection_evidence = {'manifest_sha256':digest(reply.manifest),
                                       'attestation_sha256':digest(reply.attestation), 'scope':bound['scope']}
        return bound

    def _publish_file(self, name, data, deadline=False):
        pending = self.output/('pending-'+uuid.uuid4().hex)
        with pending.open('xb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if deadline:
            self.budget.check()
        _publish(pending, self.output/name)

    def finish(self, outcome, error=None):
        if self.selected is not None:
            try:
                self.budget.check()
            except Exception:
                self.selected = None
                outcome = 'deadline_before_selection'
        data = self.selected if self.selected is not None else b''
        decision = {'schema':'formal-output-decision-v1','run_id':self.run_id,
            'outcome':outcome, 'error_type':error,
            'selection_basis':'compile_verified' if self.selected is not None else 'no_eligible_candidate',
            'selected_stage':self.selected_stage if self.selected is not None else None,
            'compiler_evidence':self.selection_evidence if self.selected is not None else None,
            'solution_sha256':digest(data), 'solution_bytes':len(data),
            'public_fields_sha256':self.public_sha,
            'routing_decision':self.routing_decision,
            'interface_compatibility':'unknown','functional_status':'unknown',
            'model_claims':self.claims, 'adapter_handoffs':self.handoffs,
            'adapter_reported_posts':self.posts_reported, 'post_outcome_unknown':self.posts_unknown,
            'tool_handoffs':self.tool_handoffs,
            'adapter_reported_tool_closures':self.tools_reported_closed,
            'tool_outcome_unknown':self.tools_unknown,
            'physical_model_calls_verified':False, 'runtime_accepted':False,
            'formal_submission_ready':False, 'model_benefit_verified':False,
            'publication_rule':'solution.v is published last; a decision alone is not publication; verify its SHA',
            'absolute_deadline_monotonic':self.budget.end, 'clock_domain':self.budget.domain}
        self.emit('agent_result', rc=0 if data else 1, outcome=outcome,
                  selection_basis=decision['selection_basis'], functional_status='unknown')
        self.emit('task_closed', rc=0 if self.posts_unknown == self.tools_unknown == 0 else 1,
                  adapter_request_closure='unknown' if self.posts_unknown else 'reported',
                  adapter_tool_closure='unknown' if self.tools_unknown else 'reported',
                  native_closure_verified=False, official_grade='not_run')
        self._publish_file('selection.json', canonical(decision))
        self._publish_file('trace.jsonl', b''.join(canonical(row)+b'\n' for row in self.trace))
        # No write follows this atomic, exclusive publication. Partial files and
        # an unfulfilled selection decision remain evidence, never a replay cue.
        self._publish_file('solution.v', data, deadline=bool(data))
        return decision

    def run(self):
        outcome, error = 'unknown', None
        try:
            public = self.snapshot()
            builder = PromptBuilder()
            self.budget.check()
            raw = self.adapters.preflight(context=self.context())
            self.store.record('chain', 'runtime.json', raw)
            receipt = runtime_receipt(raw, self.context())
            self.emit('model_identity', runtime_sha256=RUNTIME, evidence_scope=receipt['evidence_scope'])
            self.budget.check()
            self.phase = 'runtime_checked'
            route = self.model('route', builder.build_route(public))
            try:
                route_text = route.decode('utf-8')
            except UnicodeError:
                solve = builder.build_direct(public,'route_unusable',digest(route),
                                             {'route_error':'invalid UTF-8','semantic_no_card_confirmed':False})
            else:
                solve = builder.build_solve(public, route_text)
            audit = solve.audit()
            self.routing_decision = {'mode':'direct' if audit['kind']=='direct_solve' else 'knowledge',
                                     'reason':audit.get('reason','applicable_cards'),
                                     'cards':audit.get('selected_leaf_ids',[]),
                                     'raw_route_sha256':digest(route),
                                     'router_selected_no_card':audit.get('router_selected_no_card',False),
                                     'semantic_no_card_confirmed':audit.get('semantic_no_card_confirmed',False)}
            self.emit('routing_decision',**self.routing_decision)
            initial = self.model('initial', solve)
            compiled = self.compile('initial', initial)
            if compiled['status'] == 'compile_fail' and compiled['failure'] is not None:
                repair = build_compiler_prompt(self.public, compiled['failure'])
                body = self.model('compiler_repair', repair)
                compiled = self.compile('compiler_repair', body)
            outcome = 'compile_only_candidate_delivered' if self.selected is not None else 'compiler_failure_unrepaired'
        except Exception as exc:
            # Never continue or retry after an ambiguous effect. Adapters own
            # native cleanup and must preserve their raw records on exceptions.
            outcome, error = 'incomplete_or_unknown', type(exc).__name__
            self.selected = None
            self.emit('agent_error', rc=1, error_type=error, phase=self.phase)
        return self.finish(outcome, error)


def run_offline(input_dir, output_dir, **kwargs):
    return Controller(input_dir, output_dir, **kwargs).run()


if __name__ == '__main__':
    raise SystemExit('Use run.sh for a task or serve/start.sh for the API service.')
