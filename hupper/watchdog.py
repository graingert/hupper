# check ``hupper.compat.is_watchdog_supported`` before using this module
from __future__ import absolute_import

import os.path
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .interfaces import IFileMonitor


class WatchdogFileMonitor(FileSystemEventHandler, Observer, IFileMonitor):
    def __init__(self, callback):
        super(WatchdogFileMonitor, self).__init__()
        self.callback = callback
        self.paths = set()
        self.dirpaths = set()
        self.lock = threading.Lock()

    def add_path(self, path):
        with self.lock:
            if path not in self.paths:
                self.paths.add(path)
                dirpath = os.path.dirname(path)
                if dirpath not in self.dirpaths:
                    self.dirpaths.add(dirpath)
                    self.schedule(self, dirpath)

    def on_any_event(self, event):
        with self.lock:
            src_path = event.src_path
            dep_paths = set([src_path])
            if src_path.endswith('.pyc'):
                dep_paths.add(src_path[:-1])
            if src_path.endswith('.py'):
                dep_paths.add(src_path + 'c')
            for path in dep_paths:
                if path in self.paths:
                    self.callback([src_path])
                    break
