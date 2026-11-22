"""Run software acceptance without querying hardware or touching user history."""
import argparse, contextlib, json, os, re, sys, time, unittest
from pathlib import Path

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--output',required=True)
args=parser.parse_args()
out=Path(args.output).resolve()
out.mkdir(parents=True,exist_ok=True)
os.environ['ATK_TEST_TMP']=str(out/'temporary')
os.environ['ATK_BATTERY_DATA_DIR']=str(out/'isolated-runtime')
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.path.insert(0,str(root))

class TimedResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.timings=[]
    def startTest(self,test):
        self._started=time.perf_counter();super().startTest(test)
    def stopTest(self,test):
        self.timings.append(dict(test=test.id(),elapsed_ms=round((time.perf_counter()-self._started)*1000,3)))
        super().stopTest(test)

with (out/'software-tests.log').open('w',encoding='utf-8') as f, contextlib.redirect_stdout(f),contextlib.redirect_stderr(f):
    suite=unittest.defaultTestLoader.discover(str(root/'tests'),pattern='test_*.py')
    result=unittest.TextTestRunner(stream=f,verbosity=2,resultclass=TimedResult).run(suite)
log=(out/'software-tests.log').read_text(encoding='utf-8')
summary=dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),
             successful=result.wasSuccessful(),metrics={k:float(v) for k,v in re.findall(r'METRIC ([a-z_]+)=([\d.]+)',log)},
             timing=result.timings,hardware_access=False)
(out/'software-tests.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k!='timing'},ensure_ascii=False))
raise SystemExit(0 if result.wasSuccessful() else 1)
