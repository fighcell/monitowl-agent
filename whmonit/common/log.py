# -*- encoding: utf-8 -*-
'''
Custom log handlers.
'''
from __future__ import absolute_import
import datetime
import gzip
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
import traceback
from itertools import chain
from textwrap import TextWrapper
from backports.shutil_get_terminal_size import get_terminal_size
from colorclass import Color, Windows


# Our own logging values for WebServer usage.
# Low to make sure they are not printed, unless
# enabled explicitly.
SEND = 7
RECV = 5
UBERDEBUG = 3


def getLogger(name=None):
    '''
    Gets logger prefixed with 'monitowl'.
    '''
    # Invalid function name
    # pylint: disable=C0103
    return logging.getLogger(not name and "monitowl" or "monitowl.{}".format(name))


class DefaultFormatter(logging.Formatter):
    '''
    Logs formatter to be used by default.

    Entries produced look like this:
    <timestamp> | <levelname><coloredblock> (<name>:<pid>)<file>:<lineno>:<function>
                             <coloredblock> <message>
                             <coloredblock> <message continued>
    '''
    _COLORS = {
        logging.INFO: "autobgwhite",
        logging.WARNING: "autobgyellow",
        logging.ERROR: "autobgred",
        logging.CRITICAL: "autobgred",
        SEND: "autobgcyan",
        RECV: "autobgmagenta",
    }

    def __init__(self, fmt=None, datefmt=None, colored=True):
        '''
        Creates formatter.

        Checks whether we are capable of displaying colors.
        If not, colors are disabled regardless of the :colored:.

        Also performs MS Windows CMD init for color support, if necessary.
        '''

        super(DefaultFormatter, self).__init__(
            fmt=fmt, datefmt=datefmt or "%Y.%m.%d %H:%M:%S"
        )
        self.wrapper = TextWrapper()
        self.colored = colored and sys.__stderr__.isatty()
        self.join_str = "\n"

        self._wrap = self.wrapper.wrap
        self._indent = "{}{{0}} ".format(" " * 33)
        self._header = (
            "{inds}{asctime}.{msecs:003.0f} |{levelname:>8}{inde}{block}"
            " {{b}} {name}:{process} {filename}:{lineno}:{funcName}(){{/b}}\n"
        )

        if not self.colored:
            self._wrap = lambda x: [x]
            self._indent = ""
            self._header = (
                "{asctime}.{msecs:003.0f} |{levelname:>8}{block}"
                " {name}:{process} {filename}:{lineno}:{funcName}():"
            )
            self.join_str = ""
        elif os.name == 'nt':
            Windows.enable(auto_colors=True, reset_atexit=True)

    def format(self, record):
        '''
        Formats incoming message according to look described in main doc.
        '''
        try:
            msg = record.getMessage()
        except TypeError:
            msg = str(record.msg)

        record.asctime = self.formatTime(record, self.datefmt)
        record.block = (self.colored and "{{{0}}} {{/{0}}}" or " ").format(
            self._COLORS.get(record.levelno, "invis")
        )
        if self.colored and record.levelno == logging.CRITICAL:
            record.inds, record.inde = ("{negative}", "{/negative}")
        else:
            record.inds, record.inde = ("", "")

        # Arbitrary number is for cases like supervisor'd
        # with self.colored == True
        self.wrapper.width = get_terminal_size().columns or 500
        self.wrapper.initial_indent = self._indent
        self.wrapper.subsequent_indent = self._indent

        if record.exc_info:
            msg = '{}\n{}'.format(
                msg, ''.join(traceback.format_exception(*record.exc_info))
            )
            self.join_str = "\n"

        if record.levelno in [SEND, RECV]:
            self.join_str = "\n"

        msg = self.join_str.join(chain.from_iterable(
            self._wrap(m)
            for m in msg.replace("{", "{{").replace("}", "}}").splitlines()
        ))

        return Color("{}{}".format(self._header, msg).format(
            record.block,
            **record.__dict__
        ))


class LogFileHandler(logging.handlers.TimedRotatingFileHandler):
    '''
    Log handler, puts all logs into a file, rotates files (saves current
    log file and opens new one) daily (by default),
    compresses old ones and deletes oldest to preserve given space limit.
    Also, puts all errors to agent's error channel.queue, config_id,
    '''
    # R0902: Too many instance attributes
    # pylint: disable=R0902
    def __init__(self, filename, when='MIDNIGHT',
                 interval=1, backup_count=14, encoding=None,
                 delay=False, utc=False, max_disk_space=10485760):
        '''
        Returns new instance of LogFileHandler.
        :param filename: specified file is opened and used as the stream for logging.
            On rotating it also sets the filename suffix
        :param when: type of interval: 'S' - seconds, 'M' - minutes, 'H' - hours
            'D' - Days, 'W0'-'W6' - weekday (0=Monday), 'MIDNIGHT' - roll over at midnight
        :param interval: rotating happens based on the product of when and
            interval, if `when` is 'MIDNIGHT' or 'W0'-'W6' interval is ignored
        :param encoding: file encoding
        :param delay: if true, file opening is deferred until the first log occurs.
        :param utc: if true, utc time is used rather then localtime
        :param max_disk_space: given disk space limit for storing log files, in bytes
        '''
        # R0913: Too many arguments
        # pylint: disable=R0913
        super(LogFileHandler, self).__init__(
            filename, when, interval, backup_count, encoding, delay, utc
        )
        self.delay = delay
        # We divide space limit in two, so one half goes for all compressed
        # files, and second one for current log file (uncompressed).
        self.max_disk_space = max_disk_space / 2
        self.start_time = int(time.time())
        # Log file suffix, whith second precision, as even if `when`='MIDNIGHT',
        # due to space restrictions, rotating may occur midday.
        self.log_suffix = "%Y-%m-%d_%H-%M-%S"
        self.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.gz$")
        self.setFormatter(DefaultFormatter(colored=False))

    def shouldRollover(self, record):
        '''
        Determine if rollover should occur - should log files be rotated.
        '''
        if int(time.time()) >= self.rolloverAt:
            return 1
        # Check if current logfile exceeds self.max_disk_space.
        if self.stream is None:
            # Delay was set to True.
            self.stream = self._open()
        msg = '{}\n'.format(self.format(record))
        # Due to non-posix-compliant Windows feature.
        self.stream.seek(0, 2)
        if self.stream.tell() + len(msg) >= self.max_disk_space:
            return 1
        return 0

    def getFilesToDelete(self):
        '''
        Determine the log files to delete when rolling over.
        '''
        # E1103: Maybe no member - `extMatch` might not have `match` member
        # pylint: disable=E1103
        dir_name, base_name = os.path.split(self.baseFilename)
        file_names = os.listdir(dir_name)
        prefix = base_name + '.'
        log_files = [
            os.path.join(dir_name, file_name) for file_name in file_names
            if file_name.startswith(prefix)
            and self.extMatch.match(file_name.strip(prefix))
        ]
        log_files.sort(reverse=True)
        used_space = 0
        i = 0
        for log_file in log_files:
            used_space += os.path.getsize(log_file)
            if used_space > self.max_disk_space:
                break
            i += 1
        return log_files[min(i, self.backupCount):]

    def doRollover(self):
        '''
        Does a rollover - rotates log files.

        Close current log file, compress it,
        check used max_disk_space and delete oldest files to fit in disk space limit,
        open new filestream.
        '''
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.utc:
            time_tuple = time.gmtime(self.start_time)
        else:
            time_tuple = time.localtime(self.start_time)
        dfn = '{}.{}.gz'.format(
            self.baseFilename,
            time.strftime(self.log_suffix, time_tuple)
        )
        if os.path.exists(dfn):
            os.remove(dfn)
        if os.path.exists(self.baseFilename):
            f_in = open(self.baseFilename, 'rb')
            f_out = gzip.open(dfn, 'wb')
            f_out.writelines(f_in)
            f_out.close()
            f_in.close()
        for old_file in self.getFilesToDelete():
            os.remove(old_file)
        if not self.delay:
            os.remove(self.baseFilename)
            self.stream = self._open()
        self.start_time = int(time.time())
        current_time = int(time.time())
        dst_now = time.localtime(current_time)[-1]
        new_rollover_at = self.computeRollover(current_time)
        while new_rollover_at <= current_time:
            new_rollover_at = new_rollover_at + self.interval
        # If DST changes and midnight or weekly rollover, adjust for this.
        if (self.when == 'MIDNIGHT' or self.when.startswith('W')) and not self.utc:
            dst_at_rollover = time.localtime(new_rollover_at)[-1]
            if dst_now != dst_at_rollover:
                if not dst_now:
                    # DST kicks in before next rollover, so deduct an hour.
                    addend = -3600
                else:
                    # DST bows out before next rollover, so add an hour.
                    addend = 3600
                new_rollover_at += addend
        self.rolloverAt = new_rollover_at


class AgentErrorLogHandler(logging.Handler):
    '''
    Agent custom handler for putting error and critical logs
    into agent's error channel.
    '''
    def __init__(self, queue, config_id):
        super(AgentErrorLogHandler, self).__init__(logging.ERROR)
        self.queue = queue
        self.config_id = config_id

    def emit(self, record):
        msg = {
            'config_id': self.config_id,
            'data': record.getMessage(),
            'datatype': str,
            'timestamp': datetime.datetime.utcnow(),
            'stream_name': '_error'
        }
        self.queue.put(msg)


def __init_logging():
    '''
    Initializes system-wide logging infrastructure.
    '''
    logging.addLevelName(SEND, 'SEND')
    logging.addLevelName(RECV, 'RECV')
    logging.addLevelName(UBERDEBUG, 'UBERDEBUG')

    def send(self, msg, *args, **kwargs):
        '''
        Helper for logging incoming messages.
        '''
        self.log(SEND, json.dumps(msg, indent=2), args, **kwargs)

    def recv(self, msg, *args, **kwargs):
        '''
        Helper for logging outgoing messages.
        '''
        self.log(RECV, json.dumps(msg, indent=2), args, **kwargs)

    def uberdebug(self, msg, *args, **kwargs):
        '''
        Helper for logging even more debug.
        '''
        self.log(UBERDEBUG, msg, args, **kwargs)

    def excepthook(self, ex_cls, ex, tback):
        '''
        Helper for capturing exceptions.
        '''
        self.critical("Uncaught exception", exc_info=(ex_cls, ex, tback))

    # This is generally bad, we should probably subclass Logger and set it
    # as default in manager, but others (*pointing at Celery*) are patching
    # manager as well...
    logging.Logger.send = send
    logging.Logger.recv = recv
    logging.Logger.uberdebug = uberdebug
    logging.Logger.excepthook = excepthook
    __handler = logging.StreamHandler()
    __handler.setFormatter(DefaultFormatter())
    __logger = logging.getLogger("monitowl")
    __logger.addHandler(__handler)
    __logger.propagate = False

    # Let's do this for Tornado as well!
    logging.getLogger("tornado").addHandler(__handler)

    # Let's make Celery behave a bit, too.
    # We have to set level here, because `--loglevel` in Celery
    # sets *root loggers' level*, which we definitely don't want.
    __celery = logging.getLogger("celery")
    __celery.setLevel(logging.WARNING)
    __celery.addHandler(__handler)

    sys.excepthook = __logger.excepthook

    def __set_level(level):
        '''
        Helper for switching log levels with signals.
        '''
        __logger.setLevel(level)
        __logger.debug(
            "Log level switched to `%s`", logging.getLevelName(level),
        )

    __rtmin = signal.SIGRTMIN
    for i, level in enumerate([UBERDEBUG, RECV, SEND, logging.DEBUG]):
        signal.signal(__rtmin + i, lambda s, f, l=level: __set_level(l))

# At module level, because we want to do this only once
__init_logging()
