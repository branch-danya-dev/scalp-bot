"""PDH pressure sampler around the unchanged historical benchmark.

The only companion thread is measurement: one counter read per second, buffered
in memory; file output after the measured runs. No foreign processes are changed.
"""
import argparse
import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import json
import platform
import threading
import time
from pathlib import Path
from scalp_bot.offline_benchmark import main,stats
from contextlib import contextmanager

class Value(ctypes.Structure):
    _fields_=[('status',wintypes.DWORD),('value',ctypes.c_double)]

class Pressure:
    def __init__(self):
        self.stop=threading.Event();self.samples=[];self.errors={};self.counters={}
        self.pdh=ctypes.WinDLL('pdh');self.query=wintypes.HANDLE()
        self.pdh.PdhOpenQueryW.argtypes=[wintypes.LPCWSTR,ctypes.c_size_t,ctypes.POINTER(wintypes.HANDLE)]
        self.pdh.PdhAddEnglishCounterW.argtypes=[wintypes.HANDLE,wintypes.LPCWSTR,ctypes.c_size_t,ctypes.POINTER(wintypes.HANDLE)]
        self.pdh.PdhCollectQueryData.argtypes=[wintypes.HANDLE]
        self.pdh.PdhGetFormattedCounterValue.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,ctypes.POINTER(Value)]
        self.pdh.PdhCloseQuery.argtypes=[wintypes.HANDLE]
        code=self.pdh.PdhOpenQueryW(None,0,ctypes.byref(self.query))
        if code:raise RuntimeError(('PdhOpenQuery',code))
        for name,path in dict(cpu_percent=r'\Processor(_Total)\% Processor Time',available_ram_mb=r'\Memory\Available MBytes',pages_per_sec=r'\Memory\Pages/sec',disk_queue=r'\PhysicalDisk(_Total)\Avg. Disk Queue Length',disk_bytes_sec=r'\PhysicalDisk(_Total)\Disk Bytes/sec').items():
            handle=wintypes.HANDLE();code=self.pdh.PdhAddEnglishCounterW(self.query,path,0,ctypes.byref(handle))
            if code:self.errors[name]=code
            else:self.counters[name]=handle
        self.thread=threading.Thread(target=self.run,name='benchmark-pressure',daemon=True)
    def run(self):
        self.pdh.PdhCollectQueryData(self.query)
        while not self.stop.wait(1):
            sample={'perf_ns':time.perf_counter_ns(),'wall_ns':time.time_ns()};code=self.pdh.PdhCollectQueryData(self.query)
            if code:sample['collect_error']=code
            for name,handle in self.counters.items():
                v=Value();code=self.pdh.PdhGetFormattedCounterValue(handle,0x200,None,ctypes.byref(v))
                sample[name]=v.value if code==0 and v.status in (0,1) else None
            self.samples.append(sample)
    def close(self):
        self.stop.set();self.thread.join(2);self.pdh.PdhCloseQuery(self.query)

@contextmanager
def timer_resolution(milliseconds):
    if milliseconds is None:
        yield {'requested_ms':None,'changed':False};return
    if platform.system()!='Windows':raise RuntimeError('Windows timer profile requested on another OS')
    winmm=ctypes.WinDLL('winmm')
    result=winmm.timeBeginPeriod(milliseconds)
    if result:raise RuntimeError(('timeBeginPeriod',result))
    try:yield {'requested_ms':milliseconds,'changed':True,'api':'timeBeginPeriod/timeEndPeriod; scoped to this benchmark process; not a launcher change'}
    finally:
        result=winmm.timeEndPeriod(milliseconds)
        if result:raise RuntimeError(('timeEndPeriod',result))

async def idle_timer(samples=1000):
    values=[]
    for _ in range(samples):
        before=time.perf_counter_ns();await asyncio.sleep(.001)
        values.append(max(0,(time.perf_counter_ns()-before-1_000_000)/1e6))
    return dict(metric='same 1ms sleep deadline lateness; no engine/worker',stats=stats(values),raw_ms=values)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('model');p.add_argument('output');p.add_argument('--offset',type=int,required=True);p.add_argument('--timer-ms',type=int,choices=[1]);a=p.parse_args()
    output=Path(a.output);output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists() or output.with_suffix('.pressure.json').exists():raise FileExistsError(output)
    idle_before=asyncio.run(idle_timer())
    with timer_resolution(a.timer_ms) as timer_profile:
        idle_requested=asyncio.run(idle_timer())
        pressure=Pressure();pressure.thread.start();started=time.time()
        try:asyncio.run(main(a.source,a.model,a.output,60,3,a.offset))
        finally:
            pressure.close()
            output.with_suffix('.pressure.json').write_text(json.dumps(dict(started_wall=started,ended_wall=time.time(),counter_errors=pressure.errors,samples=pressure.samples,platform=platform.platform(),harness_sha256=hashlib.sha256(Path('scalp_bot/offline_benchmark.py').read_bytes()).hexdigest(),timer_profile=timer_profile,idle_before=idle_before,idle_requested=idle_requested),separators=(',',':'))+'\n')
