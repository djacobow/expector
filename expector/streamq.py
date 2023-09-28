#!/usr/bin/env python3

import os
import queue
import re
import sys
import threading
import time

from . import xterm_colors

# these are defaults that can be overriden from tests by assigning
# color = { ... } in tr.start() and tr.connect() calls
# you can set fg (foreground), bg (backgroun), bold and
# underline
QPRINTER_DEFAULT_COLORS = {
    'SeqRunner': {
        'dark':  { 'fg': 'DeepSkyBlue1', 'bold': False },
        'light': { 'fg': 'NavyBlue', 'bold': False },
    },
    'reflector:out': {
        'dark': { 'fg': 'Cyan3', 'bold': False },
        'light': { 'fg': 'DarkRed', 'bold': False },
    },
    'reflector:err': {
        'dark': { 'fg': 'Cyan2', 'bold': False},
        'light': { 'fg': 'DarkRed', 'bold': False},
    },
    'target:out': {
        'dark': { 'fg': 'MistyRose3', 'bold': False },
        'light': { 'fg': 'DeepPink4', 'bold': False },
    },
    'target:err': {
        'dark': { 'fg': 'MistyRose1', 'bold': False},
        'light': { 'fg': 'DeepPink4', 'bold': False},
    },
    'sdk:out': {
        'dark':  { 'fg': 'DodgerBlue3', 'bold': False},
        'light':  { 'fg': 'DeepSkyBlue2', 'bold': False},
    },
    'sdk:err': {
        'dark':  { 'fg': 'DodgerBlue3', 'bold': True  },
        'light':  { 'fg': 'DeepSkyBlue2', 'bold': True  },
    },
}

QPRINTER_ALERT_COLORS = {
    'ERROR':         { 'fg': 'Red',    'bg': 'White'  },
    'WARNING':       { 'fg': 'Yellow', 'bg': 'Black'  },
    'WARN':          { 'fg': 'Yellow', 'bg': 'Black'  },
    'FAILED':        { 'fg': 'Yellow', 'bg': 'Black'  },
    'FAIL':          { 'fg': 'Yellow', 'bg': 'Black'  },
    'DFATAL':        { 'fg': 'White',  'bg': 'Red'  },
    'FATAL':         { 'fg': 'White',  'bg': 'Red'  },
}


class Printer():
    def __init__(self, force_color = False):
        self.q = queue.Queue()
        self.running = False
        self.starttime = time.time()
        self.colorscheme = os.environ.get('TESTRUNNER_COLORSCHEME','dark')
        self.do_colorize = sys.stdout.isatty() or force_color
        # these are defaults that can be overriden from tests by assigning
        # color = { ... } in tr.start() and tr.connect() calls
        self.colortable = QPRINTER_DEFAULT_COLORS
        self.alert_colors = QPRINTER_ALERT_COLORS
        self.last_format_header = None

        # some aliases because we use a bunch of names in our scripts
        self.colortable['console'] = self.colortable['SeqRunner']
        color_aliases = {
            'console':      'target',
            'p0':           'reflector',
            'p1':           'target',
        }
        for alias in color_aliases:
            for subpipe in ('out','err'):
                self.colortable[':'.join([alias,subpipe])] = self.colortable[':'.join([color_aliases[alias],subpipe])]

    def put(self,v):
        self.q.put({'ts': time.time(), 'v': v})

    def stop(self):
        self.running = False

    def empty(self):
        return self.q.empty()

    def setColor(self, name, color):
        self.colortable[name] = color

    def colorChunk(self, chunk, line_color):
        ch_color = self.alert_colors.get(chunk, line_color)
        return xterm_colors.colorize256(chunk, ch_color) if ch_color else chunk

    def formatLine(self, ts, name_parts, line):
        main_name = name_parts[0] if len(name_parts) else ''
        sub_name  = name_parts[1] if len(name_parts) > 1 else ''

        out_ts_str = ts_str   = f'{ts:8.3f}'
        full_name = ':'.join(name_parts)
        out_name_str = name_str = f'{full_name:>17}'

        if self.last_format_header is not None:
            if ts_str == self.last_format_header['ts']:
                out_ts_str = ' ' * len(ts_str)
            if name_str == self.last_format_header['name']:
                out_name_str = ' ' * len(name_str)
                    
        line = ''.join([c if c.isprintable() else '' for c in line])

        msg = f'{out_ts_str} │ {out_name_str} │ {line}'

        self.last_format_header = {
            'ts': ts_str, 'name': name_str
        }
        return msg

    def printLoop(self):
        while self.running:
            alert_split = '|'.join(self.alert_colors.keys())
            alert_regex = ''.join(['(',alert_split,')'])
            try:
                v = self.q.get(True,timeout=2)
                if v is not None:
                    timestamp = v['ts'] - self.starttime
                    name_parts = v['v']['name'].split(':')
                    line     = v['v']['line']
                    msg      = self.formatLine(timestamp, name_parts, line)
                    if self.do_colorize:
                        line_color = self.colortable.get(v['v']['name'],{}).get(self.colorscheme,None)
                        cchunks = [ self.colorChunk(ch,line_color) for ch in re.split(alert_regex, msg) ]
                        msg = ''.join(cchunks)
                    print(msg)
            except queue.Empty:
                pass
            except TimeoutError:
                pass

    def start(self):
        if not self.running:
            t = threading.Thread(
                target=self.printLoop,
                args=[]
            )
            t.daemon = True
            self.thread = t
            self.running = True
            t.start()

class StreamQ():
    def __init__(self,name,printer=None, handler=None, context=None, failpats=None):
        self.name = name
        self.printer = printer
        self.q = queue.Queue()
        self._closed = False
        # if a handler is provided, it is called with every
        # line received. The handler receives the line and
        # an optional context reference
        self.handler = handler
        self.handler_context = context

        self.failpats = []
        self.failed = False
        self.failed_pattern = None
        self.setFailurePatterns(failpats)
 
    # if "failpats" are provided, then they are used to catch
    # bad strings without a specific lookfor. Basically, if this
    # string ever appears, it generates an exception immediately 
    def setFailurePatterns(self, failpats):
        if failpats is not None:
            if isinstance(failpats,str):
                self.failpats = [ re.compile(failpats) ]
            elif isinstance(failpats,(list, tuple)):
                self.failpats = [ re.compile(fp) for fp in failpats ]

    def hasFailed(self):
        return self.failed
       
    def failedPattern(self):
        return self.failed_pattern

    def put(self,v):
        assert not self.closed()

        # allow inclusion of non-string things, but not null str's
        if v is not None and not (isinstance(v,str) and not len(v)):
            if self.printer:
                self.printer.put({'name':self.name,'line':v})
            # the user-supplied handler is run on every line, if
            # present. The handler is actually allowed to consume
            # the line, so that it will not be visible to lookfor()
            # commands, or it can simply return the stream it was
            # given, and it will be put into the search queue as
            # normal. A third possiblity is that the handler will
            # transform the string in some way and *that* will go
            # into the search queue. I'm not sure if that's a good
            # idea or not, but it's interesting.
            if self.handler is not None:
                nv = self.handler(v, self.handler_context)
                if nv is not None:
                    self.q.put(nv)
            else:
                self.q.put(v)

            for fp in self.failpats:
                fm = fp.search(v)
                if fm:
                    msg = (
                        '<<< ERROR: Failure pattern found in stream. >>>>',
                        f'    pat   : {fp.pattern}',
                        f'    match : {fm}'
                    )
                    for l in msg:
                        self.printer.put({'name':self.name,'line':l})
                    self.failed = True
                    self.failed_pattern = fp.pattern


    def close(self):
        self.put("<<EOF>>")
        self._closed = True

    def closed(self):
        return self._closed

    def empty(self):
        return self.q.empty()

    def flush(self):
        while not self.q.empty():
            dummy = self.q.get(False)

    def get(self):
        if not self.q.empty():
            return self.q.get(False)
        return None

    def readFromFile(self, f):
        while True:
            try:
                line = f.readline()
                if isinstance(line, bytes):
                    line = line.decode('utf-8',errors='replace')
            except Exception as e:
                self.put("<<Exception on stream: %s>>" % (e))
                f.close()
                self.close()
                return

            if not line:
                f.close()
                self.close()
                return

            self.put(line.rstrip())

if __name__ == '__main__':
    from time import sleep
    printer = Printer()
    printer.start()

    q0 = StreamQ("one",printer)
    q1 = StreamQ("two",printer)
    for i in range(10):
        q0.put(str(i))
        q1.put(str(i<<1))
    while not printer.empty():
        pass
