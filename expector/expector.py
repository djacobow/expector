#!/usr/bin/env python3

import base64
import datetime
import functools
import inspect
import json
import json
import os
import platform
import re
import serial
import socket
import statistics
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import types
import uuid

from . import streamq
from . import procrun 

default_failpats = [
    r'FATAL',
    r'Internal error: Oops',
    r'Kernel panic',
]

# items in this list will cause a slack message to be sent
notifiable_failpats = [
    r'Internal error: Oops',
    r'Kernel panic',
]

# default checkers to be used if test step does
# not provide one.
def defaultLookChecker(m,l):
    return True if m else False
def defaultShellChecker(r):
    return True if r['code'] == 0 else False


def dprint(*args, **kwargs):
    if True:
        print('-seqrunner-DEBUG- ',end='')
        print(*args, **kwargs)


class StepEncoder(json.JSONEncoder):
    def default(self, obj):
        if callable(obj):
            if obj == defaultLookChecker:
                return 'match_only'
            else:
                return repr(obj)
        if isinstance(obj, (bytes,bytearray,)):
            try:
                return base64.b64encode(obj).decode('ascii')
            except Exception as e:
                return repr(obj)
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        if hasattr(obj,'toJSON'):
            return obj.toJSON()
        try:
            res = json.JSONEncoder.default(self, obj)
        except Exception as e:
            res = 'could not encode to json ({})'.format(repr(e))
        return res


# all seqrunner public functions will return an instance
# of this class, which can also carry a payload of returned
# "user data" as well as other info about the command, such
# as argument validation problems, etc.
class trReturnValue():
    def __init__(self):
        self._ok = True
        self._validation_errors = []
        self._userdata = None
        self.leftover_args = {
            'args': [],
            'kwargs': {},
        }

    def fail(self):
        self._ok = False

    def succeed(self):
        self._ok = True

    def set_ok(self,v):
        self._ok = v == True

    def ok(self):
        return len(self._validation_errors) == 0 and self._ok

    def validation(self):
        return self._validation_errors

    def validated(self):
        return len(self._validation_errors) == 0

    def userdata(self):
        return self._userdata

    def set_userdata(self,ud):
        self._userdata = ud

    def set_bad_arg(self,msg):
        self._validation_errors.append(msg)

    def check_remaining_args(self,args,kw):
        self.leftover_args = {
            'args': args,
            'kwargs': kw,
        }
        if len(args) or len(kw.keys()):
            self.fail()
            return True
        return False

    def get_leftovers(self):
        if len(self.leftover_args['args']) or len(self.leftover_args['kwargs']):
            return self.leftover_args
        return None


class StepFailError(Exception):
    pass

class StepValidateError(Exception):
    pass


# add this decorator if you do not want the step to print
# its name and arguments. You can still do something in
# the function, but some functions like insist(), you do
# not want to see unless they fail
def trNoSelfAnnouce(f):
   setattr(f,'suppress_showstep',True)
   return f

# this decorator is used to add some boilerplate to every
# public function, particularly the ignore_fail option and
# printing validation failures
def trUserCallable(f):
    def wrapper(self, *args, **kw):
        # before each command, check to see if any of exceptions
        # from the stream gathering threads are pending.
        # This isn't ideal, but it does allow for "passive"
        # detection of problem strings in streams
        self._raiseOnQueueFailure()

        rv = trReturnValue()
        fail_ok = kw.pop('ignore_failure',False) or self.global_ignore_failure

        suppress_show_step = getattr(f,'suppress_showstep',False) or kw.pop('suppress_showstep',False)

        if not suppress_show_step:
            self._showStep(f.__name__, kw)
        f(self, rv, list(args), kw)

        if not rv.validated():
            print('Validation failure: {}'.format(', '.join(rv.validation())))
            self.test_ok = False
            raise StepValidateError()
            return

        if not rv.ok():
            unconsumed = rv.get_leftovers()
            if unconsumed:
                self._showUnknownArguments(f.__name__,unconsumed)
            if fail_ok:
                if self.global_ignore_failure:
                    # we still want to mark the failure in this case
                    self.test_ok = False
                if not suppress_show_step:
                    self._printToQueue('warning; step failed; ignoring')
            else:
                self._printToQueue(f'step {f.__name__} FAILED.')
                self.test_ok = False
                raise StepFailError()

        return rv

    # this is used to identify our user callable functions, which
    # the configure function uses to check to see if a function
    # is elegible to be pre-configured with default values.
    setattr(wrapper, 'tr_name', f.__name__)
    return wrapper




class SeqRunner():

    def __init__(self, printer):
        self.printer = printer
        self.runnable = False
        self.processes = {}
        self.sockets = {}
        self.queues = {
            'SeqRunner:out': streamq.StreamQ(name='SeqRunner', printer=printer)
        }
        self.test_ok = False
        self.command_configs = {}
        self.tempdir = tempfile.TemporaryDirectory()
        self.global_ignore_failure = False
        self.addons = {} 

    def getTempDir(self):
        return self.tempdir.name

    def resetOK(self):
        self.test_ok = True

    def setFailure(self):
        self.test_ok = False

    def isOK(self):
        for qn in self.queues:
            if self.queues[qn].hasFailed():
                return False
        return self.test_ok

    def getName(self):
        return 'SeqRunner'

    def cleanup(self):
        self._printToQueue('Terminating all streams and cleaning up')
        for proc in self.processes.values():
            proc.stop_and_wait()

        self.processes = {}
        self.tempdir.cleanup()

    def _setGlobalIgnoreFailure(self, ignore=False):
        self.global_ignore_failure = ignore

    def getEnv(self, n, alt=None):
        return os.environ.get('BITBUCKET_' + n,
               os.environ.get('HIL_CONTEXT_' + n,
               os.environ.get(n,alt)))

    def _notifySpecialFailures(self, qn):
        # not implemented
        pass

    def _raiseOnQueueFailure(self):
        for qn in self.queues:
            if self.queues[qn].hasFailed():
                self._notifySpecialFailures(qn)
                raise StepFailError()

    def _showUnknownArguments(self, name, unk):
        args = unk['args']
        kw   = unk['kwargs']
        kw_vs = [ '{}={}'.format(x,repr(kw[x])) for x in kw.keys() ]
        if len(args) or len(kw_vs):
            lines = []
            if len(args):
                lines += ['Args:   {}'.format(','.join([repr(x) for x in args]))]
            if len(kw_vs):
                lines += ['KWargs: {}'.format(','.join(kw_vs))]
            self._boxMessage(lines,f'{name} -- unknown argument')

    # expects a function that returns true on success, false on failure, and none
    # on not succeeding or failing just yet
    def _succeedOrTimeout(self, fn, step, timeout, iterdelay = 0.01, message = ''):
        start = time.time()
        while True:
            self._raiseOnQueueFailure()
            if time.time() > (start + timeout):
                self._printToQueue('-ERR- timeout {}'.format(message))
                return False
            success = fn(self, step)
            if success is not None:
                return success

            time.sleep(iterdelay)


    def _printToQueue(self,os):
        self.queues['SeqRunner:out'].put(os)

    def _getCmdConfig(self, command, variable, default):
        cmdvars = self.command_configs.get(command,{})
        return cmdvars.get(variable, default)

    def _getCommandVar(self, command, kw, variable, default):
        return kw.pop(variable, self._getCmdConfig(command,variable,default))

    def _setCmdConfig(self, command, variable, value):
        if not command in self.command_configs:
            self.command_configs[command] = {}
        self.command_configs[command][variable] = value;

    def _boxMessage(self,lines,title = None):
        max_line = max([len(x) for x in lines])
        max_line = max(max_line, 80)
        if title is not None:
            tlen = len(title)
            max_line = max([max_line, tlen + 2])
            self._printToQueue('┌─╢' + title + '╟' + '─'*(max_line-len(title)-2) + '┐')
        else:
            self._printToQueue('┌' + '─'*(max_line+1) + '┐')
        for line in lines:
            self._printToQueue('│' + line + ' ' * (1+max_line-len(line)) + '│')
        self._printToQueue('└' + '─'*(max_line+1) + '┘')

    def _wrapLong(self,text):
        lines = textwrap.wrap(text,
            width=75,
            break_on_hyphens=False,
            subsequent_indent='  ',
            initial_indent='  ')
        return lines

    def _showStep(self,name,step):
        if name not in ('message',):
            lines = self._wrapLong(json.dumps(step,cls=StepEncoder))
            self._boxMessage(lines,name)


    ## #####
    ## beyond here are public functions that tests would generally call
    ## #####

    """
    SeqRunner equivalent to "assert".
    The first argument, or "assertion" are what is to be tested.
    "message" is printed if the assertion fails.
    """
    @trUserCallable
    @trNoSelfAnnouce
    def insist(self, rv, args, kw):
        a0 = None
        a1 = None
        if len(args) == 1:
            assertion = args.pop(0)
        elif len(args) == 2:
            a0 = args.pop(0)
            a1 = args.pop(0)
            assertion = a0 == a1
        elif 'assertion' in kw:
            assertion = kw.pop('assertion',False)
        else:
            rv.set_bad_arg('must provide "assertion" clause')

        default_message = 'assertion failed'
        if a0 is not None and a1 is not None:
            default_message = f'assertion {a0} == {a1} FAILED'
        message = kw.pop('message', default_message)

        if rv.check_remaining_args(args,kw):
            return

        if not assertion:
            self._boxMessage([message],'insist - failure')
        rv.set_ok(assertion)


    """
    Start a program in the background and convert it's stdout and stderr
    into streams usable by other SeqRunner commands.

    "args" name the program and its arguments.
 
    "name" is the name to be used for the new streams. Two are created,
    one "name:out" and the other "name:err".

    "env" is an optional OS environment for the program, otherwise you
    get the same one SeqRunner is in.

    Set trace=False if you do not want to see this program's output in
    the log.

    "run_from" lets you specify a starting directory

    """
    @trUserCallable
    def start(self, rv, args, kw):
        startargs = kw.pop('args',None)
        name = kw.pop('name',None)
        trace = kw.pop('trace',True)
        env = kw.pop('env',None)
        color = kw.pop('color',None)
        run_pwd = kw.pop('run_from',None)
        failpats = kw.pop('failpats',default_failpats)

        if rv.check_remaining_args(args,kw):
            return

        if not isinstance(startargs,(list,tuple,)):
            rv.set_bad_arg('missing start arguments')
            return
        if not isinstance(name,str):
            rv.set_bad_arg('missing name in step')
            return

        printer = self.printer if trace else None
        p = procrun.ProcRunner(name=name, command=startargs, cwd=run_pwd, parent=self, printer=printer, env=env, color=color)
        self.processes[name] = p
        if p.start():
            pqueues = p.getQueues()
            for qn in pqueues:
                pqueues[qn].setFailurePatterns(failpats)
                self.queues[':'.join([p.getName(),qn])]  = pqueues[qn]
            return
        self._printToQueue('-ERR- failed to start: {}'.format(p.getName()))
        rv.fail()


    """
    Pause the test for "duration" seconds.

    Use this function as a last resort. A lookfor with a timeout is a 
    much better idea.
    """
    @trUserCallable
    def delay(self, rv, args, kw):
        dur = None
        silent_source = kw.pop('expect_silence_from', None)

        if len(args):
            dur = args.pop(0)
        else:
            dur = kw.pop('duration',None)

        if rv.check_remaining_args(args,kw):
            return

        if dur is None:
            rv.set_bad_arg('missing duration')
            return
        if not isinstance(dur,(int, float,)):
            rv.set_bad_arg('duration is not a number')
            return

        q = None
        if silent_source is not None:
            q = self.queues.get(silent_source)
            if q is None:
                self._printToQueue(f'-ERR- unknown source: {silent_source}')
                rv.fail()
                return

            while not q.empty():
                    l = q.get()

        time.sleep(dur)

        if silent_source is not None:
            if not q.empty():
                lines = []
                while not q.empty():
                    lines += q.get()
                rv.set_userdata({'lines': lines})
                self._printToQueue(f'-ERR- {lines}')
                rv.fail();


    """
    Connect to a tcp port and convert it to a stream for use by other SeqRunner
    functions.

    "name" is what you are going to call the stream.

    "host" should contains the name of the host *and* port.

    trace=True means print everything that comes on this stream.

    "handler" lets you specify your own function to be called on 
    each line received.

    "failpats" let you override the default patterns, which, if seen, will
    cause immediate failure.
    """
    @trUserCallable
    def connect(self, rv, args, kw):
        name = kw.pop('name')
        host = kw.pop('host')
        color = kw.pop('color',None)
        trace = kw.pop('trace',True)
        mode  = kw.pop('mode','text')
        handler = kw.pop('handler',None)
        context = kw.pop('context',None)
        failpats = kw.pop('failpats',default_failpats)

        if rv.check_remaining_args(args,kw):
            return

        if handler is not None and not callable(handler):
            rv.set_bad_arg('handler needs to be callable if present')
            return

        if mode not in ('text','json'):
            rv.set_bad_arg('mode can only be "text" or "json"')
            return

        if not isinstance(host,(tuple,list,)):
            rv.set_bad_arg('host should be a tuple of hostname and port')
            return

        if not isinstance(name,str):
            rv.set_bad_arg('connection needs a name')
            return


        # try to open the socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(host)
            self._printToQueue('-info- socket opened successfully')
        except Exception as e:
            self._printToQueue('-ERR- could not connect to {}: {}'.format(host, repr(e)))
            rv.fail()
            return

        # create a printer and queue
        printer = self.printer if trace else None
        if printer and color:
            printer.setColor(name, color)
        q = QWrap(name=name, printer=printer, handler=handler, context=context, failpats=failpats)
        self.queues[name] = q

        # start the socket read thread
        f = s.makefile('rb', errors='replace')
        t = threading.Thread(
            target=q.readFromFile,
            args=[f]
        )
        t.daemon = True

        self.sockets[name] = { 's': s, 'thread': t, 'mode': mode }

        t.start()


    """
    Print something to the console, inside a cute box

    set "text" with the message, and, optionally, "title" with a title
    to be put in the box frame. "obj" can be set with a json-serializable
    object to be printed.
    """
    @trUserCallable
    def message(self, rv, args, kw):
        obj  = kw.pop('obj',None)
        text = kw.pop('text',None)
        title = kw.pop('title',None)

        if rv.check_remaining_args(args,kw):
            return

        if obj is not None and isinstance(obj,(dict,)):
            text = json.dumps(obj,cls=StepEncoder)

        if text is None:
            rv.set_bad_arg('"message" is missing "text"')
            return

        if not isinstance(text,(list,tuple,)):
            text = self._wrapLong(text)

        self._boxMessage(text, title)


    """
    Open a serial port and convert it to a line-based "stream" for use in
    other SeqRunner commands.

    The "name" is the name of the stream you are creating.
   
    Set trace=True if you want to see the stream on the console.

    Set "failpats" if you want to override the default patterns that,
    if seen, will result in immediate failure.

    Set a "handler" if you want to do something with every line received.
    """
    @trUserCallable
    def serial(self, rv, args, kw):
        name = kw.pop('name',None)
        rawport = kw.pop('port',None)
        speed = kw.pop('speed',None)
        color = kw.pop('color',None)
        trace = kw.pop('trace',None)
        handler = kw.pop('handler',None)
        context = kw.pop('context',None)
        failpats = kw.pop('failpats',default_failpats)

        if rv.check_remaining_args(args,kw):
            return

        if handler is not None and not callable(handler):
            return rv.set_bad_arg('handler needs to be callable if present')
        if rawport is None:
            return rv.set_bad_arg('must provide a port')
        if speed is None:
            return rv.set_bad_arg('must provide a speed')
        if name is None:
            return rv.set_bad_arg('connection needs a name')

        try:
            s = serial.Serial(rawport, speed)
            self._printToQueue(f'-info- serial port opened {rawport} {speed}')
        except Exception as e:
            self._printToQueue('-ERR- {}, could not open serial port {}'.format(repr(e), rawport))
            rv.fail()
            return

        def our_sendall(s, b):
            return s.write(b)

        # monkeypatch in "sendall" which our qwrap expects
        s.sendall = types.MethodType(our_sendall, s)

        # create a printer and queue
        printer = self.printer if trace else None
        if printer and color:
            printer.setColor(name, color)
        q = QWrap(name=name, printer=printer, handler=handler, context=context, failpats=failpats)
        self.queues[name] = q

        # start the socket read thread
        t = threading.Thread(target=q.readFromFile, args=[s])
        t.daemon = True

        self.sockets[name] = { 's': s, 'thread': t, }
        t.start()



    """
    Send a string to an already open stream named in the "sink" argument.

    You can do this three ways.
       1. If you are sending a command type of string, you
          can just use "args" to specify a list of tuple of strings to be send as a
          command and arguments.
       2. If you instead set "raw", then you provide a bytes() object to be sent as-is.
       3. If you instead set "json", you can provide a json-serializable object which
          will be serialized and sent
    """
    @trUserCallable
    def send(self, rv, args, kw):
        raw = kw.pop('raw',None)
        sendargs = kw.pop('args',None)
        js  = kw.pop('json',None)
        sink = kw.pop('sink',None)

        if rv.check_remaining_args(args,kw):
            return

        if sink is None:
           rv.set_bad_arg('must provide a named sink')
           return
        if not isinstance(raw,(bytes,bytearray,)) and not isinstance(sendargs,(list,tuple,)) and not isinstance(js,(dict,list,tuple)):
           rv.set_bad_arg('must provide "raw" as bytes, or "args" as list, or "json" as a json-serializable object')
           return

        try:
            if raw is not None:
                self.sockets[sink]['s'].sendall(raw)
            elif js is not None:
                obytes = ''.join([json.dumps(js),'\n']).encode('utf-8')
                self.sockets[sink]['s'].sendall(obytes)
            else:
                strargs = [ x if isinstance(x,str) else str(x) for x in sendargs ]
                outmsg = ' '.join([s.strip() for s in strargs]) + '\r\n'
                obytes = outmsg.encode('utf-8')
                self.sockets[sink]['s'].sendall(obytes)
        except Exception as e:
            self._printToQueue('Exception while sending: {}'.format(repr(e)))
            rv.fail()
            return



    """
    This command is meant to be run on streams that are returning JSON
    blobs, one per line.

    It can work in two modes. If you provide "has" then the command will
    look for blob to contain the keys named.

    If you provide "match" then it will look for the keys named, and
    the values should match.

    It basically a a JSON-analog for "lookfor".
    """
    @trUserCallable
    def lookJS(self, rv, args, kw):
        timeout  = self._getCommandVar('lookfor', kw, 'timeout', 1)
        source   = kw.pop('source',None)
        haslist  = kw.pop('has',[])
        matchobj = kw.pop('match', {})


        if rv.check_remaining_args(args,kw):
            return

        if len(haslist) == 0 and len(matchobj.keys()) == 0:
            rv.set_bad_arg('no "has" or "match" provided')
            return

        if not source in self.queues:
            rv.set_bad_arg('Request to look for data in nonexistent stream')
            return

        def lookJSChecker(self, step):
            q = self.queues[source]

            # Check queue line by line
            while not q.empty():
                d = q.get()
                if d is not None and isinstance(d,str):
                    try:
                        d = json.loads(d)
                    except json.decoder.JSONDecodeError:
                        d = None
                if d is not None and isinstance(d,dict):
                    has_satisfied = True
                    for skey in haslist:
                        if not skey in d:
                            has_satisfied = False
                            break

                    match_satisfied = True
                    for skey in matchobj:
                        if matchobj[skey] != d.get(skey,None):
                            match_satisfied = False
                            break


                    if has_satisfied and match_satisfied:
                        rv.set_userdata(d)
                        self._showStep('lookJS(found)', d)
                        return True


            # If the queue is drained and we know nothing more will ever
            # be added to it, return False
            if q.empty() and q.closed():
                return False

            return None

        rv.set_ok(self._succeedOrTimeout(lookJSChecker, kw, timeout, 0.010,
                  'waiting for match source {}'.format(source)))



    """
    SeqRunner has a handful of default "failure patterns" which will
    cause an error to be noted if they occur in *any* stream.

    You can use this function to change (or remove) those patterns
    for any particular stream.
    """
    @trUserCallable
    def resetFailPatterns(self, rv, args, kw):
        source  = kw.pop('source',None)
        failpats = kw.pop('failpats', [])

        if rv.check_remaining_args(args,kw):
            return
        if source is None:
            rv.set_bad_arg('required source name not specified')
            return
        if failpats is None:
            rv.set_bad_arg('required list of fail patterns is not specified')
            return

        q = self.queues[source]
        q.setFailurePatterns(failpats)


    """
    "Looks for" a pattern in a stream, and if it is not found within
    "timeout" seconds, then the command fails.

    This is basically "the" core function of SeqRunner -- looking for
    patterns in the output of a data source.  The pattern can be a full
    Python regex.

    An optional "failon" parameter can be provided, which is a list of
    patterns which, if found, will result in immediate failure. That is,
    these are things you do NOT want to see.

    An optional "checker" is a callback function you can provide that
    is called on each line where the pattern matches. This lets you 
    run code to make some sub-determination about pass/fail based on
    the match. For example, if the match is on r'result=(\d+)', the
    checker can check the actual number, based on some other context.
    """
    @trUserCallable
    def lookfor(self, rv, args, kw):
        timeout = self._getCommandVar('lookfor', kw, 'timeout', 1)
        checker = self._getCommandVar('lookfor', kw, 'checker', defaultLookChecker)
        source  = kw.pop('source',None)
        failon  = self._getCommandVar('lookfor', kw, 'failon', [])
        strpat  = kw.pop('pattern',None)

        if rv.check_remaining_args(args,kw):
            return

        if strpat is None:
            rv.set_bad_arg('no pattern provided')
            return
        if source is None:
            rv.set_bad_arg('required source name not specified')
            return
        if checker is not None and not callable(checker):
            rv.set_bad_arg('checker must be a function taking a match and a line string')
            return
        if not source in self.queues:
            rv.set_bad_arg('Request to look for data in nonexistent stream')
            return

        pat     = re.compile(strpat)

        if not isinstance(failon,(list,tuple)):
            failon = [ failon ]
        fpats = [ re.compile(f) for f in failon ]

        def lookForChecker(self, step):
            q = self.queues[source]

            # Check queue line by line
            while not q.empty():
                l = q.get()
                if l is not None and len(l):
                    # For each line check if it matches a fail condition
                    for fpat in fpats:
                        fm = fpat.search(l)
                        if fm:
                            self._showStep('lookfor (FAILon)', {'groups': [fm.group(0)] + list(fm.groups())})
                            rv.set_userdata({'match': None, 'fail_match': fm})
                            return False
                    # check for a successful match
                    m = pat.search(l)
                    rv.set_userdata({'match': m, 'fail_match': None})
                    if m:
                        self._showStep('lookfor (found)', {'groups': [m.group(0)] + list(m.groups())})
                        found_match = True
                        return checker(m,l)

            # If the queue is drained and we know nothing more will ever
            # be added to it, return False
            if q.empty() and q.closed():
                return False

            return None

        rv.set_ok(self._succeedOrTimeout(lookForChecker, kw, timeout, 0.010,
                  'waiting for match to {} from source {}'.format(pat,source)))


    """
    Run the command specified with the list "args" on the host and print
    its output. Contrary to the name, the command is not run with the
    shell unless you also set use_shell=True.

    "run_from" lets you set a directory to run from.

    A callbac "checker" can be specified, which will get the command
    output and can do whatever it wants with it; The return value of
    the checker is returned.
    """
    @trUserCallable
    def shell(self, rv, args, kw):
        shellargs = kw.pop('args',None)
        checker = self._getCommandVar('shell', kw, 'checker', defaultShellChecker)
        use_shell = kw.pop('use_shell',False)
        run_from = kw.pop('run_from',None)

        if rv.check_remaining_args(args,kw):
            return

        if not isinstance(shellargs,(list,tuple,)):
            rv.set_bad_arg('missing shell arguments')
            return
        if checker is not None and not callable(checker):
            rv.set_bad_arg('shell return checker not callable')
            return

        try:
            r = shell(shellargs, use_shell=use_shell, cwd=run_from)
            self._printToQueue('shell output: {}'.format(r['output']))
            return checker(r)
        except Exception as e:
            self._printToQueue(repr(e))
            return False



    """
    Blocks until the named stream is closed. This is most useful when
    you have "started" a program and want to wait for it to complete
    before proceeding.

    Note that is returns True if the process exited cleanly (code==0)
    and False otherwise.
    """
    @trUserCallable
    def waitdone(self, rv, args, kw):
        name = kw.pop('name',None)
        if not isinstance(name,str):
            rv.set_bad_arg('missing name in step')
            return

        timeout = kw.pop('timeout', 1)

        if rv.check_remaining_args(args,kw):
            return

        # determine which queues we need to watch for closedness
        # could just assume :out and :err, but this more
        # general
        must_be_closed = set()
        for qn in self.queues:
            qnparts = qn.split(':')
            if len(qnparts) and qnparts[0] == name:
                must_be_closed.add(qn)

        def closedChecker(self, kw):
            proc = self.processes.get(name)
            assert(proc is not None)

            # if the process hasn't terminated yet, continue waiting
            if proc.running():
                return None

            # if any of the queues are still open (i.e. output is still being copied
            # from pipes), continue waiting
            for n in must_be_closed:
                if not self.queues[n].closed():
                    return None

            # return success if the return code is 0
            return proc.returnCode() == 0

        rv.set_ok(self._succeedOrTimeout(closedChecker, kw, timeout, 0.010, 'waiting on {} to exit'.format(name)))


    """
    Consumes (and discards) all available lines from the named stream.
    """
    @trUserCallable
    def slurp(self, rv, args, kw):
        source = kw.pop('source',None)

        if source is None:
            rv.set_bad_arg('must provide a source')
            return

        q = self.queues.get(source, None)
        if q is None:
            self._printToQueue(f'source {source} not found')
            rv.fail();
            return
        while not q.empty():
            l = q.get()


    """
    This command is used to associate a dict of configuration values with 
    an existing SeqRunner command. This lets you set defaults for certain
    commands.

    I do not believe any commands currently make use of this facility.
    """
    @trUserCallable
    def configure(self, rv, args, kw):
        command = kw.pop('command',None)

        if command is None:
            rv.set_bad_arg('config must provide name of function to configure')
            return

        def isCallable(fn_name):
            dundered = re.compile(r'^__.*__$').match(fn_name)
            fn = getattr(self, fn_name)
            is_ours  = getattr(fn,'tr_name',None) is not None
            return not dundered and is_ours and callable(fn)

        cmdlist = filter(isCallable, dir(self))

        if command not in cmdlist:
            rv.set_bad_arg('target name {} is not one of {}'.format(command, ','.join(cmdlist)))
            return

        kw_keys = list(kw.keys())
        for vn in kw_keys:
            if vn not in ('command',):
                self._setCmdConfig(command,vn,kw[vn])
            kw.pop(vn)

        if rv.check_remaining_args(args,kw):
            return


    """
    opens a connection to another machine via ssh and then makes interacting
    with the ssh connection just like dealing with any other stream
    (using .send(), .lookfor(), etc.)

    You can provide an optional handler (and context). If you do, the handler
    will be called with each line received, and provided the context. This
    lets you set up automatic handling for lines received.

    failpats is a list of pattern that, if received, will cause an immediate
    test failure
    """
    @trUserCallable
    def ssh(self, rv, args, kw):
        name = kw.pop('name', None)
        host = kw.pop('host', None)
        user = kw.pop('user','root')
        port = kw.pop('port',None)
        color = kw.pop('color',None)
        trace = kw.pop('trace',True)
        handler = kw.pop('handler',None)
        context = kw.pop('context',None)
        failpats = kw.pop('failpats',default_failpats)

        if rv.check_remaining_args(args,kw):
            return

        if host is None:
            rv.set_bad_arg('You must provide a machine to ssh to')
            return
        if name is None:
            rv.set_bad_arg('ssh connection needs a name')
            return
        if handler is not None and not callable(handler):
            rv.set_bad_arg('handler needs to be callable if present')
            return


        try:
            sshargs = ['ssh', '-t', '-t', '-o', 'StrictHostKeyChecking=no', '{}@{}'.format(user,host) ]
            if port is not None:
                sshargs += ['-p',str(port)]

            s = subprocess.Popen(sshargs,
                shell=False,stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                universal_newlines=True)
        except Exception as e:
            self._printToQueue('-ERR- {}, could not ssh connection {}@{}'.format(repr(e), user, host))
            return False

        def our_sendall(s, b):
            s.stdin.write(b.decode('utf-8'))
            return s.stdin.flush()

        # monkeypatch in "sendall" which our qwrap expects
        s.sendall = types.MethodType(our_sendall, s)

        # create a printer and queue
        printer = self.printer if trace else None
        if printer and color:
            printer.setColor(name, color)
        q = QWrap(name=name, printer=printer, handler=handler, context=context, failpats=failpats)
        self.queues[name] = q

        # start the socket read thread
        t = threading.Thread(target=q.readFromFile, args=[s.stdout])
        t.daemon = True

        self.sockets[name] = { 's': s, 'thread': t, }
        t.start()



    """
    Calls a function provided repeatedly, up to "attempts" times.
 
    Function signature should be fn(tr, context), where tr is handle
    to SeqRunner itself that the function may or may not use, and an
    optional context that the function can access.
 
    Unlike normal operation of SeqRunner, where a single failure in any
    command immediately ends the test, this will allow the function to
    cause failures and still be retried. If the function ever completes
    without a failure, the test proceeds normally. If attempts are
    exhausted and the function still fails, the test fails.

    Note that the function does not need to return a value. This
    uses the testRunner state itself to determine failure. Therefore,
    if your function does not make use of SeqRunner commands, it
    should call .setFailure() to indicat to SeqRunner that it failed.
    (If you do use regular SeqRunner commands like lookFor, you do
    not need to do this.)
    """
    @trUserCallable
    def retry(self, rv, args, kw):
        attempts = kw.pop('attempts', 0)
        function = kw.pop('function', None)
        context  = kw.pop('context', None)

        if rv.check_remaining_args(args,kw):
            return

        if function is None or not callable(function):
            rv.set_bad_arg('You must provide a funcion to run')
            return

        if attempts == 0:
            rv.set_bad_arg('You must set the attempts variable to an interger > 0')
            return

        finish_ok = False
        while not finish_ok and attempts:
            self._setGlobalIgnoreFailure(ignore=True)
            self.resetOK()
            function(self, context=context)
            self._setGlobalIgnoreFailure(ignore=False)
            finish_ok = self.isOK()
            attempts -= 1
        if not finish_ok:
            rv.fail()
            self.setFailure()

    def exit(self):
        if self.isOK():
            dprint('-info- Yes!!! This test passed.')
            sys.exit(0)
        dprint('-ERR- Boo. Fail.')
        sys.exit(-1)

    def registerAddOn(self, aoo=None):
        self.addons[aoo.__class__.__name__] = aoo
        if hasattr(aoo, 'getFunctions'):
            for f in aoo.getFunctions().items():
                nfn = types.MethodType(trUserCallable(f[1]), self)
                setattr(self, f[0], nfn)


def makeCallable(f):
    setattr(f, 'add_to_tr', True)
    return f


class SeqRunnerAddOn(object):
    def __init__(self):
        pass

    def getFunctions(self):
        rv = {};
        for a in dir(self):
            f = getattr(self, a, None)
            if f is not None and callable(f) and getattr(f, 'add_to_tr', False):
                rv[a] = f
        return rv

def run_testfn(testfn, force_color=False, addons=[], doexit=True):

    qp = streamq.Printer(force_color=force_color)
    qp.start()
    sr = SeqRunner(qp)

    for addon in addons:
        sr.registerAddOn(addon)

    sr.resetOK()

    success = True
    try:
        testfn(sr)
    except (expector.StepFailError, expector.StepValidateError) as e:
        # Explicit exceptions from the seqrunner framework when a step fails
        success = False
        pass
    except Exception as e:
        # Uncaught exceptions from the test script itself
        success = False
        print(f'-ERR- uncaught exception {repr(e)} in seqrunner function')
        traceback.print_exc()

    if not sr.isOK():
        success = False

    sr.cleanup()

    while not qp.empty():
        time.sleep(0.1)
        pass

    if success:
        print('-info- Test PASSED!')
    else:
        print('-ERR- FAIL')

    if doexit:
        sr.exit()

    return success


if __name__ == '__main__':
    pass
