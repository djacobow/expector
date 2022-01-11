#!/usr/bin/env python3

import sys
sys.path.append('../../')
from expector import expector 

class Bob(expector.SeqRunnerAddOn):
    def __init__(self):
        self.burp = 'burp'
        pass

    @expector.makeCallable
    def boop(self, sr, rv, args, kw):
        sr.message(text=repr(args))
        sr.message(text=f'BOOP {self.burp}')

    @expector.makeCallable
    def beep(self, sr, rv, args, kw):
        sr.message(text=repr(kw))
        sr.message(text=f'BEEP {self.burp}')


if __name__ == '__main__':
    def test(sr):
        sr.message(title='start',text='This is the start')
        sr.boop(1,2,3)
        sr.beep(a='b',c='d')
        sr.message(title='end',text='This is the end')

    expector.run_testfn(test, addons=[Bob()])
