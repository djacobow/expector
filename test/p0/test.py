#!/usr/bin/env python3

import sys
sys.path.append('../../')

from expector import expector 

if __name__ == '__main__':
    def test(sr):
        sr.start(name='p0', args=['./counter.sh', '0.5'], trace=True)

        sr.lookfor(source='p0:out', pattern=r'1', timeout=5)
        sr.lookfor(source='p0:out', pattern=r'3', timeout=5)
        sr.lookfor(source='p0:out', pattern=r'5', timeout=5)
        sr.lookfor(source='p0:out', pattern=r'7', timeout=5)

        sr.start(name='p1', args=['./counter.sh', '0.5'], trace=True)

        sr.lookfor(source='p1:out', pattern=r'1', timeout=5)
        sr.lookfor(source='p0:out', pattern=r'9', timeout=5)
        sr.lookfor(source='p1:out', pattern=r'3', timeout=5)
        sr.lookfor(source='p0:out', pattern=r'11', timeout=5)
        sr.lookfor(source='p1:out', pattern=r'5', timeout=5)
        sr.lookfor(source='p0:out', pattern=r'13', timeout=5)
        sr.lookfor(source='p1:out', pattern=r'7', timeout=5)
        sr.lookfor(source='p0:out', pattern=r'15', timeout=5)
     
    expector.run_testfn(test)
