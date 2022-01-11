#!/usr/bin/env python3

import signal
import subprocess
import sys
import threading

from . import streamq


class ProcRunner():
    def __init__(self, name=None, command=None, cwd=None, parent=None, printer=None, env=None, color=None):
        if name is None:
            print('-err- No name provided')
            sys.exit(-1)
        if command is None:
            print('-err- No command provided')
            sys.exit(-2)

        self.returncode = None
        self.cwd = cwd
        self.name = name
        self.command = command
        self.parent = parent
        self.env = env
        self.ph = None
        self.queues = {
            'out': streamq.StreamQ(name=':'.join([name,'out']), printer=printer),
            'err': streamq.StreamQ(name=':'.join([name,'err']), printer=printer),
        }

        if printer and color:
            printer.setColor(':'.join([name,'out']), color)
            printer.setColor(':'.join([name,'out']), color)

        self.threads = {}
        self.pipes = {}

    def getName(self):
        return self.name

    def getQueues(self):
        return self.queues

    def returnCode(self):
        return self.returncode;

    def waitForExit(self):
        if not self.proc_handle:
            return

        # block until the process exits
        self.proc_handle.wait()

        # docs say returncode is "Exit status of the child process. If
        # the process exited due to a signal, this will be the
        # negative signal number."
        retcode = self.proc_handle.returncode

        if retcode < 0:
            msg = "due to signal %s" % (signal.Signals(-retcode))
        else:
            msg = "with status %d" % (retcode)

        self.parent._printToQueue("%s exited %s" % (self.name, msg))

        self.returncode = retcode

    def start(self):
        try:
            self.proc_handle = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                bufsize=1, # this implies "line buffered", which is exactly what we want
                universal_newlines=True,
                errors='replace',
                close_fds=True)
        except Exception as e:
            self.parent._printToQueue(f"ERROR: could not start {self.command}: {e}")
            self.proc_handle = None
            for q in self.queues.values():
                q.close()
            return False

        self.pipes = {
            'out': self.proc_handle.stdout,
            'err': self.proc_handle.stderr,
        }

        # Start a thread per queue that will copy data from the pipe
        # into a queue
        for qn in self.queues:
            q = self.queues[qn]
            t = threading.Thread(
                target=q.readFromFile,
                args=[self.pipes[qn]])
            self.threads[qn] = t
            t.start()

        # Start a thread that waits for the process to exit
        t = threading.Thread(
            target=self.waitForExit
        )
        t.start()
        self.threads['waitfor'] = t
        return True

    def running(self):
        if self.proc_handle is None:
            return False
        return self.proc_handle.poll() == None

    def done(self):
        return not self.running()

    def stop_and_wait(self):
        # kill the process if it's still running
        if self.proc_handle and self.running():
            try:
                self.proc_handle.terminate()
            except Exception as e:
                print('Exception {} while killing {}; probably died before we could kill it.'.format(repr(e),self.name))

        # wait for all threads to exit
        for t in self.threads.values():
            t.join()


if __name__ == '__main__':

    import time

    qp = streamq.Printer()
    qp.start()

    pms = []
    people = {'Ann':0.1,'Bob':0.2,'Cat':0.3,'Dog':0.4}
    for name in people:
        pms.append(ProcRunner(name=name, command=['./me.sh',name,str(people[name])], printer=qp))

    for pm in pms:
        print("starting " + repr(pm))
        pm.start()

    done = False
    while not done:
        for pm in pms:
            if pm.done():
                done = True
        time.sleep(0.5)
