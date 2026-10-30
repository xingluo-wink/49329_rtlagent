"""Bounded interval observations. Not a verdict or process-closure assertion."""
from contextlib import contextmanager
import math


class PhaseTimings:
    def __init__(self, clock, max_intervals=256):
        self.clock = clock
        self.max_intervals = max_intervals
        self.intervals = []
        self.totals = {}
        self.dropped_intervals = 0
        self.invalid_clock_samples = 0

    def stamp(self):
        try:
            value = self.clock.monotonic()
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('invalid clock')
            return value
        except Exception:
            self.invalid_clock_samples += 1
            return None

    @contextmanager
    def span(self, name):
        start = self.stamp()
        error_type = None
        try:
            yield
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            end = self.stamp()
            duration = end-start if start is not None and end is not None and end >= start else None
            if duration is None:
                self.invalid_clock_samples += 1
            item = self.totals.setdefault(name, {'calls':0,'known_duration_s':0.0,'unknown_duration_count':0,'exceptions':0})
            item['calls'] += 1
            item['exceptions'] += error_type is not None
            if duration is None: item['unknown_duration_count'] += 1
            else: item['known_duration_s'] += duration
            row = {'name':name,'start_monotonic':start,'end_monotonic':end,
                   'duration_s':duration,'error_type':error_type}
            if len(self.intervals) < self.max_intervals: self.intervals.append(row)
            else: self.dropped_intervals += 1

    def snapshot(self):
        return {'schema':'bounded-phase-timing-v1','time_basis':'caller_monotonic_clock',
                'intervals':self.intervals,'totals':self.totals,
                'dropped_intervals':self.dropped_intervals,
                'invalid_clock_samples':self.invalid_clock_samples,
                'nested_intervals_must_not_be_summed':True,
                'snapshot_excludes_its_own_serialization_and_publication':True,
                'resource_closure_asserted':False,'verdict_asserted':False,
                'performance_benefit_verified':False}
