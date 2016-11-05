from __future__ import print_function

import signal
import sys
import threading
import time

from .compat import (
    is_watchdog_supported,
    queue,
)
from .interfaces import IFileMonitor
from .ipc import ProcessGroup
from .worker import (
    Worker,
    is_active,
    get_reloader,
)


class FileMonitorProxy(IFileMonitor):
    def __init__(self, monitor_factory, verbose=1):
        self.monitor = monitor_factory(self.files_changed)
        self.verbose = verbose
        self.change_event = threading.Event()
        self.lock = threading.Lock()
        self.changed_paths = set()

    def out(self, msg):
        if self.verbose > 0:
            print(msg)

    def add_path(self, path):
        if (
            path.startswith('/Users/michael/work/oss/hupper')
        ):
            print(path)
        self.monitor.add_path(path)

    def start(self):
        self.monitor.start()

    def stop(self):
        self.monitor.stop()

    def join(self):
        self.monitor.join()

    def files_changed(self, paths):
        with self.lock:
            for path in sorted(paths):
                if path not in self.changed_paths:
                    self.change_event.set()
                    self.changed_paths.add(path)
                    self.out('%s changed; reloading ...' % (path,))

    def is_changed(self):
        return self.change_event.is_set()

    def wait_for_change(self, timeout=None):
        return self.change_event.wait(timeout)

    def clear_changes(self):
        with self.lock:
            self.change_event.clear()
            self.changed_paths.clear()


class Reloader(object):
    """
    A wrapper class around a file monitor which will handle changes by
    restarting a new worker process.

    """
    def __init__(self,
                 worker_path,
                 monitor_factory,
                 reload_interval=1,
                 verbose=1,
                 ):
        self.worker_path = worker_path
        self.monitor_factory = monitor_factory
        self.reload_interval = reload_interval
        self.verbose = verbose
        self.monitor = None
        self.worker = None
        self.group = ProcessGroup()

    def out(self, msg):
        if self.verbose > 0:
            print(msg)

    def run(self):
        """
        Execute the reloader forever, blocking the current thread.

        This will invoke ``sys.exit(1)`` if interrupted.

        """
        self._capture_signals()
        self._start_monitor()
        try:
            while True:
                start = time.time()
                if not self._run_worker():
                    self._wait_for_changes()
                debounce = self.reload_interval - (time.time() - start)
                if debounce > 0:
                    time.sleep(debounce)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop_monitor()
            self._restore_signals()
        sys.exit(1)

    def run_once(self):
        """
        Execute the worker once.

        This method will return after a file change is detected.

        """
        self._capture_signals()
        self._start_monitor()
        try:
            self._run_worker()
        except KeyboardInterrupt:
            return
        finally:
            self._stop_monitor()
            self._restore_signals()

    def _run_worker(self):
        self.worker = Worker(self.worker_path)
        self.worker.start()

        try:
            # register the worker with the process group
            self.group.add_child(self.worker.pid)

            self.out("Starting monitor for PID %s." % self.worker.pid)
            self.monitor.clear_changes()

            while not self.monitor.is_changed() and self.worker.is_alive():
                try:
                    # if the child has sent any data then restart
                    if self.worker.pipe.poll(0):
                        # do not read, the pipe is closed after the break
                        break
                except EOFError:  # pragma: nocover
                    pass

                try:
                    path = self.worker.files_queue.get(
                        timeout=self.reload_interval,
                    )
                except queue.Empty:
                    pass
                else:
                    self.monitor.add_path(path)
        finally:
            if self.worker.is_alive():
                self.out('Killing server with PID %s.' % self.worker.pid)
                self.worker.terminate()
                self.worker.join()

            else:
                self.worker.join()
                self.out('Server with PID %s exited with code %d.' %
                         (self.worker.pid, self.worker.exitcode))

        self.monitor.clear_changes()

        force_exit = self.worker.terminated
        self.worker = None
        return force_exit

    def _wait_for_changes(self):
        self.out('Waiting for changes before reloading.')
        while (
            not self.monitor.wait_for_change(self.reload_interval)
        ):  # pragma: nocover
            pass

        self.monitor.clear_changes()

    def _start_monitor(self):
        self.monitor = FileMonitorProxy(self.monitor_factory, self.verbose)
        self.monitor.start()

    def _stop_monitor(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor.join()
            self.monitor = None

    def _capture_signals(self):
        # SIGHUP is not supported on windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, self._signal_sighup)

    def _signal_sighup(self, signum, frame):
        if self.worker:
            self.out('Received SIGHUP, triggering a reload.')
            self.worker.terminate()

    def _restore_signals(self):
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, signal.SIG_DFL)


def start_reloader(
    worker_path,
    reload_interval=1,
    verbose=1,
    monitor_factory=None,
):
    """
    Start a monitor and then fork a worker process which starts by executing
    the importable function at ``worker_path``.

    If this function is called from a worker process that is already being
    monitored then it will return a reference to the current
    :class:`.ReloaderProxy` which can be used to communicate with the monitor.

    ``worker_path`` must be a dotted string pointing to a globally importable
    function that will be executed to start the worker. An example could be
    ``myapp.cli.main``. In most cases it will point at the same function that
    is invoking ``start_reloader`` in the first place.

    ``reload_interval`` is a value in seconds and will be used to throttle
    restarts.

    ``verbose`` controls the output. Set to ``0`` to turn off any logging
    of activity and turn up to ``2`` for extra output.

    ``monitor_factory`` is a :class:`hupper.interfaces.IFileMonitorFactory`.
    If left unspecified, this will try to create a
    :class:`hupper.watchdog.WatchdogFileMonitor` if
    `watchdog <https://pypi.org/project/watchdog/>`_ is installed and will
    fallback to the less efficient
    :class:`hupper.polling.PollingFileMonitor` otherwise.

    """
    if is_active():
        return get_reloader()

    if monitor_factory is None:
        if is_watchdog_supported():
            from .watchdog import WatchdogFileMonitor

            def monitor_factory(callback):
                return WatchdogFileMonitor(callback)

            if verbose > 1:
                print('File monitor backend: watchdog')

        else:
            from .polling import PollingFileMonitor

            def monitor_factory(callback):
                return PollingFileMonitor(callback, reload_interval)

            if verbose > 1:
                print('File monitor backend: polling')

    reloader = Reloader(
        worker_path=worker_path,
        reload_interval=reload_interval,
        verbose=verbose,
        monitor_factory=monitor_factory,
    )
    return reloader.run()
