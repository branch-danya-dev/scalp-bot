"""Read current worker resource counters without a dependency or privileges."""
import os
import time


def process_stats():
    result={"cpu_seconds":time.process_time(),"rss_mb":None,"peak_rss_mb":None}
    if os.name=="nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[("cb",wintypes.DWORD),("PageFaultCount",wintypes.DWORD)]+[
                (name,ctypes.c_size_t) for name in ("PeakWorkingSetSize","WorkingSetSize",
                 "QuotaPeakPagedPoolUsage","QuotaPagedPoolUsage","QuotaPeakNonPagedPoolUsage",
                 "QuotaNonPagedPoolUsage","PagefileUsage","PeakPagefileUsage")]
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        psapi=ctypes.WinDLL("psapi",use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        counters=Counters();counters.cb=ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
            result.update(rss_mb=counters.WorkingSetSize/1024**2,peak_rss_mb=counters.PeakWorkingSetSize/1024**2)
    return result
