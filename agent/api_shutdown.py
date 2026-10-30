"""Best-effort API cleanup: preserve each attempted outcome; no native closure claim."""


class ApiShutdownError(RuntimeError):
    def __init__(self, report):
        super().__init__('API cleanup or evidence publication is incomplete')
        self.report = report


def shutdown_api(*, owner, pump=None, server=None, watcher=None, stop_watch=None,
                 primary_error=None, publish):
    report = {'schema':'api-cleanup-r10', 'steps':[],
              'primary_error_type':type(primary_error).__name__ if primary_error else None,
              'native_closure_verified':False, 'runtime_accepted':False}
    failed = False
    def attempt(name, action):
        nonlocal failed
        try:
            action()
            report['steps'].append({'name':name,'status':'returned'})
        except BaseException as error:
            failed = True
            report['steps'].append({'name':name,'status':'unknown','error_type':type(error).__name__})
    def joined(thread):
        # A successful timeout-limited join alone is not exit evidence.
        if thread.is_alive():raise RuntimeError('thread is still alive')
    if owner is not None:attempt('stop_admission',lambda:setattr(owner,'tainted',True))
    if server is not None:attempt('stop_server',lambda:setattr(server,'should_exit',True))
    if pump is not None:attempt('stop_pump',pump.stop.set)
    if stop_watch is not None:attempt('stop_watcher',stop_watch.set)
    if watcher is not None:
        attempt('join_watcher',lambda:watcher.join(2.))
        attempt('observe_watcher_exit',lambda:joined(watcher))
    # Owner performs identity-qualified native termination before the final
    # dispatcher join. A failed owner close must never skip that join.
    if owner is not None:attempt('owner_close',owner.close)
    if pump is not None:
        attempt('pump_close',pump.close)
        attempt('observe_pump_exit',lambda:joined(pump.thread))
        if pump.error is not None:
            failed = True
            report['steps'].append({'name':'pump_run','status':'unknown','error_type':str(pump.error)})
    report['cleanup_methods_completed'] = not failed
    report['operation_succeeded'] = primary_error is None and not failed
    # The file written by publish is an attempted publication record. Only the
    # returned in-memory report can observe publish returning successfully.
    report['publication_status'] = 'attempting'
    try:
        publish(report)
        report['publication_status'] = 'returned'
    except BaseException as error:
        failed = True
        report['operation_succeeded'] = False
        report['publication_status'] = 'unknown'
        report['publication_error_type'] = type(error).__name__
    if failed:
        raise ApiShutdownError(report) from primary_error
    return report
