"""Bounded, real-device UI acceptance; closes normally after two queries."""
from pathlib import Path
import argparse,json,logging,os,sys,time
from logging.handlers import RotatingFileHandler
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--output', required=True)
parser.add_argument('--preview', action='store_true', help='No HID calls; render pages only')
args=parser.parse_args()
out=Path(args.output).resolve()
out.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(root))
if not args.preview:
    os.environ['ATK_BATTERY_DATA_DIR']=str(out/'isolated-runtime')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from dashboard import Dashboard,singleton
from data_store import APP_DIR
lock=singleton()
if lock is None:raise SystemExit('Project instance already running; no acceptance launched')
APP_DIR.mkdir(parents=True,exist_ok=True)
logging.basicConfig(handlers=[RotatingFileHandler(APP_DIR/'app.log',maxBytes=1_000_000,backupCount=3,encoding='utf-8')],
                    level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s',force=True)
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
w=Dashboard(start_worker=not args.preview);w.show()
records=[];click_at=None;exit_at=None
def save():
    report=dict(events=records,worker_stopped=not w.worker or not w.worker.isRunning(),
                exit_wait_ms=round((time.perf_counter()-exit_at)*1000,2) if exit_at else None,
                real_device=not args.preview,physical_mouse_state='uncontrolled',legacy_history_not_written=True)
    (out/('preview-runtime.json' if args.preview else 'deployed-runtime.json')).write_text(json.dumps(report,default=str,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,default=str,ensure_ascii=False),flush=True)
def request_manual():
    global click_at
    click_at=time.perf_counter()
    for _ in range(100):w.refresh()
def completed(e):
    global exit_at
    row={k:v for k,v in e.items() if k!='last_success'}
    row.update(ui_feedback=w.refresh_status.text(),ui_state=w.state_chip.text(),storage_feedback=w.storage_status.text())
    if e.get('manual') and click_at:
        row['ui_feedback_ms']=round((time.perf_counter()-click_at)*1000,2)
    records.append(row)
    if len(records)==1:
        QTimer.singleShot(2000,request_manual)
    elif e.get('manual'):
        w.grab().save(str(out/'deployed-ui.png'))
        exit_at=time.perf_counter()
        QTimer.singleShot(200,w.exit_app)
def safety_stop():
    global exit_at
    exit_at=exit_at or time.perf_counter();w.exit_app()
def render_preview():
    w.grab().save(str(out/'final-ui-top.png'))
    scroll=w.pages.currentWidget()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    app.processEvents()
    w.grab().save(str(out/'final-ui-bottom.png'))
    QTimer.singleShot(200,w.exit_app)
if args.preview:
    QTimer.singleShot(500,render_preview)
else:
    w.worker.result.connect(completed)
    QTimer.singleShot(60000,safety_stop)
app.aboutToQuit.connect(save)
try:code=app.exec()
finally:lock[0].CloseHandle(lock[1])
raise SystemExit(code)
