import _thread
import os
import sys
import time

from micropython import const, schedule

CRITICAL = const(50)
ERROR = const(40)
WARNING = const(30)
INFO = const(20)
DEBUG = const(10)
NOTSET = const(0)

_STAT_SIZE_INDEX = 6

_level_str = {CRITICAL: "CRITICAL", ERROR: "ERROR", WARNING: "WARNING", INFO: "INFO", DEBUG: "DEBUG"}

_global_lock = _thread.allocate_lock()

_handlers = []

_format = "%(levelname)s:%(name)s:%(message)s"

_loggers = dict()


class Handler:
    def __init__(self, level=NOTSET):
        self.level = level

    def _handle_io_error(self, e, msg=""):
        print("--- Logging I/O Error ---")
        if msg:
            print(msg)
        sys.print_exception(e)

    def emit(self, record, record_str):
        raise NotImplementedError()

    def emit_exception(self, exception):
        raise NotImplementedError()

    def close(self):
        pass


class StreamHandler(Handler):
    def __init__(self, stream=sys.stderr, level=NOTSET):
        super().__init__(level)
        self.stream = stream

    def emit(self, record, record_str):
        try:
            self.stream.write(record_str)
        except Exception:
            pass

    def emit_exception(self, exception):
        try:
            sys.print_exception(exception, self.stream)
        except Exception:
            pass


class FileHandler(Handler):
    def __init__(self, filename, filemode="a", level=NOTSET):
        super().__init__(level)
        self.filename = filename
        self.filemode = filemode

        self._file_pointer = None
        self._file_size = 0

    def _open_file(self):
        if self._file_pointer is None:
            try:
                self._file_pointer = open(self.filename, self.filemode)
                self._file_size = -1
            except Exception as e:
                self._handle_io_error(e, f"Error: Failed to open log file: {self.filename}")
                return False

        if self._file_size == -1:
            try:
                self._file_size = os.stat(self.filename)[_STAT_SIZE_INDEX]
            except OSError:
                self._file_size = 0

        return True

    def emit(self, record, record_str):
        if not self._open_file():
            return

        try:
            bytes_written = self._file_pointer.write(record_str)
            self._file_size += bytes_written
            self._file_pointer.flush()
        except Exception as e:
            self._handle_io_error(e, f"Error: Failed to write to log file: {self.filename}")
            self.close()

    def emit_exception(self, exception):
        if not self._open_file():
            return

        try:
            sys.print_exception(exception, self._file_pointer)
            self._file_pointer.flush()
            self._file_size = -1
        except Exception as e:
            self._handle_io_error(e, f"Error: Failed to write exception to log file: {self.filename}")
            self.close()

    def close(self):
        if self._file_pointer:
            self._file_pointer.close()
            self._file_pointer = None
        # Invalidate the cached size.
        self._file_size = -1


class RotatingFileHandler(FileHandler):
    def __init__(self, filename, filemode="a", max_bytes=0, backup_count=0, level=NOTSET):
        super().__init__(filename, filemode, level)
        self.max_bytes = max_bytes
        self.backup_count = backup_count

    def do_rollover(self):
        self.close()

        if self.backup_count > 0:
            for i in range(self.backup_count - 1, -1, -1):
                sfn = self.filename + (f".{i}" if i > 0 else "")  # Source filename
                dfn = self.filename + f".{i + 1}"  # Destination filename

                try:
                    os.stat(sfn)

                    if i == self.backup_count - 1:
                        try:
                            os.remove(dfn)
                        except OSError:
                            pass

                    os.rename(sfn, dfn)
                except OSError:
                    pass

    def emit(self, record, record_str):
        if not self._open_file():
            return

        if 0 < self.max_bytes <= self._file_size + len(record_str):
            self.do_rollover()

        super().emit(record, record_str)


class Logger:
    def __init__(self, name, level=NOTSET):
        self._name = name
        self._level = level
        self._start_ms = time.ticks_ms()

    def setLevel(self, level):
        self._level = level

    def _scheduled_emit(self, record_tuple):
        level, message, name, exception = record_tuple

        with _global_lock:
            handlers_copy = list(_handlers)
            format_copy = _format

        if not handlers_copy:
            return

        record_data = {
            "levelname": _level_str.get(level, str(level)),
            "level": level,
            "message": message,
            "name": name,
            "asctime": "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*time.localtime()),
            "chrono": "{:f}".format(time.ticks_diff(time.ticks_ms(), self._start_ms) / 1000),
            "exception": exception,
        }

        try:
            record_str = format_copy % record_data + "\n"
        except (TypeError, KeyError) as e:
            print("--- Logging Format Error ---")
            print(f"Format string: '{format_copy}'")
            print(f"Record data: {record_data}")
            sys.print_exception(e)
            record_str = f"{record_data['levelname']}:{record_data['name']}:{record_data['message']}\n"

        for handler in handlers_copy:
            if record_data["level"] >= handler.level:
                handler.emit(record_data, record_str)
                if exception:
                    handler.emit_exception(exception)

    def log(self, level, message, *args, **kwargs):
        if level < self._level or not _handlers:
            return

        try:
            if args:
                message = message % args
        except TypeError:
            message = f"FORMATTING ERROR: msg='{message}' args={args}"
            level = ERROR

        record_tuple = (
            level,
            message,
            self._name,
            kwargs.get("exception"),
        )

        try:
            schedule(self._scheduled_emit, record_tuple)
        except RuntimeError:
            print("WARNING: Log message dropped, scheduler queue is full.")

    def debug(self, message, *args):
        self.log(DEBUG, message, *args)

    def info(self, message, *args):
        self.log(INFO, message, *args)

    def warning(self, message, *args):
        self.log(WARNING, message, *args)

    def error(self, message, *args):
        self.log(ERROR, message, *args)

    def critical(self, message, *args):
        self.log(CRITICAL, message, *args)

    def exception(self, exception, message, *args):
        self.log(ERROR, message, *args, exception=exception)


def getLogger(name="root", level=INFO):
    with _global_lock:
        if name not in _loggers:
            _loggers[name] = Logger(name, level=level)
        return _loggers[name]


def setLevel(level):
    getLogger().setLevel(level)


def close():
    with _global_lock:
        for handler in _handlers:
            handler.close()
        _handlers.clear()
        _loggers.clear()


def addHandler(handler):
    with _global_lock:
        _handlers.append(handler)


def basicConfig(
    *,
    level=INFO,
    stream_level=INFO,
    filename=None,
    file_level=WARNING,
    filemode="a",
    max_bytes=0,
    backup_count=0,
    format=None,
):
    global _handlers, _format, _loggers

    new_handlers = []
    if stream_level is not None:
        new_handlers.append(StreamHandler(level=stream_level))

    if filename is not None:
        if max_bytes > 0:
            new_handlers.append(
                RotatingFileHandler(filename, max_bytes=max_bytes, backup_count=backup_count, level=file_level)
            )
        else:
            new_handlers.append(FileHandler(filename, filemode=filemode, level=file_level))

    with _global_lock:
        for handler in _handlers:
            handler.close()
        _handlers.clear()
        _loggers.clear()

        if "root" not in _loggers:
            _loggers["root"] = Logger("root")
        root = _loggers["root"]
        root.setLevel(level)

        _handlers.extend(new_handlers)
        if format is not None:
            _format = format


def debug(message, *args):
    getLogger().debug(message, *args)


def info(message, *args):
    getLogger().info(message, *args)


def warning(message, *args):
    getLogger().warning(message, *args)


def error(message, *args):
    getLogger().error(message, *args)


def critical(message, *args):
    getLogger().critical(message, *args)


def exception(exception, message, *args):
    getLogger().exception(exception, message, *args)
