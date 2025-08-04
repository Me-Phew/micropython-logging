# logging.py
# Minimalistic logging implementation for MicroPython.

# ------------------------------------------------------------------------------
#  Last modified 4.08.2025, 10:53, micropython-logging                         -
# ------------------------------------------------------------------------------

import _thread
import os
import sys
import time

from micropython import const, schedule

# --- Logging Levels ---
# These are the standard logging levels, compatible with CPython's logging module.
CRITICAL = const(50)
ERROR = const(40)
WARNING = const(30)
INFO = const(20)
DEBUG = const(10)
NOTSET = const(0)

# --- Module-level constants and globals ---
_STAT_SIZE_INDEX = 6  # Index for file size in the tuple returned by os.stat()

_level_str = {CRITICAL: "CRITICAL", ERROR: "ERROR", WARNING: "WARNING", INFO: "INFO", DEBUG: "DEBUG"}

# A global lock is used to ensure thread safety when modifying shared resources
# like the list of handlers or the dictionary of loggers.
_global_lock = _thread.allocate_lock()

# List of all configured handlers.
_handlers = []

# The default format string for log messages.
_format = "%(levelname)s:%(name)s:%(message)s"

# A cache of logger instances, keyed by name.
_loggers = dict()


class Handler:
    """Base class for all log handlers.

    Subclasses must implement the `emit` method and may implement `emit_exception` and `close`.
    """

    def __init__(self, level=NOTSET):
        """Initializes the handler with a specific logging level.

        Args:
            level (int): The minimum level of messages this handler will process.
        """
        self.level = level

    def _handle_io_error(self, e, msg=""):
        """A simple, non-crashing way to handle I/O errors during logging.

        This prints the error to the default `sys.stdout` instead of trying to use
        a potentially broken logging stream.

        Args:
            e (Exception): The I/O exception that occurred.
            msg (str, optional): An additional message to print. Defaults to "".
        """
        print("--- Logging I/O Error ---")
        if msg:
            print(msg)
        sys.print_exception(e)

    def emit(self, record, record_str):
        """Handles a log record. This must be overridden by subclasses.

        Args:
            record (dict): A dictionary containing the log record's data.
            record_str (str): The formatted log message string.
        """
        raise NotImplementedError()

    def emit_exception(self, exception_obj):
        """Handles an exception. This can be overridden by subclasses.

        The default implementation does nothing. Subclasses that handle exceptions
        (like writing to a file) should override this.

        Args:
            exception_obj (Exception): The exception to log.
        """
        raise NotImplementedError()

    def close(self):
        """Cleans up any resources used by the handler (e.g., closing files)."""
        pass


class StreamHandler(Handler):
    """A handler that logs records to a stream (e.g., `sys.stderr`)."""

    def __init__(self, stream=sys.stderr, level=NOTSET):
        """Initializes the handler with a specific stream and level.

        Args:
            stream (object): The stream to write log messages to. Must have a `write` method.
            level (int): The minimum level of messages this handler will process.
        """
        super().__init__(level)
        self.stream = stream

    def emit(self, record, record_str):
        """Writes the formatted log record to the handler's stream.

        Args:
            record (dict): A dictionary containing the log record's data (unused).
            record_str (str): The formatted log message string.
        """
        try:
            self.stream.write(record_str)
        except Exception:
            # If the configured stream itself is broken (e.g., stderr), there is
            # no reliable fallback for reporting the error. We silently ignore it
            # to prevent a potential infinite loop of logging failures.
            pass

    def emit_exception(self, exception_obj):
        """Prints the exception and its traceback to the handler's stream.

        Args:
            exception_obj (Exception): The exception to log.
        """
        try:
            sys.print_exception(exception_obj, self.stream)
        except Exception:
            # Similar to emit(), if the stream is broken, we can't do anything.
            pass


class FileHandler(Handler):
    """A handler that logs records to a file."""

    def __init__(self, filename, filemode="a", level=NOTSET):
        """Initializes the handler with a filename, mode, and level.

        Args:
            filename (str): The path to the log file.
            filemode (str, optional): The mode to open the file in. Defaults to "a" (append).
            level (int): The minimum level of messages this handler will process.
        """
        super().__init__(level)
        self.filename = filename
        self.filemode = filemode

        self._file_pointer = None
        self._file_size = 0

    def _open_file(self):
        """Helper to open the log file and synchronize the file size.

        This method lazily opens the file on the first write. It also caches the
        file size to avoid repeated `os.stat` calls.

        Returns:
            bool: True if the file is open and ready, False otherwise.
        """
        if self._file_pointer is None:
            try:
                self._file_pointer = open(self.filename, self.filemode)
                # Mark size as unknown to force a check after opening.
                self._file_size = -1
            except Exception as e:
                self._handle_io_error(e, f"Error: Failed to open log file: {self.filename}")
                return False

        if self._file_size == -1:  # A value of -1 indicates the size needs to be resynchronized.
            try:
                # `os.stat` is the most reliable way to get the file size, especially
                # after opening in append mode, as the file pointer may not be at the end.
                self._file_size = os.stat(self.filename)[_STAT_SIZE_INDEX]
            except OSError:
                # This can happen if the file doesn't exist yet.
                self._file_size = 0

        return True

    def emit(self, record, record_str):
        """Writes the formatted log record to the file.

        Args:
            record (dict): A dictionary containing the log record's data (unused).
            record_str (str): The formatted log message string.
        """
        if not self._open_file():
            return

        try:
            bytes_written = self._file_pointer.write(record_str)
            self._file_size += bytes_written
            self._file_pointer.flush()
        except Exception as e:
            self._handle_io_error(e, f"Error: Failed to write to log file: {self.filename}")
            self.close()

    def emit_exception(self, exception_obj):
        """Writes the exception and its traceback to the file.

        Args:
            exception_obj (Exception): The exception to log.
        """
        if not self._open_file():
            return

        try:
            sys.print_exception(exception_obj, self._file_pointer)
            self._file_pointer.flush()
            # The size of the exception traceback is unknown, so we must
            # invalidate our cached size and force a resync on the next emit.
            self._file_size = -1
        except Exception as e:
            self._handle_io_error(e, f"Error: Failed to write exception to log file: {self.filename}")
            self.close()

    def close(self):
        """Closes the file stream if it is open."""
        if self._file_pointer:
            self._file_pointer.close()
            self._file_pointer = None
        # Invalidate the cached size.
        self._file_size = -1


class RotatingFileHandler(FileHandler):
    """A handler that logs to a file, with rolling backups when a size limit is reached."""

    def __init__(self, filename, filemode="a", max_bytes=0, backup_count=0, level=NOTSET):
        """Initializes the handler for rotating logs.

        Args:
            filename (str): The path to the log file.
            filemode (str, optional): The mode for the file. Note: for rotation, this is
                                      typically always 'a'. Defaults to "a".
            max_bytes (int, optional): The maximum size in bytes a log file can reach before
                                       it is rolled over. 0 means no rotation. Defaults to 0.
            backup_count (int, optional): The number of backup files to keep. Defaults to 0.
            level (int): The minimum level of messages this handler will process.
        """
        super().__init__(filename, filemode, level)
        self.max_bytes = max_bytes
        self.backup_count = backup_count

    def do_rollover(self):
        """Rotates the log files.

        The current log file is closed and renamed to `filename.1`. If `filename.1`
        already exists, it is renamed to `filename.2`, and so on. If the number of
        backups exceeds `backup_count`, the oldest one is deleted.
        """
        self.close()

        if self.backup_count > 0:
            # Iterate backwards from the second-to-last backup to the current log file.
            for i in range(self.backup_count - 1, -1, -1):
                sfn = self.filename + (f".{i}" if i > 0 else "")  # Source filename
                dfn = self.filename + f".{i + 1}"  # Destination filename

                try:
                    # Check if the source file exists before trying to move it.
                    os.stat(sfn)

                    # If the destination for the oldest backup already exists, remove it.
                    if i == self.backup_count - 1:
                        try:
                            os.remove(dfn)
                        except OSError:
                            pass  # It's okay if it doesn't exist.

                    os.rename(sfn, dfn)
                except OSError:
                    # This can happen if a source file in the chain doesn't exist
                    # (e.g., log.3 doesn't exist). We can safely continue.
                    pass

    def emit(self, record, record_str):
        """Emits a record, performing a rollover if the size limit is reached.

        If the current file size plus the new message size exceeds `max_bytes`,
        a rollover is triggered before the new message is written.

        Args:
            record (dict): A dictionary containing the log record's data.
            record_str (str): The formatted log message string.
        """
        if not self._open_file():
            return

        if 0 < self.max_bytes <= self._file_size + len(record_str):
            # The scheduler ensures this runs in the main thread, so no lock is
            # needed here to protect against race conditions during rollover.
            self.do_rollover()

        # Call the parent class's emit to handle the actual file write.
        super().emit(record, record_str)


class Logger:
    """The primary class for application code to interact with the logging system."""

    def __init__(self, name, level=NOTSET):
        """Initializes a logger with a name and level.

        This should not be called directly. Use `logging.getLogger(name)` instead.

        Args:
            name (str): The name of the logger, typically a module name.
            level (int): The minimum level of messages this logger will process.
        """
        self._name = name
        self._level = level
        self._start_ms = time.ticks_ms()

    def setLevel(self, level):
        """Sets the logging level for this logger.

        Messages with a severity lower than `level` will be ignored.

        Args:
            level (int): The new level to set.
        """
        self._level = level

    def _scheduled_emit(self, record_tuple):
        """Processes and dispatches a log record to all relevant handlers.

        This function is designed to be called by `micropython.schedule`, ensuring
        it runs in the main thread. This avoids the need for locks within handler
        methods and simplifies the overall design by serializing all log writes.

        Args:
            record_tuple (tuple): A tuple containing (level, message, name, exception).
        """
        level, message, name, exception_obj = record_tuple

        # Acquire lock only long enough to get a copy of the shared data.
        with _global_lock:
            handlers_copy = list(_handlers)
            format_copy = _format

        if not handlers_copy:
            return  # No handlers configured, nothing to do.

        # 1. Create the record dictionary. This is done here, in the main thread,
        # to ensure that time-sensitive fields like 'asctime' are accurate at
        # the time of processing, not the time of logging.
        record_data = {
            "levelname": _level_str.get(level, str(level)),
            "level": level,
            "message": message,
            "name": name,
            "asctime": "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*time.localtime()),
            "chrono": "{:f}".format(time.ticks_diff(time.ticks_ms(), self._start_ms) / 1000),
            "exception": exception_obj,
        }

        try:
            # 2. Format the log string.
            record_str = format_copy % record_data + "\n"
        except (TypeError, KeyError) as e:
            # This occurs if the format string is invalid or references a key
            # not present in `record_data`. We fall back to a safe default format.
            print("--- Logging Format Error ---")
            print(f"Format string: '{format_copy}'")
            print(f"Record data: {record_data}")
            sys.print_exception(e)
            record_str = f"{record_data['levelname']}:{record_data['name']}:{record_data['message']}\n"

        # 3. Emit the record to all relevant handlers.
        for handler in handlers_copy:
            if record_data["level"] >= handler.level:
                # Because `_scheduled_emit` is only ever run by the main thread's
                # scheduler, calls to handler methods are serialized, making them
                # inherently thread-safe without needing individual locks.
                handler.emit(record_data, record_str)
                if exception_obj:
                    handler.emit_exception(exception_obj)

    def log(self, level, message, *args, **kwargs):
        """Logs a message with a specific level.

        This is the core logging method. It formats the message, checks the level,
        and then schedules the final processing to avoid blocking the caller,
        which may be in an interrupt service routine (ISR).

        Args:
            level (int): The severity level of the message.
            message (str): The message format string.
            *args: Arguments to be merged into the message string using the % operator.
            **kwargs: Can contain `exception=e` to log an exception.
        """
        # Quick check to avoid work if the message will be ignored anyway.
        if level < self._level or not _handlers:
            return

        try:
            if args:
                message = message % args
        except TypeError:
            # If formatting fails, log an error with details about the failure.
            message = f"FORMATTING ERROR: msg='{message}' args={args}"
            level = ERROR

        record_tuple = (
            level,
            message,
            self._name,
            kwargs.get("exception_obj"),
        )

        try:
            # Defer the I/O-heavy part of logging to the main event loop.
            # This makes logging from ISRs and other threads safe and non-blocking.
            schedule(self._scheduled_emit, record_tuple)
        except RuntimeError:
            # Most likely scheduler queue is full, so we just ignore the log message.
            # Printing here could cause a recursion error if the logger is used in an exception handler.
            # It would also be a problem in an ISR context.
            pass

    def debug(self, message, *args):
        """Logs a message with level DEBUG.

        Args:
            message (str): The message format string.
            *args: Arguments for the message string.
        """
        self.log(DEBUG, message, *args)

    def info(self, message, *args):
        """Logs a message with level INFO.

        Args:
            message (str): The message format string.
            *args: Arguments for the message string.
        """
        self.log(INFO, message, *args)

    def warning(self, message, *args):
        """Logs a message with level WARNING.

        Args:
            message (str): The message format string.
            *args: Arguments for the message string.
        """
        self.log(WARNING, message, *args)

    def error(self, message, *args):
        """Logs a message with level ERROR.

        Args:
            message (str): The message format string.
            *args: Arguments for the message string.
        """
        self.log(ERROR, message, *args)

    def critical(self, message, *args):
        """Logs a message with level CRITICAL.

        Args:
            message (str): The message format string.
            *args: Arguments for the message string.
        """
        self.log(CRITICAL, message, *args)

    def exception(self, exception_obj, message, *args):
        """Logs a message with level ERROR and includes exception information.

        Args:
            exception_obj (Exception): The exception object to log.
            message (str): The message format string.
            *args: Arguments for the message string.
        """
        self.log(ERROR, message, *args, exception_obj=exception_obj)


def getLogger(name="root", level=INFO):
    """Returns a logger instance.

    If a logger with the given name already exists, it is returned. Otherwise,
    a new logger is created.

    Args:
        name (str, optional): The name of the logger. Defaults to "root".
        level (int, optional): The minimum level of messages this logger will process.

    Returns:
        Logger: The logger instance.
    """
    with _global_lock:
        if name not in _loggers:
            _loggers[name] = Logger(name, level=level)
        return _loggers[name]


def setLevel(level):
    """Sets the logging level for the root logger.

    Args:
        level (int): The minimum level to log.
    """
    getLogger().setLevel(level)


def close():
    """Shuts down the logging system.

    This closes all active handlers and clears the logger cache. It should be
    called during application shutdown to ensure all resources are released.
    """
    with _global_lock:
        for handler in _handlers:
            handler.close()
        _handlers.clear()
        _loggers.clear()


def addHandler(handler):
    """Adds a configured handler to the list of active handlers.

    Args:
        handler (Handler): The handler instance to add.
    """
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
    log_format=None,
):
    """Configures the logging system with a default setup.

    This is a convenience function that creates and adds handlers for you.
    It will close and remove any existing handlers before setting up the new ones.

    Args:
        level (int): The minimum level for the root logger.
        stream_level (int or None): The level for logging to the console (`sys.stderr`).
                                    Set to `None` to disable console logging.
        filename (str or None): The path to the file to log to. If `None`, file logging is disabled.
        file_level (int): The minimum level for logging to the file.
        filemode (str): The file mode, 'a' (append) or 'w' (write/truncate).
        max_bytes (int): If greater than 0, enables rotating file logs. This is the
                         maximum size of a log file before it is rolled over.
        backup_count (int): The number of backup files to keep for rotating logs.
        log_format (str or None): The format string for log messages. If `None`, the default is used.
    """
    global _handlers, _format, _loggers

    # Prepare new handlers outside the lock to minimize the time the lock is held.
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
        # Reset the entire logging system.
        for handler in _handlers:
            handler.close()
        _handlers.clear()
        _loggers.clear()

        # Re-initialize the root logger.
        if "root" not in _loggers:
            _loggers["root"] = Logger("root")
        root = _loggers["root"]
        root.setLevel(level)

        _handlers.extend(new_handlers)
        if log_format is not None:
            _format = log_format


def debug(message, *args):
    """Logs a message with level DEBUG on the root logger.

    Args:
        message (str): The message format string.
        *args: Arguments for the message string.
    """
    getLogger().debug(message, *args)


def info(message, *args):
    """Logs a message with level INFO on the root logger.

    Args:
        message (str): The message format string.
        *args: Arguments for the message string.
    """
    getLogger().info(message, *args)


def warning(message, *args):
    """Logs a message with level WARNING on the root logger.

    Args:
        message (str): The message format string.
        *args: Arguments for the message string.
    """
    getLogger().warning(message, *args)


def error(message, *args):
    """Logs a message with level ERROR on the root logger.

    Args:
        message (str): The message format string.
        *args: Arguments for the message string.
    """
    getLogger().error(message, *args)


def critical(message, *args):
    """Logs a message with level CRITICAL on the root logger.

    Args:
        message (str): The message format string.
        *args: Arguments for the message string.
    """
    getLogger().critical(message, *args)


def exception(exception_obj, message, *args):
    """Logs a message with level ERROR and exception info on the root logger.

    Args:
        exception_obj (Exception): The exception object to log.
        message (str): The message format string.
        *args: Arguments for the message string.
    """
    getLogger().exception(exception_obj, message, *args)
