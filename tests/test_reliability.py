"""Deterministic fault injection: never enumerate or query real hardware."""
import csv
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import QApplication
import atk_query
import data_store as store
from atk_query import TargetQuery, MODEL
from battery_worker import BatteryWorker
from protocols import (
    BindingStore,
    DeviceRegistry,
    HidTransport,
    NanoFf05Codec,
    Nordic52Codec,
    Nordic54Codec,
    make_device_uid,
    resolve_binding,
)
from research import analyze, chart_break

APP = QApplication.instance() or QApplication([])
TEST_ROOT = Path(os.environ.get('ATK_TEST_TMP', str(Path(__file__).resolve().parents[1] / '.test-temp')))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_SETTINGS = tempfile.TemporaryDirectory(dir=TEST_ROOT)
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, TEST_SETTINGS.name)

def packet(percent=95, voltage=4272, link=0):
    p = [8, 4, 0, 0, 0, 2, percent, link, voltage >> 8, voltage & 255, 0, 0, 0, 0, 0, 0, 0]
    p[-1] = (0x55 - sum(p)) % 256
    return p

def collection(path=b'fake-a', **changes):
    d = dict(vendor_id=0x373B, product_id=0x11D9, interface_number=1,
             usage_page=0xFF02, usage=2, path=path, serial_number='')
    d.update(changes)
    return d

def event(q='test-1', mode='auto', success=True):
    at = datetime.now().astimezone()
    return dict(query_id=q, mode=mode, manual=mode=='manual', connected=success,
                outcome='success' if success else 'timeout', receiver=True,
                receiver_visibility='present', at=at, started_at=at, completed_at=at,
                percent=95 if success else None, packet_valid=success, percent_valid=success,
                voltage_mv=4272 if success else None, voltage_raw=4272 if success else None,
                voltage_validation='candidate_in_range', charging=None, charging_state='unknown',
                elapsed_ms=1.0, name=MODEL.display_name, device_id='fake', status_byte=2, link_byte=0)

class TransportTests(unittest.TestCase):
    def parse(self, response):
        return Nordic52Codec().parse_response(response)

    def test_valid_unknown_charge_and_voltage_semantics(self):
        for link in (0, 1, 255):
            status = self.parse(packet(link=link))
            self.assertTrue(status.packet_valid and status.percent_valid)
            self.assertEqual(status.percent,95)
            self.assertEqual(status.voltage_raw,4272)
            self.assertIsNone(status.charging)
            self.assertEqual(status.charging_state,'unknown')
            self.assertEqual(status.link_byte,link)

    def test_response_rejections(self):
        cases = []
        cases.append((packet()[:-1], 'invalid_length'))
        for offset, value, reason in ((0,9,'invalid_report_id'),(1,5,'invalid_command'),(16,0,'invalid_checksum')):
            p=packet();p[offset]=value;cases.append((p,reason))
        cases += [(packet(percent=101),'invalid_percent'),(None,'no_response')]
        for response, reason in cases:
            with self.subTest(reason=reason):
                status=self.parse(response)
                self.assertFalse(status.present)
                self.assertEqual(status.failure_reason,reason)

    def test_candidate_outlier_does_not_destroy_percent(self):
        status=self.parse(packet(voltage=65535))
        self.assertTrue(status.percent_valid)
        self.assertIsNone(status.voltage_mv)
        self.assertEqual(status.voltage_raw,65535)
        self.assertEqual(status.voltage_validation,'candidate_out_of_range')

    def fake_device(self, fail=None, response=None, short=False):
        class Device:
            closed=False
            reads=0
            writes=0
            def open_path(self,path):
                if fail=='open':raise OSError('injected open')
            def write(self,report):
                self.writes+=1
                if fail=='write':raise OSError('injected write')
                return 1 if short else len(report)
            def read(self,length,timeout_ms):
                self.reads+=1
                if fail=='read':raise OSError('injected read')
                if response is not None:return response
                time.sleep(timeout_ms/1000)
                return []
            def close(self):self.closed=True
        return Device()

    def test_transport_failures_close_handle(self):
        req=Nordic52Codec().build_request()
        for phase in ('open','write','read'):
            d=self.fake_device(fail=phase)
            tx=HidTransport(device_factory=lambda dev=d:dev)
            raw, err=tx.transact(b'fake', req, 17)
            self.assertIsNone(raw)
            self.assertEqual(err,phase+'_error')
            self.assertTrue(d.closed)

    def test_short_write_not_accepted(self):
        d=self.fake_device(short=True)
        tx=HidTransport(device_factory=lambda:d)
        raw, err=tx.transact(b'fake', Nordic52Codec().build_request(), 17)
        self.assertIsNone(raw)
        self.assertEqual(err,'short_write')
        self.assertEqual(d.reads,0)
        self.assertTrue(d.closed)

    def test_real_timeout_budget_with_simulated_no_response(self):
        d=self.fake_device()
        tx=HidTransport(device_factory=lambda:d)
        started=time.monotonic()
        raw, err=tx.transact(b'fake', Nordic52Codec().build_request(), 17)
        elapsed=time.monotonic()-started
        print(f'METRIC simulated_timeout_ms={elapsed*1000:.2f}')
        self.assertIsNone(raw)
        self.assertEqual(err,'timeout')
        self.assertGreaterEqual(elapsed,1.0)
        self.assertLess(elapsed,1.6)
        self.assertTrue(d.closed)

    def test_cancel_during_read_returns_promptly(self):
        d=self.fake_device()
        tx=HidTransport(device_factory=lambda:d)
        cancel=threading.Event()
        timer=threading.Timer(.16,cancel.set);timer.start()
        started=time.monotonic()
        raw, err=tx.transact(b'fake', Nordic52Codec().build_request(), 17, cancel_event=cancel)
        timer.join()
        elapsed=time.monotonic()-started
        print(f'METRIC simulated_cancel_ms={elapsed*1000:.2f}')
        self.assertIsNone(raw)
        self.assertEqual(err,'cancelled')
        self.assertLess(elapsed,.55)
        self.assertTrue(d.closed)

class QueryTests(unittest.TestCase):
    def query(self, enum, response=None):
        tmp=tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(tmp.cleanup)
        store_b=BindingStore(Path(tmp.name)/'bindings.json')
        class Dev:
            def open_path(self,path):pass
            def write(self,report):return len(report)
            def read(self,length,timeout_ms):return response or packet()
            def close(self):pass
        return TargetQuery(
            enum,
            lambda:Dev(),
            lambda *a:nullcontext(),
            binding_store=store_b,
            legacy_history_probe=lambda:False,
        )('q','manual',threading.Event())

    def test_absence_and_enumeration_error_are_distinct(self):
        self.assertEqual(self.query(lambda:[])['receiver_visibility'],'absent')
        def fail():raise OSError('injected enumeration')
        e=self.query(fail)
        self.assertEqual(e['outcome'],'enumeration_error')
        self.assertEqual(e['receiver_visibility'],'unknown')

    def test_interface_missing_and_ambiguous_never_query(self):
        tmp=tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(tmp.cleanup)
        store_b=BindingStore(Path(tmp.name)/'bindings.json')
        for devices,outcome in (([collection(interface_number=2)],'interface_not_found'),
                                ([collection(),collection(b'fake-b')],'ambiguous_interface')):
            factory=lambda:self.fail('must not send query')
            e=TargetQuery(lambda:devices,factory,lambda *a:nullcontext(),binding_store=store_b,legacy_history_probe=lambda:False)('q','auto',threading.Event())
            self.assertEqual(e['outcome'],outcome)
            self.assertTrue(e['receiver'])

    def test_unplug_or_reenumeration_drops_valid_old_response(self):
        for new,outcome in (([],'receiver_absent'),([collection(b'fake-b')],'device_changed')):
            enumerations=iter([[collection()],new])
            e=self.query(lambda:next(enumerations))
            self.assertEqual(e['outcome'],outcome)
            self.assertIsNone(e['percent'])

    def test_success_not_claimed_fresh_or_charging(self):
        e=self.query(lambda:[collection()])
        self.assertTrue(e['connected'])
        self.assertIsNone(e['charging'])
        self.assertEqual(e['charging_state'],'unknown')
        self.assertEqual(e['response_freshness'],'unverified')
        self.assertEqual(e['device_identity_basis'],'host_path_hash_not_stable')

class WorkerTests(unittest.TestCase):
    def launch(self, query, writer=None):
        results=[]
        w=BatteryWorker(query=query,writer=writer or (lambda e:None),event_writer=lambda e:None)
        w.result.connect(results.append,Qt.ConnectionType.DirectConnection)
        t=threading.Thread(target=w.run);t.start()
        self.addCleanup(lambda:(w.request_stop(),t.join(2)))
        return w,t,results

    def wait_for(self, condition, timeout=2):
        deadline=time.monotonic()+timeout
        while not condition() and time.monotonic()<deadline:time.sleep(.005)
        self.assertTrue(condition(),'condition timed out')

    def test_click_burst_during_auto_coalesces_into_one_followup(self):
        entered=threading.Event();release=threading.Event();calls=[];active=0;peak=0
        def query(q,mode,cancel):
            nonlocal active,peak
            active+=1;peak=max(peak,active);calls.append((q,mode));entered.set()
            if len(calls)==1:release.wait(1)
            active-=1
            return event(q,mode)
        saved=[];w,t,results=self.launch(query,saved.append)
        self.assertTrue(entered.wait(1))
        ids=[w.refresh() for _ in range(100)]
        self.assertEqual(len(set(ids)),1)
        release.set();self.wait_for(lambda:len(results)==2)
        self.assertEqual([m for _,m in calls],['auto','manual'])
        self.assertEqual(peak,1)
        self.assertEqual(len(saved),2)
        self.assertEqual(results[-1]['request_id'],ids[0])
        self.assertNotEqual(results[-1]['query_id'],ids[0])
        self.assertNotIn('last_success',results[-1]['last_success'])

    def test_resume_drops_late_result_and_replays_manual_token(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def query(q,mode,cancel):
            calls.append(q)
            if len(calls)==1:
                entered.set();release.wait(1)
            return event(q,mode)
        w=BatteryWorker(query=query,writer=lambda e:None,event_writer=lambda e:None)
        token=w.refresh();results=[]
        w.result.connect(results.append,Qt.ConnectionType.DirectConnection)
        t=threading.Thread(target=w.run);t.start()
        self.addCleanup(lambda:(w.request_stop(),t.join(2)))
        self.assertTrue(entered.wait(1));w.resume();release.set()
        self.wait_for(lambda:len(results)==1)
        self.assertEqual(len(calls),2)
        self.assertEqual(results[0]['request_id'],token)
        self.assertNotEqual(calls[0],calls[1])

    def test_stop_discards_inflight_success(self):
        entered=threading.Event()
        def query(q,mode,cancel):
            entered.set();cancel.wait(1);return event(q,mode)
        saved=[];w,t,results=self.launch(query,saved.append)
        self.assertTrue(entered.wait(1));started=time.monotonic();w.request_stop();t.join(1)
        print(f'METRIC worker_stop_ms={(time.monotonic()-started)*1000:.2f}')
        self.assertFalse(t.is_alive());self.assertFalse(results);self.assertFalse(saved)

    def test_storage_failure_keeps_success_distinct(self):
        def fail(e):raise OSError('disk unavailable')
        w,t,results=self.launch(lambda q,m,c:event(q,m),fail)
        self.wait_for(lambda:bool(results))
        self.assertTrue(results[0]['connected'])
        self.assertEqual(results[0]['outcome'],'success')
        self.assertIn('disk unavailable',results[0]['storage_error'])

    def test_failure_never_written_as_sample_and_backoff(self):
        saved=[];w,t,results=self.launch(lambda q,m,c:event(q,m,False),saved.append)
        self.wait_for(lambda:bool(results))
        self.assertFalse(saved)
        self.assertEqual([w.failure_delay(i,'timeout') for i in range(1,6)],[15,60,300,300,300])
        self.assertEqual(w.failure_delay(5,'receiver_absent'),30)

    def test_slow_storage_does_not_block_stop_request(self):
        entered=threading.Event();release=threading.Event()
        def writer(e):entered.set();release.wait(1)
        w,t,results=self.launch(lambda q,m,c:event(q,m),writer)
        self.assertTrue(entered.wait(1))
        started=time.monotonic();w.request_stop();elapsed=time.monotonic()-started
        release.set();t.join(1)
        self.assertLess(elapsed,.1);self.assertFalse(results)

    def test_failure_then_recovery_tracks_provenance(self):
        calls=[]
        def query(q,m,c):
            calls.append(q);return event(q,m,len(calls)>1)
        saved=[];w,t,results=self.launch(query,saved.append)
        self.wait_for(lambda:len(results)==1);w.refresh()
        self.wait_for(lambda:len(results)==2)
        self.assertTrue(results[1]['recovered'])
        self.assertEqual(results[1]['failures_before'],1)
        self.assertEqual(len(saved),1)

    def test_diagnostic_failure_is_separate_from_sample_storage(self):
        results=[];saved=[]
        def fail(e):raise PermissionError('diagnostic denied')
        w=BatteryWorker(query=lambda q,m,c:event(q,m),writer=saved.append,event_writer=fail)
        w.result.connect(results.append,Qt.ConnectionType.DirectConnection)
        t=threading.Thread(target=w.run);t.start()
        self.addCleanup(lambda:(w.request_stop(),t.join(2)))
        self.wait_for(lambda:bool(results))
        self.assertEqual(len(saved),1)
        self.assertIn('diagnostic denied',results[0]['diagnostic_error'])
        self.assertIsNone(results[0]['storage_error'])

    def test_native_qthread_queued_delivery(self):
        results=[]
        w=BatteryWorker(query=lambda q,m,c:event(q,m),writer=lambda e:None,event_writer=lambda e:None)
        w.result.connect(results.append)
        w.start()
        try:
            deadline=time.monotonic()+1
            while not results and time.monotonic()<deadline:
                APP.processEvents();time.sleep(.005)
            self.assertEqual(len(results),1)
        finally:
            self.assertTrue(w.finish())

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.patches=[]
        for name,path in dict(APP_DIR=root,HISTORY_PATH=root/'v02.csv',V01_HISTORY=root/'v01.csv',
                              SAMPLES_PATH=root/'battery_samples.v1.csv',
                              SAMPLES_V2_PATH=root/'battery_samples.v2.csv',
                              EVENTS_PATH=root/'events.jsonl').items():
            p=patch.object(store,name,path);p.start();self.addCleanup(p.stop)

    def legacy(self,path,naive=False):
        at=datetime.now().astimezone()
        if naive:at=at.replace(tzinfo=None)
        path.write_text('timestamp_local,model,percent_reported,voltage_mv,charging\n'+f'{at.isoformat()},target,85,4272,0\n',encoding='utf-8-sig')

    def test_new_schema_and_legacy_unchanged(self):
        self.legacy(store.V01_HISTORY,True);self.legacy(store.HISTORY_PATH)
        before={p:p.read_bytes() for p in (store.V01_HISTORY,store.HISTORY_PATH)}
        e=event();store.write_sample(e);store.write_sample(event('test-2','manual'))
        rows=store.read_samples(limit=None)
        self.assertEqual(len(rows),4)
        self.assertTrue(all(r['charging'] is None for r in rows))
        self.assertTrue(any(r['timezone_status'].startswith('unknown') for r in rows))
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.assertEqual(len({r['query_id'] for r in rows if r['query_id']}),2)

    def test_refuse_unknown_schema_and_truncated_tail_unchanged(self):
        for raw in ('wrong,header\n1,2\n',','.join(store.HEADER_V2)+'\n1,incomplete'):
            store.SAMPLES_V2_PATH.write_text(raw,encoding='utf-8')
            before=store.SAMPLES_V2_PATH.read_bytes()
            with self.assertRaises(ValueError):store.write_sample(event())
            self.assertEqual(store.SAMPLES_V2_PATH.read_bytes(),before)

    def test_bad_row_and_outlier_preserve_raw(self):
        e=event();e.update(voltage_raw=65535,voltage_mv=None,voltage_validation='candidate_out_of_range')
        store.write_sample(e)
        with store.SAMPLES_V2_PATH.open('a',encoding='utf-8') as f:f.write('1,incomplete\n')
        rows=store.read_samples()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['voltage_raw'],65535)
        self.assertIsNone(rows[0]['mv']);self.assertTrue(store.READ_WARNINGS)

    def test_failed_sample_refused(self):
        with self.assertRaises(ValueError):store.write_sample(event(success=False))
        self.assertFalse(store.SAMPLES_V2_PATH.exists())

    def test_query_diagnostic_has_ids_and_unknown_charge(self):
        e=event();store.write_query_event(e)
        r=json.loads(store.EVENTS_PATH.read_text(encoding='utf-8'))
        self.assertEqual(r['query_id'],'test-1');self.assertIsNone(r['charging'])
        self.assertNotIn('last_success',r)

    def test_diagnostic_rotation(self):
        e=event();e['error']='x'*550000
        store.write_query_event(e);store.write_query_event(e)
        self.assertTrue(Path(str(store.EVENTS_PATH)+'.1').is_file())
        for p in store.APP_DIR.glob('events.jsonl*'):
            self.assertLess(p.stat().st_size,1_000_000)

class ResearchAndUiTests(unittest.TestCase):
    def test_real_windows_single_instance_mutex(self):
        from dashboard import singleton
        name='Local\\ATK_Battery_test_'+uuid.uuid4().hex
        lock=singleton(name)
        self.assertIsNotNone(lock)
        try:self.assertIsNone(singleton(name))
        finally:lock[0].CloseHandle(lock[1])

    def test_real_device_mutex_excludes_other_thread(self):
        from atk_query import device_guard
        outcomes=[]
        def contender():
            try:
                with device_guard():outcomes.append('acquired')
            except BlockingIOError:outcomes.append('busy')
        with device_guard():
            t=threading.Thread(target=contender);t.start();t.join(1)
            self.assertEqual(outcomes,['busy'])

    def test_flags_do_not_mutate_readings_and_unknown_charge_supported(self):
        at=datetime.now().astimezone()
        rows=[dict(ts=at,percent=85,mv=4272,source='V0.1'),
              dict(ts=at+timedelta(minutes=5),percent=95,mv=4272,source='V0.1')]
        before=repr(rows);flags=analyze(rows)
        self.assertIn('unknown',flags[0]['message']);self.assertEqual(repr(rows),before)
        self.assertFalse(chart_break(rows[0],rows[1]))
        rows[1]['ts']=at+timedelta(minutes=20)
        self.assertTrue(chart_break(rows[0],rows[1]))

    def test_drop_recovery_plateau_and_device_boundaries(self):
        at=datetime.now().astimezone()
        a=dict(ts=at,percent=95,mv=4272,source='V0.2.1',device_id='a')
        b=dict(ts=at+timedelta(minutes=5),percent=80,mv=4267,source='V0.2.1',device_id='a',recovered=True)
        # V0.3: recovered=True within <=450s does not break the chart line.
        self.assertFalse(chart_break(a,b))
        labels=';'.join(analyze([a,b])[0]['labels'])
        self.assertIn('短时',labels);self.assertIn('恢复',labels)
        b_gap=dict(b,ts=at+timedelta(minutes=10))
        self.assertTrue(chart_break(a,b_gap))
        b_dev=dict(b,device_id='b')
        self.assertFalse(analyze([a,b_dev]));self.assertTrue(chart_break(a,b_dev))
        # Plateau only fires within a continuous segment (dt<=450s steps), never across a 35-min gap.
        gap_only=[a,dict(ts=at+timedelta(minutes=35),percent=95,mv=4200,source='V0.2.1',device_id='a')]
        self.assertEqual(analyze(gap_only),[])
        cont=[dict(ts=at+timedelta(minutes=5*i),percent=95,mv=4272-4*i,source='V0.2.1',device_id='a') for i in range(8)]
        self.assertIn('平台期',analyze(cont)[0]['labels'][0])

    def test_read_samples_over_2000_rows_and_time_filter(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root=Path(tmp)
            with patch.object(store,'APP_DIR',root), patch.object(store,'HISTORY_PATH',root/'v02.csv'), patch.object(store,'V01_HISTORY',root/'v01.csv'), patch.object(store,'SAMPLES_PATH',root/'battery_samples.v1.csv'):
                now=datetime.now().astimezone()
                with store.SAMPLES_PATH.open('w',encoding='utf-8-sig',newline='') as f:
                    w=csv.writer(f);w.writerow(store.HEADER)
                    for i in range(2500):
                        # Spread 2500 rows across 40 days
                        ts=(now-timedelta(minutes=23*i)).isoformat(timespec='milliseconds')
                        w.writerow([1,f'q-{i}','',ts,ts,ts,'ATK F1 V2 Ultimate (8K)','dev-1','receiver_serial_hash',80,1,1,4200,4200,'candidate_in_range','unknown',2,0,'auto',0,0,140.0,'unverified','0.3'])
                all_rows=store.read_samples()
                self.assertEqual(len(all_rows),2500)
                rows_30d=store.read_samples(hours=30*24)
                rows_7d=store.read_samples(hours=7*24)
                self.assertGreater(len(rows_30d),len(rows_7d))
                self.assertLess(len(rows_30d),len(all_rows))

    def test_research_v03_thresholds_and_cross_gap_distinction(self):
        at=datetime.now().astimezone()
        # 5% single-step fluctuation within continuous segment should not trigger rise/comparable/recovered alerts.
        r0=dict(ts=at,percent=75,mv=4150,source='V0.2.1',device_id='a',recovered=False)
        r1=dict(ts=at+timedelta(minutes=5),percent=80,mv=4150,source='V0.2.1',device_id='a',recovered=True)
        self.assertEqual(analyze([r0,r1]),[])
        # Cross-gap +10% or -5% should not alert, while cross-gap >=15% flags major rise.
        g_small=dict(ts=at+timedelta(minutes=20),percent=85,mv=4155,source='V0.2.1',device_id='a',recovered=True)
        self.assertEqual(analyze([r0,g_small]),[])
        g_big=dict(ts=at+timedelta(minutes=20),percent=90,mv=4250,source='V0.2.1',device_id='a',recovered=True)
        fl=analyze([r0,g_big])
        self.assertEqual(len(fl),1)
        self.assertIn('断线后报告电量显著回升',fl[0]['labels'][0])
        # comparable_mv=20 boundary
        c_hit=dict(ts=at+timedelta(minutes=5),percent=65,mv=4130,source='V0.2.1',device_id='a',recovered=False)
        self.assertIn('相近候选电压对应不同百分比',analyze([r0,c_hit])[0]['labels'])
        c_miss=dict(ts=at+timedelta(minutes=5),percent=65,mv=4129,source='V0.2.1',device_id='a',recovered=False)
        self.assertNotIn('相近候选电压对应不同百分比',analyze([r0,c_miss])[0]['labels'])

    def test_wm_devicechange_4s_debounce_and_power_1s_debounce(self):
        from dashboard import Dashboard
        class DummyWorker:
            def __init__(self): self.resumes=0
            def resume(self): self.resumes+=1
        with patch('dashboard.read_samples',return_value=[]):
            window=Dashboard(start_worker=False)
            self.addCleanup(lambda:(window.tray.hide(),window.deleteLater()))
            window.worker=DummyWorker()
            self.assertTrue(window.handle_native_event(0x0219,7,now=10.0))
            self.assertFalse(window.handle_native_event(0x0219,7,now=11.5))
            self.assertFalse(window.handle_native_event(0x0219,7,now=13.9))
            self.assertTrue(window.handle_native_event(0x0219,7,now=14.1))
            self.assertEqual(window.worker.resumes,2)
            # Power resume keeps 1s debounce independent of device change burst
            self.assertTrue(window.handle_native_event(0x0218,7,now=14.5))
            self.assertFalse(window.handle_native_event(0x0218,7,now=15.0))
            self.assertTrue(window.handle_native_event(0x0218,7,now=15.6))
            self.assertEqual(window.worker.resumes,4)

    def test_history_period_defaults_and_chart_render_large_dataset(self):
        from dashboard import Dashboard
        from PySide6.QtCore import QPointF, Qt as QtCoreQt
        from PySide6.QtGui import QMouseEvent
        with patch('dashboard.read_samples',return_value=[]):
            window=Dashboard(start_worker=False)
            self.addCleanup(lambda:(window.tray.hide(),window.deleteLater()))
            labels=[window.history_period.itemText(i) for i in range(window.history_period.count())]
            self.assertIn('最近 7 天',labels)
            self.assertIn('最近 30 天',labels)
            self.assertIn('全部记录',labels)
            self.assertEqual(window.history_period.currentText(),'最近 30 天')
            self.assertEqual(window.history_period.currentData(),30*24)
            # Render 5,000 rows in TrendChart and verify hover tooltip lookup does not crash
            now=datetime.now().astimezone()
            big_rows=[dict(ts=now-timedelta(minutes=5*(5000-i)),percent=50+(i%50),mv=3900+(i%300),source='V0.2.1',recovered=(i%100==0),query_mode='auto') for i in range(5000)]
            window.history_chart.resize(800,260)
            window.history_chart.set_data(big_rows,'percent')
            pm=window.history_chart.grab()
            self.assertFalse(pm.isNull())
            self.assertTrue(window.history_chart._plot_points)
            pt=window.history_chart._plot_points[0][0]
            ev=QMouseEvent(QMouseEvent.Type.MouseMove,pt,pt,QtCoreQt.MouseButton.NoButton,QtCoreQt.MouseButton.NoButton,QtCoreQt.KeyboardModifier.NoModifier)
            window.history_chart.mouseMoveEvent(ev)

    def test_export_cannot_overwrite_original_history(self):
        import dashboard
        with patch('dashboard.read_samples',return_value=[]):
            window=dashboard.Dashboard(start_worker=False)
            try:
                with patch('dashboard.read_samples',return_value=[{'percent':95}]), patch.object(dashboard.QFileDialog,'getSaveFileName',return_value=(str(dashboard.SAMPLES_PATH),'')), patch.object(dashboard.QMessageBox,'warning') as warning:
                    window.export_csv();warning.assert_called_once()
            finally:window.tray.hide();window.deleteLater()

    def test_ui_history_age_and_storage_failure_feedback(self):
        from dashboard import Dashboard
        with patch('dashboard.read_samples',return_value=[]):
            window=Dashboard(start_worker=False)
            self.addCleanup(lambda:(window.tray.hide(),window.deleteLater()))
            e=event();e['last_success']={k:e[k] for k in ('at','percent','voltage_mv')}
            e.update(storage_error='disk unavailable',consecutive_failures=0)
            window.on_result(e)
            self.assertIn('存储失败',window.storage_status.text())
            failed=event('test-2',success=False);failed['last_success']=e['last_success']
            window.on_result(failed)
            self.assertEqual(window.main_pct.text(),'92%')
            self.assertIn('95%',window.main_pct_sub.text())
            self.assertIn('4272',window.main_voltage_sub.text())
            self.assertIn('电压估算 92%',window.tray.toolTip())
            self.assertIn('固件原报 95%',window.tray.toolTip())
            self.assertIn('历史读数',window.main_hint.text())
            self.assertIn('距今',window.main_hint.text())
            self.assertIn('未收到回复',window.refresh_status.text())
            window.pending_query_id='request-A';window.manual_pending=True
            failed.update(manual=True,query_id='query-X',request_id='request-A')
            window.on_result(failed)
            self.assertFalse(window.manual_pending)
            self.assertTrue(window.refresh_button.isEnabled())

class VoltageSocTests(unittest.TestCase):
    def test_lut_boundaries_and_interpolation(self):
        from voltage_soc import lut_mv_to_soc, LOW_EXTRAPOLATION_MV
        self.assertEqual(lut_mv_to_soc(4400),100.0)
        self.assertEqual(lut_mv_to_soc(4340),100.0)
        self.assertEqual(lut_mv_to_soc(3500),0.0)
        self.assertEqual(lut_mv_to_soc(3300),0.0)
        self.assertAlmostEqual(lut_mv_to_soc(4320),97.5,places=3)
        self.assertAlmostEqual(lut_mv_to_soc(3722),12.0,places=3)
        self.assertTrue(3700 < LOW_EXTRAPOLATION_MV)

    def test_sag_gate_spike_rejection_and_two_frame_confirmation(self):
        from voltage_soc import VoltageSocEstimator
        est=VoltageSocEstimator()
        r0=est.step(3981,link_byte=0)
        self.assertEqual(r0.percent_est,58)
        self.assertFalse(r0.sag_pending)
        # Single-frame -104 mV sag -> 0% leakage, sag_pending=True
        r_sag=est.step(3877,link_byte=0)
        self.assertEqual(r_sag.percent_est,58)
        self.assertEqual(r_sag.v_est_mv,3981.0)
        self.assertTrue(r_sag.sag_pending)
        # Next frame rebounds -> spike discarded
        r_reb=est.step(3981,link_byte=0)
        self.assertEqual(r_reb.percent_est,58)
        self.assertFalse(r_reb.sag_pending)
        # Two consecutive low frames (-68 mV offline drop) -> confirmed on frame 2
        est2=VoltageSocEstimator()
        self.assertEqual(est2.step(4167,link_byte=0).percent_est,80)
        f1=est2.step(4099,link_byte=0)
        self.assertEqual(f1.percent_est,80)
        self.assertTrue(f1.sag_pending)
        f2=est2.step(4099,link_byte=0)
        self.assertEqual(f2.percent_est,72)
        self.assertFalse(f2.sag_pending)

    def test_monotone_hold_fast_unlock_and_extrapolated_low(self):
        from voltage_soc import VoltageSocEstimator
        est=VoltageSocEstimator()
        self.assertEqual(est.step(3940,link_byte=0).percent_est,54)
        # 1-3 LSB positive noise (+5 mV / +9 mV) should not bounce upward
        self.assertEqual(est.step(3949,link_byte=0).v_est_mv,3940.0)
        # Sustained 2-frame >= +15 mV unlock
        u1=est.step(3956,link_byte=0)
        self.assertEqual(u1.v_est_mv,3940.0)
        u2=est.step(3960,link_byte=0)
        self.assertEqual(u2.v_est_mv,3960.0)
        # Single-frame >= +25 mV fast unlock
        u3=est.step(3990,link_byte=0)
        self.assertEqual(u3.v_est_mv,3990.0)
        # Charging link_byte=1 unlocks immediately even on small steps
        est.step(3990,link_byte=0)
        c1=est.step(3995,link_byte=1)
        self.assertEqual(c1.v_est_mv,3995.0)
        # Low-voltage extrapolation flag (< 3722 mV)
        low_est=VoltageSocEstimator()
        low_res=low_est.step(3690,link_byte=0)
        self.assertTrue(low_res.extrapolated_low)
        self.assertLess(low_res.percent_est,12)

    def test_missing_voltage_fallback_and_restart_replay_consistency(self):
        from voltage_soc import VoltageSocEstimator, estimate_series
        now=datetime.now().astimezone()
        rows=[
            dict(ts=now,percent=85,mv=None,source='V0.1',link_byte=None),
            dict(ts=now+timedelta(minutes=5),percent=80,mv=4167,source='V0.2.1',link_byte=0),
            dict(ts=now+timedelta(minutes=10),percent=80,mv=None,source='V0.2.1',link_byte=0),
            dict(ts=now+timedelta(minutes=15),percent=75,mv=4158,source='V0.2.1',link_byte=0),
            dict(ts=now+timedelta(minutes=20),percent=75,mv=4163,source='V0.2.1',link_byte=0),
        ]
        full_enriched, _ = estimate_series(rows)
        self.assertTrue(full_enriched[0]['fallback_used'])
        self.assertIsNone(full_enriched[0]['percent_est'])
        self.assertEqual(full_enriched[0]['display_percent'],85)
        self.assertEqual(full_enriched[1]['percent_est'],80)
        self.assertTrue(full_enriched[2]['fallback_used'])
        self.assertEqual(full_enriched[2]['display_percent'],80)
        # Split replay rows[:3] then step rows[3:] must match full replay identically
        part_enriched, part_est = estimate_series(rows[:3])
        tail_res=[part_est.step(r['mv'],link_byte=r['link_byte']) for r in rows[3:]]
        self.assertEqual([r['percent_est'] for r in full_enriched[3:]],[x.percent_est for x in tail_res])
        self.assertEqual([r['v_est_mv'] for r in full_enriched[3:]],[x.v_est_mv for x in tail_res])

    def test_v04_ui_fallback_overlay_table_and_dynamic_icon(self):
        from dashboard import Dashboard, make_icon
        now=datetime.now().astimezone()
        sample_rows=[
            dict(ts=now-timedelta(minutes=5),percent=80,mv=None,source='V0.1',recovered=False,query_mode='auto',query_id='q1',link_byte=None),
            dict(ts=now,percent=75,mv=4140,source='V0.2.1',recovered=True,query_mode='auto',query_id='q2',link_byte=0),
        ]
        with patch('dashboard.read_samples',return_value=sample_rows):
            window=Dashboard(start_worker=False)
            self.addCleanup(lambda:(window.tray.hide(),window.deleteLater()))
            self.assertEqual(window.table.columnCount(),5)
            self.assertEqual(window.table.horizontalHeaderItem(1).text(),'电压估算')
            self.assertEqual(window.table.horizontalHeaderItem(2).text(),'固件原报')
            self.assertTrue(window.show_fw_overlay.isChecked())
            window.show_fw_overlay.setChecked(False)
            APP.processEvents()
            self.assertFalse(window.history_chart.show_firmware)
        # Missing voltage event falls back cleanly to firmware percent
        with patch('dashboard.read_samples',return_value=[]):
            w2=Dashboard(start_worker=False)
            self.addCleanup(lambda:(w2.tray.hide(),w2.deleteLater()))
            ev=event();ev['voltage_mv']=None;ev['percent']=65
            ev['last_success']=dict(at=now,percent=65,voltage_mv=None)
            w2.on_result(ev)
            self.assertEqual(w2.main_pct.text(),'65%')
            self.assertIn('回退',w2.main_pct_title.text())
        self.assertFalse(make_icon(58).isNull())
        self.assertFalse(make_icon(15,stale=True).isNull())

class TaskbarBadgeTests(unittest.TestCase):
    def test_badge_style_themes_low_stale_and_nodata(self):
        from taskbar_badge import resolve_badge_style
        s_light=resolve_badge_style(45,stale=False,light_theme=True,hovered=False)
        self.assertEqual(s_light.text,'45%')
        self.assertEqual(s_light.fg_color.alpha(),255)
        self.assertEqual(s_light.outline_color.name().lower(),'#1e1e24')
        self.assertEqual(s_light.fill_color.name().lower(),'#7c64b8')
        self.assertEqual(s_light.wheel_color,s_light.outline_color)
        self.assertEqual(s_light.bg_color.alpha(),1)

        s_full_light=resolve_badge_style(100,stale=False,light_theme=True,hovered=False)
        self.assertEqual(s_full_light.wheel_color.name().lower(),'#ffffff')

        s_dark=resolve_badge_style(45,stale=False,light_theme=False,hovered=True)
        self.assertEqual(s_dark.fg_color.alpha(),255)
        self.assertEqual(s_dark.outline_color.name().lower(),'#f3f4f6')
        self.assertEqual(s_dark.fill_color.name().lower(),'#a68ee6')
        self.assertGreater(s_dark.bg_color.alpha(),1)

        s_full_dark=resolve_badge_style(100,stale=False,light_theme=False,hovered=False)
        self.assertEqual(s_full_dark.wheel_color.name().lower(),'#1e1e24')

        s_low=resolve_badge_style(16,stale=False,light_theme=True,hovered=False)
        self.assertTrue(s_low.low_battery)
        self.assertEqual(s_low.fill_color.name().lower(),'#d66853')
        self.assertEqual(s_low.fg_color.name().lower(),'#c45545')

        s_custom_thresh=resolve_badge_style(28,stale=False,light_theme=False,hovered=False,low_threshold=30)
        self.assertTrue(s_custom_thresh.low_battery)
        self.assertEqual(s_custom_thresh.fg_color.name().lower(),'#f28b76')

        s_stale=resolve_badge_style(45,stale=True,light_theme=True,hovered=False)
        self.assertTrue(s_stale.stale)
        self.assertEqual(s_stale.fg_color.name().lower(),'#867e96')
        self.assertTrue(150 <= s_stale.fg_color.alpha() <= 175)

        s_none=resolve_badge_style(None,stale=False,light_theme=False,hovered=False)
        self.assertEqual(s_none.text,'--%')
        self.assertTrue(s_none.stale)
        self.assertEqual(s_none.fg_color.name().lower(),'#9a93a8')
        self.assertTrue(150 <= s_none.fg_color.alpha() <= 175)

    def test_render_badge_image_125_dpi_and_nodata(self):
        from PySide6.QtGui import QImage
        from taskbar_badge import render_badge_image
        cases = (
            (45, False, True, False, 120),
            (100, False, True, False, 120),
            (16, False, True, False, 120),
            (45, True, True, False, 120),
            (None, True, True, False, 96),
            (45, False, False, False, 120),
            (100, False, False, True, 120),
            (16, False, False, False, 120),
            (45, True, False, False, 120),
            (None, True, False, False, 96),
        )
        for pct, stale, light, hovered, dpi in cases:
            img=render_badge_image(pct,stale=stale,light_theme=light,hovered=hovered,dpi=dpi)
            self.assertEqual(img.format(),QImage.Format.Format_ARGB32_Premultiplied)
            self.assertGreater(img.width(),44)
            self.assertGreater(img.height(),24)
            # Hit-pad invariant: full canvas corners + center MUST retain alpha >= 1 even when stale
            for cx, cy in (
                (0, 0),
                (img.width() - 1, 0),
                (0, img.height() - 1),
                (img.width() - 1, img.height() - 1),
                (img.width() // 2, img.height() // 2),
            ):
                self.assertGreaterEqual(img.pixelColor(cx, cy).alpha(), 1, f'Transparent corner ({cx},{cy}) for {(pct,stale,light)}')
            # Foreground icon/text/liquid-fill must contain visible pixels
            has_fg=any(
                img.pixelColor(x,y).alpha() >= 100
                for x in range(0,img.width(),2)
                for y in range(0,img.height(),2)
            )
            self.assertTrue(has_fg)

    def test_compute_badge_rect_win11_26h2_and_overlap_guard(self):
        from taskbar_badge import compute_badge_rect
        tray=(0,1380,2560,1440)
        notify=(2200,1380,2560,1440)
        rebar=(1143,1380,1308,1440)
        r=compute_badge_rect(tray,notify,rebar,88,44,dpi=120)
        self.assertIsNotNone(r)
        x,y,w,h=r
        self.assertEqual((w,h),(88,44))
        self.assertEqual(y,1388)  # vertically centered in 60px bar
        self.assertEqual(x+w,2200-6)  # 6px gap at 125% DPI left of TrayNotifyWnd
        # Overlap guard: rebar right very close to notify left -> returns None
        crowded=compute_badge_rect(tray,notify,(1143,1380,2150,1440),88,44,dpi=120)
        self.assertIsNone(crowded)
        # Auto-hidden taskbar (height 2px) -> returns None
        self.assertIsNone(compute_badge_rect((0,1438,2560,1440),notify,rebar,88,44,dpi=120))

    def test_fullscreen_detection_excludes_desktop_and_taskbar(self):
        from taskbar_badge import should_hide_for_fullscreen
        mon=(0,0,2560,1440)
        self.assertFalse(should_hide_for_fullscreen((0,0,2560,1440),'Progman',mon,1))
        self.assertFalse(should_hide_for_fullscreen((0,0,2560,1440),'Shell_TrayWnd',mon,1))
        self.assertTrue(should_hide_for_fullscreen((0,0,2560,1440),'Chrome_WidgetWin_1',mon,1))
        self.assertTrue(should_hide_for_fullscreen((100,100,800,600),'SomeGame',mon,3))

    def test_badge_mount_failure_graceful_degradation_and_setting_persistence(self):
        from dashboard import Dashboard
        from taskbar_badge import TaskbarBadge
        b=TaskbarBadge(enabled=True)
        with patch.object(b,'_create_window',side_effect=OSError('mock create failed')):
            ok=b.start()
        self.assertFalse(ok)
        self.assertTrue(b.degraded)
        # Dashboard stays healthy if TaskbarBadge raises during startup, and setting persists
        with patch('dashboard.read_samples',return_value=[]), patch('dashboard.TaskbarBadge.start',return_value=False):
            w=Dashboard(start_worker=False)
            self.addCleanup(lambda:(w.tray.hide(),w._cleanup_badge(),w.deleteLater()))
            self.assertTrue(w.badge_action.isChecked())
            w.badge_action.setChecked(False)
            APP.processEvents()
            self.assertEqual(str(w.settings.value('taskbar_badge')).lower(),'false')
            w.badge_action.setChecked(True)
            APP.processEvents()
            self.assertEqual(str(w.settings.value('taskbar_badge')).lower(),'true')

    def test_win32_layered_window_present_and_sync(self):
        from taskbar_badge import TaskbarBadge
        b=TaskbarBadge(enabled=True)
        self.addCleanup(b.destroy)
        self.assertTrue(b.start())
        mock_geo={
            'tray_hwnd': 0,
            'tray_rect': (0,1380,2560,1440),
            'notify_rect': (2200,1380,2560,1440),
            'rebar_rect': (1143,1380,1308,1440),
            'dpi': 120,
            'mon_rect': (0,0,2560,1440),
            'fg_rect': (0,0,1000,800),
            'fg_class': 'Notepad',
            'quns_state': 1,
        }
        with patch.object(b,'_probe_taskbar_geometry',return_value=mock_geo):
            b.update_state(48,stale=False,tooltip_text='48%')
            self.assertFalse(b.degraded)
            self.assertTrue(b._visible)
            self.assertIsNotNone(b._last_rect)
        b.destroy()
        self.assertIsNone(b._hwnd)


class IrisGreenhouseUiTests(unittest.TestCase):
    def test_topbar_navigation_window_size_and_badges(self):
        from PySide6.QtCore import Qt
        from dashboard import Dashboard
        with patch('dashboard.read_samples',return_value=[]):
            w=Dashboard(start_worker=False)
            self.addCleanup(lambda:(w.tray.hide(),w._cleanup_badge(),w.deleteLater()))
            self.assertEqual((w.width(),w.height()),(1080,700))
            self.assertEqual((w.minimumWidth(),w.minimumHeight()),(1080,700))
            self.assertEqual((w.maximumWidth(),w.maximumHeight()),(1080,700))
            self.assertFalse(bool(w.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint))
            self.assertFalse(hasattr(w,'sidebar'))
            self.assertFalse(hasattr(w,'theme_box'))
            self.assertEqual(len(w.nav_buttons),3)
            self.assertEqual(w.topbar_inner.maximumWidth(),1240)
            self.assertEqual(w.pages.maximumWidth(),1240)
            self.assertEqual(w.statusbar_inner.maximumWidth(),1240)
            self.assertNotIn('373B:11D9',w.device_selector.currentText())
            for idx in (1,2,0):
                w.nav_buttons[idx].click()
                APP.processEvents()
                self.assertEqual(w.pages.currentIndex(),idx)
                self.assertTrue(w.nav_buttons[idx].property('active'))
            w.show()
            APP.processEvents()
            self.assertEqual(w.topbar_inner.x(),w.pages.x())
            self.assertEqual(w.pages.x(),w.statusbar_inner.x())
            self.assertEqual(w.topbar_inner.width(),w.pages.width())
            self.assertEqual(w.pages.width(),w.statusbar_inner.width())
            self.assertEqual(len(w.core_subcards),4)
            self.assertEqual(w.core_left_card.width(),340)
            self.assertGreaterEqual(w.core_right_card.width(),620)
            self.assertEqual(w.home_chart_note.text(),'持续记录中')
            # Low voltage (< 3722 mV, 10%) keeps top-right pill metric-only while slot pill marks low
            now=datetime.now().astimezone()
            ev=event();ev['voltage_mv']=3680;ev['percent']=10
            ev['last_success']=dict(at=now,percent=10,voltage_mv=3680)
            w.on_result(ev)
            self.assertNotIn('373B:11D9',w.device_selector.currentText())
            self.assertEqual(w.badge_est_type.text(),'● 电压估算')
            self.assertTrue(w.badge_low_extrap.isHidden())
            self.assertEqual(w.ring_widget.sub_text,'低段外推')
            self.assertEqual(w.slot_state_pill.text(),'低电')
            self.assertIn('V0.8.0',w.status_right.text())
            self.assertIn('永久免费开源',w.status_right.text())

    def test_trend_chart_area_fill_and_isolated_points(self):
        from PySide6.QtCore import QPointF
        from dashboard import TrendChart
        from ui_theme import should_fill_chart_area
        wide_seg=[QPointF(50.0,60.0),QPointF(220.0,80.0)]
        narrow_seg=[QPointF(50.0,60.0),QPointF(56.0,64.0)]
        self.assertTrue(should_fill_chart_area([wide_seg],24))
        self.assertFalse(should_fill_chart_area([wide_seg],7*24))
        self.assertFalse(should_fill_chart_area([wide_seg],30*24))
        self.assertFalse(should_fill_chart_area([wide_seg],None))
        self.assertFalse(should_fill_chart_area([wide_seg]*13,24))
        self.assertFalse(should_fill_chart_area([narrow_seg],24))

        now=datetime.now().astimezone()
        # 30d data with isolated single point + multiple segments -> area fill disabled
        rows_30d=[
            dict(ts=now-timedelta(days=10),percent=80,mv=4167,display_percent=80,source='V0.2.1',recovered=False,query_mode='auto'),
            dict(ts=now-timedelta(days=5),percent=70,mv=4070,display_percent=70,source='V0.2.1',recovered=False,query_mode='auto'),
            dict(ts=now-timedelta(days=5)-timedelta(minutes=-5),percent=70,mv=4068,display_percent=70,source='V0.2.1',recovered=False,query_mode='auto'),
            dict(ts=now,percent=60,mv=3990,display_percent=60,source='V0.2.1',recovered=True,query_mode='auto'),
        ]
        chart=TrendChart()
        chart.resize(800,240)
        chart.set_data(rows_30d,'percent',show_firmware=True,auto_scale=False,period_hours=30*24)
        pm30=chart.grab()
        self.assertFalse(pm30.isNull())
        self.assertFalse(chart.last_area_filled)

        # 24h continuous wide segment -> area fill enabled
        rows_24h=[
            dict(ts=now-timedelta(hours=20)+timedelta(minutes=5*i),percent=60-(i%5),mv=3990-2*i,display_percent=60-(i%5),source='V0.2.1',recovered=False,query_mode='auto')
            for i in range(15)
        ]
        chart.set_data(rows_24h,'percent',show_firmware=True,auto_scale=True,period_hours=24)
        pm24=chart.grab()
        self.assertFalse(pm24.isNull())
        self.assertTrue(chart.last_area_filled)

    def test_y_axis_auto_scale_bounds_and_period_defaults(self):
        from dashboard import Dashboard
        from ui_theme import compute_y_axis_bounds, default_auto_scale_for_hours
        self.assertTrue(default_auto_scale_for_hours(1))
        self.assertTrue(default_auto_scale_for_hours(24))
        self.assertFalse(default_auto_scale_for_hours(7*24))
        self.assertFalse(default_auto_scale_for_hours(30*24))
        self.assertFalse(default_auto_scale_for_hours(None))

        low,high=compute_y_axis_bounds([48.0,52.0,56.0],'percent',auto_scale=True)
        self.assertGreater(low,35.0)
        self.assertLess(high,70.0)
        self.assertEqual((high-low)%4,0)
        self.assertEqual(compute_y_axis_bounds([48.0,56.0],'percent',auto_scale=False),(0.0,100.0))

        v_low,v_high=compute_y_axis_bounds([3.88,3.92],'mv',auto_scale=True)
        self.assertGreater(v_low,3.6)
        self.assertLess(v_high,4.2)

        with patch('dashboard.read_samples',return_value=[]):
            w=Dashboard(start_worker=False)
            self.addCleanup(lambda:(w.tray.hide(),w._cleanup_badge(),w.deleteLater()))
            # Home default is 24h -> auto_scale True
            self.assertTrue(w.home_auto_scale.isChecked())
            # History default is 30d -> auto_scale False
            self.assertFalse(w.history_auto_scale.isChecked())
            # Switching history to 24h enables auto_scale automatically
            w.history_period.setCurrentIndex(1)
            APP.processEvents()
            self.assertTrue(w.history_auto_scale.isChecked())
            # Switching history to 7d disables auto_scale automatically
            w.history_period.setCurrentIndex(2)
            APP.processEvents()
            self.assertFalse(w.history_auto_scale.isChecked())

    def test_toggle_switch_state_and_taskbar_badge_sync(self):
        from dashboard import Dashboard
        from ui_theme import ToggleSwitch
        sw=ToggleSwitch('测试开关')
        sw.resize(sw.sizeHint())
        self.assertFalse(sw.isChecked())
        pm_off=sw.grab()
        self.assertFalse(pm_off.isNull())
        sw.click()
        self.assertTrue(sw.isChecked())
        pm_on=sw.grab()
        self.assertFalse(pm_on.isNull())

        with patch('dashboard.read_samples',return_value=[]), patch('dashboard.TaskbarBadge.start',return_value=False):
            w=Dashboard(start_worker=False)
            self.addCleanup(lambda:(w.tray.hide(),w._cleanup_badge(),w.deleteLater()))
            w.taskbar_badge_switch.setChecked(False)
            APP.processEvents()
            self.assertFalse(w.badge_action.isChecked())
            self.assertEqual(str(w.settings.value('taskbar_badge')).lower(),'false')
            w.badge_action.setChecked(True)
            APP.processEvents()
            self.assertTrue(w.taskbar_badge_switch.isChecked())
            self.assertEqual(str(w.settings.value('taskbar_badge')).lower(),'true')

    def test_50000_rows_load_and_5000_points_render_performance(self):
        from dashboard import Dashboard
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root=Path(tmp)
            with patch.object(store,'APP_DIR',root), patch.object(store,'HISTORY_PATH',root/'v02.csv'), patch.object(store,'V01_HISTORY',root/'v01.csv'), patch.object(store,'SAMPLES_PATH',root/'battery_samples.v1.csv'):
                now=datetime.now().astimezone()
                base_ts=now-timedelta(days=180)
                with store.SAMPLES_PATH.open('w',encoding='utf-8-sig',newline='') as f:
                    writer=csv.writer(f);writer.writerow(store.HEADER)
                    rows_data=[]
                    for i in range(50000):
                        ts=(base_ts+timedelta(minutes=5*i)).isoformat(timespec='seconds')
                        pct=45+(i%20)
                        mv=3870+(i%120)
                        rows_data.append([1,f'q-{i}','',ts,ts,ts,'ATK F1 V2 Ultimate (8K)','dev-1','receiver_serial_hash',pct,1,1,mv,mv,'candidate_in_range','unknown',2,0,'auto',0,0,140.0,'unverified','0.6'])
                    writer.writerows(rows_data)
                t0=time.perf_counter()
                loaded=store.read_samples()
                load_ms=(time.perf_counter()-t0)*1000
                print(f'METRIC read_50000_rows_ms={load_ms:.2f}')
                self.assertEqual(len(loaded),50000)

                w=Dashboard(start_worker=False)
                self.addCleanup(lambda:(w.tray.hide(),w._cleanup_badge(),w.deleteLater()))
                w.resize(960,640)
                t1=time.perf_counter()
                w.history_chart.resize(880,240)
                w.history_chart.set_data(loaded[-5000:],'percent',show_firmware=True,auto_scale=True)
                pm=w.history_chart.grab()
                canvas_pm=w.centralWidget().grab()
                render_ms=(time.perf_counter()-t1)*1000
                print(f'METRIC render_5000_points_ms={render_ms:.2f}')
                self.assertFalse(pm.isNull())
                self.assertFalse(canvas_pm.isNull())
                self.assertLess(render_ms,1500.0)


class CleanRoomAndMultiDeviceV07Tests(unittest.TestCase):
    def test_repo_clean_of_mouse_tray_and_old_debug_env(self):
        root=Path(__file__).resolve().parents[1]
        self.assertFalse((root/'mouse_tray').exists())
        self.assertFalse((root/'upstream-README.md').exists())
        self.assertFalse((root/'upstream-pyproject.toml').exists())
        bad_hits=[]
        for p in root.rglob('*'):
            if not p.is_file():
                continue
            rel=p.relative_to(root).as_posix()
            if any(rel.startswith(prefix) for prefix in ('backup_', 'catalog_research/', '.venv/', '__pycache__/', 'analysis_reports/', '.test-temp/')):
                continue
            if p.suffix.lower() not in ('.py', '.txt', '.md', '.cmd', '.json', '.toml'):
                continue
            txt=p.read_text(encoding='utf-8', errors='ignore')
            # Allow test file itself only where asserting absence via string literals
            if rel == 'tests/test_reliability.py':
                continue
            if 'mouse_tray' in txt.lower() or 'mouse_tray_debug' in txt.lower():
                bad_hits.append(rel)
        self.assertEqual(bad_hits, [], f'Found forbidden legacy references in: {bad_hits}')

    def test_three_protocol_codecs_whitelist_and_unknown_charge(self):
        # 1) nordic52 with link_byte=1 must still keep charging_state='unknown'
        c52=Nordic52Codec()
        r52=c52.parse_response(packet(percent=80, voltage=4167, link=1))
        self.assertTrue(r52.present and r52.percent_valid)
        self.assertEqual(r52.percent,80)
        self.assertEqual(r52.voltage_mv,4167)
        self.assertEqual(r52.link_byte,1)
        self.assertIsNone(r52.charging)
        self.assertEqual(r52.charging_state,'unknown')

        # 2) nordic54 wireless (no voltage) vs wired (little-endian mV) + asleep
        c54=Nordic54Codec()
        req_wl=c54.build_request(wired=False)
        self.assertEqual((len(req_wl), req_wl[0], req_wl[2], req_wl[5], req_wl[6]), (64, 0x08, 0x72, 0x01, 0x07))
        resp_wl=[0]*64
        resp_wl[0]=0x08;resp_wl[1]=0x72;resp_wl[2]=0x00;resp_wl[5]=0x07;resp_wl[7]=77
        resp_wl[8]=0xAA;resp_wl[9]=0xBB  # wireless junk must be ignored
        parsed_wl=c54.parse_response(resp_wl, wired=False)
        self.assertTrue(parsed_wl.present and parsed_wl.percent_valid)
        self.assertEqual(parsed_wl.percent,77)
        self.assertIsNone(parsed_wl.voltage_mv)
        self.assertEqual(parsed_wl.voltage_validation,'not_supported_on_link')
        self.assertEqual(parsed_wl.charging_state,'unknown')

        resp_wd=[0]*64
        resp_wd[0]=0x08;resp_wd[1]=0x72;resp_wd[2]=0x00;resp_wd[5]=0x07;resp_wd[7]=92
        resp_wd[8]=4250 & 0xFF;resp_wd[9]=(4250 >> 8) & 0xFF
        parsed_wd=c54.parse_response(resp_wd, wired=True)
        self.assertEqual(parsed_wd.voltage_mv,4250)
        self.assertIsNone(parsed_wd.charging)
        self.assertEqual(parsed_wd.charging_state,'unknown')

        resp_sleep=[0]*64
        resp_sleep[0]=0x08;resp_sleep[1]=0x72;resp_sleep[2]=0x01;resp_sleep[5]=0x07
        parsed_sleep=c54.parse_response(resp_sleep, wired=False)
        self.assertTrue(parsed_sleep.asleep)
        self.assertEqual(parsed_sleep.failure_reason,'device_asleep')

        # 3) nano_ff05 must never build a 64B write request
        c_nano=NanoFf05Codec()
        self.assertEqual(c_nano.protocol_status,'pending_capture')
        self.assertIsNone(c_nano.build_request())
        self.assertEqual(c_nano.parse_response([0]*64).failure_reason,'protocol_pending_capture')

        # 4) Whitelist guard: unregistered 0xFF05 device must trigger 0 writes
        writes=[]
        class SpyDev:
            def open_path(self,p):writes.append(('open',p))
            def write(self,rep):writes.append(('write',rep));return len(rep)
            def read(self,l,timeout_ms=100):return []
            def close(self):pass
        unregistered_ff05=dict(vendor_id=0x9999, product_id=0x8888, interface_number=1,
                               usage_page=0xFF05, usage=0x0001, path=b'unknown-ff05', serial_number='S1')
        tmp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.addCleanup(tmp.cleanup)
        tq=TargetQuery(
            lambda:[unregistered_ff05],
            lambda:SpyDev(),
            lambda *a:nullcontext(),
            binding_store=BindingStore(Path(tmp.name)/'b.json'),
            legacy_history_probe=lambda:False,
        )
        res=tq('q-wl','auto',threading.Event())
        self.assertEqual(res['outcome'],'receiver_absent')
        self.assertEqual(writes,[])

        # Also whitelisted pending_capture 373B:1216 (nano_ff05) must never write 64B probe
        nano_dev=dict(vendor_id=0x373B, product_id=0x1216, interface_number=1,
                      usage_page=0xFF05, usage=0x0001, path=b'nano-ff05', serial_number='N1')
        res_nano=TargetQuery(
            lambda:[nano_dev],
            lambda:SpyDev(),
            lambda *a:nullcontext(),
            binding_store=BindingStore(Path(tmp.name)/'b.json'),
            legacy_history_probe=lambda:False,
        )('q-nano','auto',threading.Event())
        self.assertEqual(res_nano['outcome'],'protocol_pending_capture')
        self.assertEqual(writes,[])

    def test_v04_equivalence_and_uncalibrated_degradation(self):
        from voltage_soc import (
            ATK_F1_V2_ULTIMATE_PROFILE, VoltageSocEstimator, estimate_series,
        )
        now=datetime.now().astimezone()
        replay_mvs=[(4340,0),(4255,0),(4167,0),(4099,0),(4160,0),(3981,0),(3877,0),(3981,0),(3995,1),(3690,0)]
        rows=[
            dict(ts=now+timedelta(minutes=5*i), percent=90-(i*5)%80, mv=mv, link_byte=lb, source='V0.7')
            for i,(mv,lb) in enumerate(replay_mvs)
        ]
        baseline_est=VoltageSocEstimator()
        baseline=[baseline_est.step(r['mv'], link_byte=r['link_byte']) for r in rows]
        enriched, _ = estimate_series(rows, lut_profile_id=ATK_F1_V2_ULTIMATE_PROFILE.profile_id)
        self.assertEqual([r['percent_est'] for r in enriched], [b.percent_est for b in baseline])
        self.assertEqual([r['v_est_mv'] for r in enriched], [b.v_est_mv for b in baseline])
        self.assertEqual([r['sag_pending'] for r in enriched], [b.sag_pending for b in baseline])
        self.assertEqual([r['extrapolated_low'] for r in enriched], [b.extrapolated_low for b in baseline])

        # Uncalibrated model (lut_profile_id=None) skips LUT and uses firmware_reported
        uncal, uncal_est = estimate_series(rows, lut_profile_id=None)
        self.assertIsNone(uncal_est)
        self.assertTrue(all(r['percent_est'] is None for r in uncal))
        self.assertEqual([r['display_percent'] for r in uncal], [r['percent'] for r in rows])
        self.assertTrue(all(r['battery_source_mode']=='firmware_reported' for r in uncal))

    def test_v1_readonly_sha256_and_v2_multidevice_isolation(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root=Path(tmp)
            v1_path=root/'battery_samples.v1.csv'
            v2_path=root/'battery_samples.v2.csv'
            now=datetime.now().astimezone()
            ts1=(now-timedelta(minutes=30)).isoformat(timespec='milliseconds')
            with v1_path.open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.writer(f);w.writerow(store.HEADER)
                w.writerow([1,'v1-q1','',ts1,ts1,ts1,'ATK F1 V2 Ultimate (8K)','373b:11d9:f1serial00000001','receiver_serial_hash',65,1,1,4030,4030,'candidate_in_range','unknown',2,0,'auto',0,0,120.0,'unverified','0.2.1'])
            v1_sha_before=hashlib.sha256(v1_path.read_bytes()).hexdigest()

            with patch.object(store,'APP_DIR',root), \
                 patch.object(store,'HISTORY_PATH',root/'v02.csv'), \
                 patch.object(store,'V01_HISTORY',root/'v01.csv'), \
                 patch.object(store,'SAMPLES_PATH',v1_path), \
                 patch.object(store,'SAMPLES_V2_PATH',v2_path):
                self.assertTrue(store.has_legacy_f1_v1_history())
                e_f1=event('v2-q1')
                e_f1.update(device_uid='373b:11d9:f1serial00000001', device_id='373b:11d9:f1serial00000001',
                            vid_pid='373B:11D9', model_id='atk_f1_v2_ultimate', model_display_name='ATK F1 V2 Ultimate (8K)',
                            protocol_family='nordic52', link_mode='wireless_dongle', battery_source_mode='voltage_lut_1pct')
                e_m2=event('v2-q2')
                e_m2.update(percent=80, voltage_mv=4180, voltage_raw=4180,
                            device_uid='3554:f58a:secondmouse00002', device_id='3554:f58a:secondmouse00002',
                            vid_pid='3554:F58A', model_id='vxe_r1_pro_max', model_display_name='VXE R1 Pro Max',
                            protocol_family='nordic52', link_mode='wireless_dongle', battery_source_mode='firmware_reported')
                store.write_sample(e_f1)
                store.write_sample(e_m2)

                # v1 must stay byte-for-byte untouched
                self.assertEqual(hashlib.sha256(v1_path.read_bytes()).hexdigest(), v1_sha_before)
                # Filtered read for F1 with legacy enabled gets v1 + v2(F1) only
                f1_rows=store.read_samples(device_uid='373b:11d9:f1serial00000001', include_legacy_f1=True)
                self.assertEqual([r['query_id'] for r in f1_rows], ['v1-q1', 'v2-q1'])
                # Filtered read for Mouse 2 gets ONLY v2(M2), zero legacy or F1 contamination
                m2_rows=store.read_samples(device_uid='3554:f58a:secondmouse00002', include_legacy_f1=False)
                self.assertEqual([r['query_id'] for r in m2_rows], ['v2-q2'])

    def test_shared_pid_373b_11d9_binding_state_machine(self):
        reg=DeviceRegistry.load()
        rec=reg.get_receiver(0x373B, 0x11D9)
        self.assertIsNotNone(rec)
        self.assertTrue(rec.shared_pid_collision)

        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            bpath=Path(tmp)/'device_bindings.json'
            bstore=BindingStore(bpath)
            uid, basis = make_device_uid(0x373B, 0x11D9, 'SER-F1V2', b'path-1')

            # 1) Fresh env: no binding, no v1 history -> ambiguous_unbound, no recommendation, never writes binding
            r_fresh=resolve_binding(rec, uid, basis, reg, bstore, has_legacy_f1_v1_history=False)
            self.assertEqual(r_fresh.binding_state,'ambiguous_unbound')
            self.assertIsNone(r_fresh.bound_model)
            self.assertIsNone(r_fresh.recommended_model_id)
            self.assertFalse(bpath.exists())

            # 2) Upgrade env with v1 F1 history -> ambiguous_unbound + recommends atk_f1_v2_ultimate, still no auto-write
            r_upg=resolve_binding(rec, uid, basis, reg, bstore, has_legacy_f1_v1_history=True)
            self.assertEqual(r_upg.binding_state,'ambiguous_unbound')
            self.assertIsNone(r_upg.bound_model)
            self.assertEqual(r_upg.recommended_model_id,'atk_f1_v2_ultimate')
            self.assertFalse(bpath.exists())

            # 3) User confirms -> persists to device_bindings.json & next resolve becomes bound with F1 LUT
            bstore.set_binding(
                device_uid=uid, vid_pid='373B:11D9', selected_model_id='atk_f1_v2_ultimate',
                identity_basis=basis, include_legacy_v1_v01_v02=True,
            )
            r_bound=resolve_binding(rec, uid, basis, reg, bstore, has_legacy_f1_v1_history=True)
            self.assertEqual(r_bound.binding_state,'bound')
            self.assertIsNotNone(r_bound.bound_model)
            self.assertEqual(r_bound.bound_model.lut_profile_id,'atk_f1_v2_ultimate_cycle1_v1')

    def test_ui_collision_banner_confirm_and_badge_mount(self):
        from dashboard import Dashboard
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root=Path(tmp)
            bpath=root/'device_bindings.json'
            with patch('dashboard.read_samples',return_value=[]), \
                 patch('dashboard.BINDINGS_PATH', bpath), \
                 patch('dashboard.TaskbarBadge.start',return_value=True):
                w=Dashboard(start_worker=False)
                self.addCleanup(lambda:(w.tray.hide(),w._cleanup_badge(),w.deleteLater()))
                w.binding_store=BindingStore(bpath)
                now=datetime.now().astimezone()
                # Simulate ambiguous_unbound result on 373B:11D9 (4180 mV -> LUT would be 82%, firmware is 75%)
                ev=event('q-amb')
                ev.update(
                    percent=75, voltage_mv=4180, voltage_raw=4180,
                    device_uid='373b:11d9:amb123', device_id='373b:11d9:amb123',
                    vid_pid='373B:11D9', model_id=None, model_display_name='373B:11D9 (待确认型号)',
                    lut_profile_id=None, binding_state='ambiguous_unbound',
                    recommended_model_id='atk_f1_v2_ultimate',
                    candidate_models=[
                        dict(model_id='atk_f1_v2_ultimate', brand='ATK', display_name='ATK F1 V2 Ultimate (8K)', verification_tier='verified_hw', calibration_status='calibrated', lut_profile_id='atk_f1_v2_ultimate_cycle1_v1'),
                        dict(model_id='atk_a9_ultimate', brand='ATK', display_name='ATK A9 Ultimate', verification_tier='catalog_only', calibration_status='uncalibrated', lut_profile_id=None),
                    ],
                    last_success=dict(at=now, percent=75, voltage_mv=4180, link_byte=0, query_id='q-amb'),
                )
                w.on_result(ev)
                APP.processEvents()
                self.assertFalse(w.collision_banner.isHidden())
                self.assertEqual(w.main_pct.text(), '75%')  # Unbound -> firmware_reported, NOT 82%!
                self.assertEqual(w.collision_combo.currentData(), 'atk_f1_v2_ultimate')

                # User clicks confirm -> saves binding, hides banner, activates F1 LUT -> 82%
                w.confirm_ambiguous_model()
                APP.processEvents()
                self.assertTrue(w.collision_banner.isHidden())
                self.assertTrue(bpath.is_file())
                self.assertEqual(w.main_pct.text(), '82%')
                self.assertIsNotNone(w.badge)


class V08UiAndReleaseTests(unittest.TestCase):
    def test_cycle_metrics_and_sleep_gaps(self):
        from research import (
            compute_cycle_metrics, compute_sleep_gaps,
            format_cycle_elapsed, format_daily_drain, format_relative_age,
        )
        now = datetime.now().astimezone()
        rows = [
            dict(ts=now - timedelta(days=5), percent=25, mv=3740, source='V0.7', device_id='dev1'),
            dict(ts=now - timedelta(days=4, hours=23, minutes=5), percent=95, mv=4310, source='V0.7', device_id='dev1'),
            dict(ts=now - timedelta(days=4, hours=23), percent=100, mv=4340, source='V0.7', device_id='dev1'),
            dict(ts=now - timedelta(days=4), percent=90, mv=4230, source='V0.7', device_id='dev1'),
            # > 450s gap -> detected as sleep gap
            dict(ts=now - timedelta(days=2), percent=72, mv=4100, source='V0.7', device_id='dev1'),
            dict(ts=now, percent=60, mv=3990, source='V0.7', device_id='dev1'),
        ]
        m = compute_cycle_metrics(rows)
        self.assertIsNotNone(m)
        self.assertEqual(m.start_pct, 100)
        self.assertEqual(m.current_pct, 60)
        self.assertEqual(m.drop_pct, 40)
        self.assertGreaterEqual(len(m.sleep_gaps), 2)
        self.assertIsNotNone(m.avg_daily_drain)
        self.assertIn('天', format_cycle_elapsed(m.elapsed_seconds))
        self.assertIn('每日约', format_daily_drain(m.avg_daily_drain))
        self.assertEqual(format_daily_drain(None), '样本积累中')
        self.assertEqual(format_relative_age(now - timedelta(seconds=15), now=now), '刚刚')
        self.assertEqual(format_relative_age(now - timedelta(minutes=8), now=now), '8 分钟前')

        short_rows = [
            dict(ts=now - timedelta(minutes=20), percent=99, mv=4320, source='V0.7', device_id='dev1'),
            dict(ts=now, percent=99, mv=4318, source='V0.7', device_id='dev1'),
        ]
        m_short = compute_cycle_metrics(short_rows)
        self.assertIsNone(m_short.avg_daily_drain)
        self.assertEqual(len(compute_sleep_gaps(rows)), len(m.sleep_gaps) + 1)

    def test_offline_redacted_csv_and_zip_export(self):
        import zipfile
        from exporter import export_cycle_csv, export_redacted_diagnostic_zip, sanitize_text
        raw_secret = 'SN-998877-TOPSECRET'
        dirty = r'Path C:\Users\TestUser\AppData\Local\ATK\log.txt and \\?\hid#vid_373b&pid_11d9#8&abc and ' + raw_secret
        cleaned = sanitize_text(dirty, extra_secrets=[raw_secret])
        self.assertNotIn('TestUser', cleaned)
        self.assertNotIn(raw_secret, cleaned)
        self.assertNotIn('hid#vid_373b', cleaned)
        self.assertNotIn('C:\\', cleaned)

        now = datetime.now().astimezone()
        sample_rows = [
            dict(
                ts=now - timedelta(hours=2),
                timestamp_raw=r'C:\Users\TestUser\raw.csv',
                device_uid='373b:11d9:abcd1234',
                vid_pid='373B:11D9',
                model_id='atk_f1_v2_ultimate',
                protocol_family='nordic52',
                link_mode='wireless_dongle',
                battery_source_mode='voltage_lut_1pct',
                display_percent=84,
                percent=85,
                mv=4180,
                voltage_raw=4180,
                link_byte=0,
                query_mode='auto',
            )
        ]
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                export_cycle_csv(sample_rows, root / 'battery_samples.v1.csv')
            with self.assertRaises(ValueError):
                export_cycle_csv(sample_rows, root / 'battery_samples.v2.csv')

            csv_out = export_cycle_csv(sample_rows, root / 'cycle_redacted.csv')
            csv_body = csv_out.read_text(encoding='utf-8-sig')
            self.assertNotIn('TestUser', csv_body)
            self.assertIn('373B:11D9', csv_body)
            self.assertIn(',unknown,', csv_body)

            zip_out = export_redacted_diagnostic_zip(
                root / 'diag_redacted.zip',
                rows=sample_rows,
                device_info={
                    'device_uid': '373b:11d9:abcd1234',
                    'vid_pid': '373B:11D9',
                    'serial_number': raw_secret,
                    'device_path': r'\\?\hid#vid_373b&pid_11d9#8&1234',
                },
                extra_notes=dirty,
            )
            with zipfile.ZipFile(zip_out, 'r') as zf:
                names = set(zf.namelist())
                self.assertEqual(names, {'manifest.json', 'device_profile_snapshot.json', 'samples_redacted.csv', 'diagnostic_notes.txt'})
                for name in names:
                    txt = zf.read(name).decode('utf-8')
                    self.assertNotIn(raw_secret, txt)
                    self.assertNotIn('TestUser', txt)
                    self.assertNotIn('hid#vid_373b', txt)

    def test_v08_5_visual_states_custom_png_and_3_pages_smoke(self):
        from PySide6.QtGui import QImage
        from dashboard import Dashboard
        now = datetime.now().astimezone()
        sample_rows = [
            dict(ts=now - timedelta(hours=6), percent=90, mv=4230, source='V0.7', device_id='dev1', recovered=False, query_mode='auto'),
            dict(ts=now - timedelta(hours=3), percent=85, mv=4185, source='V0.7', device_id='dev1', recovered=False, query_mode='auto'),
            dict(ts=now - timedelta(minutes=10), percent=85, mv=4180, source='V0.7', device_id='dev1', recovered=False, query_mode='auto'),
        ]
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            png_path = Path(tmp) / 'custom_mouse.png'
            img = QImage(64, 64, QImage.Format.Format_ARGB32)
            img.fill(0xFF7C64B8)
            self.assertTrue(img.save(str(png_path)))

            with patch('dashboard.read_samples', return_value=sample_rows), \
                 patch('dashboard.TaskbarBadge.start', return_value=False):
                w = Dashboard(start_worker=False)
                self.addCleanup(lambda: (w.tray.hide(), w._cleanup_badge(), w.deleteLater()))
                w.show()
                APP.processEvents()

                # State 1: Empty / initial disconnected wait
                pm_init = w.grab()
                self.assertFalse(pm_init.isNull())

                # State 2: Normal connected live voltage reading (84%) + clean footer summary
                ev_ok = event('q-ok')
                ev_ok.update(percent=85, voltage_mv=4180, link_mode='wireless_dongle', last_success=dict(at=now, percent=85, voltage_mv=4180, link_byte=0, query_id='q-ok'))
                w.on_result(ev_ok)
                self.assertEqual(w.badge_est_type.text(), '● 电压估算')
                self.assertEqual(w.slot_state_pill.text(), '实时')
                self.assertEqual(w.slot_link_val.text(), '2.4G 无线')
                self.assertNotIn('字段语义待验证', w.home_chart_note.text())
                self.assertNotIn('百分比平台期', w.home_chart_note.text())
                self.assertNotIn('字段语义待验证', w.history_desc.text())
                self.assertNotIn('百分比平台期', w.history_desc.text())
                self.assertIn('耗电', w.home_chart_note.text())
                self.assertIn('峰谷', w.history_desc.text())

                # State 3: Low battery (<=20%) — two consecutive frames confirm sag past V0.4 sag_gate, top-right stays metric-only
                ev_low = event('q-low-1')
                ev_low.update(percent=15, voltage_mv=3660, last_success=dict(at=now, percent=15, voltage_mv=3660, link_byte=0, query_id='q-low-1'))
                w.on_result(ev_low)
                ev_low2 = event('q-low-2')
                ev_low2.update(percent=15, voltage_mv=3660, last_success=dict(at=now, percent=15, voltage_mv=3660, link_byte=0, query_id='q-low-2'))
                w.on_result(ev_low2)
                self.assertEqual(w.badge_est_type.text(), '● 电压估算')
                self.assertEqual(w.slot_state_pill.text(), '低电')

                # State 4: Stale/sleeping disconnected snapshot prefixes "休眠前 "
                ev_sleep = event('q-sleep')
                ev_sleep.update(
                    connected=False, outcome='device_asleep', receiver_visibility='present',
                    last_success=dict(at=now - timedelta(minutes=25), percent=80, voltage_mv=4160, link_byte=0, query_id='q-ok'),
                )
                w.on_result(ev_sleep)
                self.assertEqual(w.slot_state_pill.text(), '休眠')
                self.assertTrue(w.home_chart_note.text().startswith('休眠前 '))

                # State 5: Firmware-only uncalibrated device (zero LUT hallucination)
                ev_fw = event('q-fw')
                ev_fw.update(
                    device_uid='373b:1199:fwonly', model_id='vxe_r1_pro_max', model_display_name='VXE R1 Pro Max',
                    brand='VXE', binding_state='bound', lut_profile_id=None, percent=70, voltage_mv=None,
                    last_success=dict(at=now, percent=70, voltage_mv=None, link_byte=0, query_id='q-fw'),
                )
                w.on_result(ev_fw)
                self.assertEqual(w.badge_est_type.text(), '● 固件原报')
                self.assertEqual(w.main_pct.text(), '70%')

                # Custom PNG import & reset + topbar taskbar pill sync
                w.ring_widget.set_custom_image(str(png_path))
                self.assertTrue(w.ring_widget.has_custom_image())
                w._on_custom_image_reset()
                self.assertFalse(w.ring_widget.has_custom_image())

                w.taskbar_pill.setChecked(False)
                APP.processEvents()
                self.assertFalse(w.taskbar_badge_switch.isChecked())
                self.assertFalse(w.badge_action.isChecked())

                # Verify segmented controls, legend updates, and all 3 pages render cleanly
                self.assertIn('— 电量变化', w.home_legend.text())
                self.assertIn('■ 休眠空窗', w.home_legend.text())
                self.assertFalse(hasattr(w, 'history_desc_help'))
                w.home_period_seg.set_current('cycle', emit_signal=True)
                w.home_mode_seg.set_current('mv', emit_signal=True)
                self.assertIn('— 电压变化', w.home_legend.text())
                for idx in (0, 1, 2):
                    w.select_page(idx)
                    APP.processEvents()
                    self.assertFalse(w.grab().isNull())

    def test_window_buttons_and_close_hides_to_tray_keeping_badge_alive(self):
        from PySide6.QtCore import Qt
        from dashboard import Dashboard
        with patch('dashboard.read_samples', return_value=[]), \
             patch('dashboard.TaskbarBadge.start', return_value=True):
            w = Dashboard(start_worker=False)
            self.addCleanup(lambda: (w.tray.hide(), w._cleanup_badge(), w.deleteLater()))
            flags = w.windowFlags()
            self.assertTrue(bool(flags & Qt.WindowType.WindowMinimizeButtonHint))
            self.assertTrue(bool(flags & Qt.WindowType.WindowCloseButtonHint))
            self.assertFalse(bool(flags & Qt.WindowType.WindowMaximizeButtonHint))
            self.assertIsNotNone(w.badge)
            w.show()
            APP.processEvents()
            self.assertTrue(w.isVisible())

            # Closing window hides to tray/background instead of quitting or destroying badge
            w.close()
            APP.processEvents()
            self.assertFalse(w.isVisible())
            self.assertFalse(w._closing)
            self.assertFalse(w.allow_quit)
            self.assertIsNotNone(w.badge)

    def test_adaptive_sleep_gap_thresholds_across_windows(self):
        from research import adaptive_sleep_gap_s, compute_sleep_gaps, chart_break
        self.assertEqual(adaptive_sleep_gap_s(1), 450)
        self.assertEqual(adaptive_sleep_gap_s(24), 2700)
        self.assertEqual(adaptive_sleep_gap_s(7 * 24), 10800)
        self.assertEqual(adaptive_sleep_gap_s('cycle'), 10800)
        self.assertEqual(adaptive_sleep_gap_s(30 * 24), 43200)
        self.assertEqual(adaptive_sleep_gap_s(None), 43200)

        now = datetime.now().astimezone()
        # 30-min, 2-hour, and 6-hour gaps
        r0 = dict(ts=now - timedelta(hours=10), percent=90, mv=4230, source='V0.7', device_id='dev1')
        r_30m = dict(ts=now - timedelta(hours=9, minutes=30), percent=89, mv=4220, source='V0.7', device_id='dev1')
        r_2h = dict(ts=now - timedelta(hours=7, minutes=30), percent=88, mv=4210, source='V0.7', device_id='dev1')
        r_6h = dict(ts=now - timedelta(hours=1, minutes=30), percent=86, mv=4190, source='V0.7', device_id='dev1')
        r_13h = dict(ts=now + timedelta(hours=11, minutes=30), percent=85, mv=4180, source='V0.7', device_id='dev1')
        series = [r0, r_30m, r_2h, r_6h, r_13h]

        # 24h (45m): 30m does NOT break; 2h, 6h, 13h break
        self.assertFalse(chart_break(r0, r_30m, gap_threshold_s=adaptive_sleep_gap_s(24)))
        self.assertEqual(len(compute_sleep_gaps(series, gap_threshold_s=adaptive_sleep_gap_s(24))), 3)

        # 7d / cycle (3h): 2h does NOT break; 6h, 13h break
        self.assertFalse(chart_break(r_30m, r_2h, gap_threshold_s=adaptive_sleep_gap_s(168)))
        self.assertEqual(len(compute_sleep_gaps(series, gap_threshold_s=adaptive_sleep_gap_s(168))), 2)

        # 30d (12h): 6h does NOT break (eliminates zebra stripes); only 13h breaks
        self.assertFalse(chart_break(r_2h, r_6h, gap_threshold_s=adaptive_sleep_gap_s(30 * 24)))
        self.assertEqual(len(compute_sleep_gaps(series, gap_threshold_s=adaptive_sleep_gap_s(30 * 24))), 1)

    def test_open_source_files_and_cleanliness(self):
        root = Path(__file__).resolve().parent.parent
        for rel in (
            'LICENSE',
            'README.md',
            '.gitignore',
            '.github/ISSUE_TEMPLATE/device_verification.yml',
            '.github/ISSUE_TEMPLATE/lut_calibration.yml',
        ):
            p = root / rel
            self.assertTrue(p.is_file(), f'Missing OSS file: {rel}')
            self.assertGreater(p.stat().st_size, 50)
        self.assertIn('GNU GENERAL PUBLIC LICENSE', (root / 'LICENSE').read_text(encoding='utf-8'))
        self.assertIn('永久免费开源', (root / 'README.md').read_text(encoding='utf-8'))
        self.assertIn('@liaman-li', (root / 'README.md').read_text(encoding='utf-8'))

        # Verify launcher cleanup & brand icon assets exist
        self.assertTrue((root / '启动 ATK-Pulse.cmd').is_file())
        self.assertFalse((root / '启动 ATK Battery V0.2.cmd').exists())
        self.assertFalse((root / 'v02_smoke.png').exists())
        self.assertFalse((root / 'v02_settings_smoke.png').exists())
        self.assertTrue((root / 'assets' / 'atk_pulse.ico').is_file())
        self.assertTrue((root / 'assets' / 'atk_pulse.png').is_file())

        # Verify zero mouse_tray / MOUSE_TRAY_DEBUG leakage & zero built-in mouse SVG silhouette
        for py in root.glob('*.py'):
            txt = py.read_text(encoding='utf-8')
            self.assertNotIn('mouse_tray', txt, f'Found mouse_tray in {py.name}')
            self.assertNotIn('MOUSE_TRAY_DEBUG', txt, f'Found MOUSE_TRAY_DEBUG in {py.name}')
        for fn in ('ui_theme.py', 'dashboard.py'):
            txt = (root / fn).read_text(encoding='utf-8')
            self.assertNotIn('QSvgRenderer', txt)
            self.assertNotIn('<svg', txt.lower())

    def test_v08_source_tags_brand_icon_author_and_frozen_autostart(self):
        from dashboard import Dashboard, load_app_icon
        icon = load_app_icon()
        self.assertFalse(icon.isNull())

        # Verify v2 source label reads 0.7.0 -> V0.7 and 0.8.0 -> V0.8 without rewriting v2 schema
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            v2p = root / 'battery_samples.v2.csv'
            with patch.object(store, 'APP_DIR', root), \
                 patch.object(store, 'HISTORY_PATH', root / 'missing_v02.csv'), \
                 patch.object(store, 'V01_HISTORY', root / 'missing_v01.csv'), \
                 patch.object(store, 'SAMPLES_PATH', root / 'missing_v1.csv'), \
                 patch.object(store, 'SAMPLES_V2_PATH', v2p):
                ev_old = event('q-07')
                ev_old.update(vid_pid='373B:11D9', model_id='atk_f1_v2_ultimate', protocol_family='nordic52', link_mode='wireless_dongle', battery_source_mode='voltage_lut_1pct')
                with patch.object(store, 'APP_VERSION', '0.7.0'):
                    store.write_sample(ev_old)
                ev_new = event('q-08')
                ev_new.update(at=ev_old['at'] + timedelta(minutes=5), vid_pid='373B:11D9', model_id='atk_f1_v2_ultimate', protocol_family='nordic52', link_mode='wireless_dongle', battery_source_mode='voltage_lut_1pct')
                store.write_sample(ev_new)
                rows = store.read_samples()
                self.assertEqual([r['source'] for r in rows], ['V0.7', 'V0.8'])

        with patch('dashboard.read_samples', return_value=[]):
            w = Dashboard(start_worker=False)
            self.addCleanup(lambda: (w.tray.hide(), w._cleanup_badge(), w.deleteLater()))
            self.assertIn('by @liaman-li', w.status_right.text())
            self.assertFalse(w.windowIcon().isNull())
            self.assertFalse(w.tray.icon().isNull())
            # Source mode vs frozen PyInstaller mode autostart command
            self.assertIn('main.py', Dashboard._autostart_command())
            with patch.object(sys, 'frozen', True, create=True), \
                 patch.object(sys, 'executable', r'C:\Apps\ATK-Pulse\ATK-Pulse.exe'):
                cmd = Dashboard._autostart_command()
                self.assertEqual(cmd, '"C:\\Apps\\ATK-Pulse\\ATK-Pulse.exe" --minimized')
                self.assertNotIn('main.py', cmd)

    def test_tray_hide_trims_backdrop_throttles_timer_and_defers_history(self):
        from dashboard import Dashboard, trim_process_working_set
        self.assertIsInstance(trim_process_working_set(), bool)
        with patch('dashboard.read_samples', return_value=[]):
            w = Dashboard(start_worker=False)
            self.addCleanup(lambda: (w.tray.hide(), w._cleanup_badge(), w.deleteLater()))
            w.show()
            APP.processEvents()
            canvas = w.centralWidget()
            self.assertFalse(w._hidden_to_tray)
            self.assertEqual(w.tick.interval(), 1000)
            self.assertIsNotNone(canvas._cache_pm)

            # Hide to tray: clears cached backdrop pixmap, throttles tick to 5s, marks _hidden_to_tray
            w.hide()
            APP.processEvents()
            self.assertTrue(w._hidden_to_tray)
            self.assertIsNone(canvas._cache_pm)
            self.assertEqual(w.tick.interval(), 5000)

            # Live reading while hidden defers chart/table refresh_history
            now = datetime.now().astimezone()
            ev = event('q-bg')
            ev.update(last_success=dict(at=now, percent=80, voltage_mv=4160, link_byte=0, query_id='q-bg'))
            with patch.object(w, 'refresh_history') as mock_ref:
                w.on_result(ev)
                mock_ref.assert_not_called()
            self.assertTrue(w._history_dirty)

            # Restoring window flushes deferred history & restores 1s timer
            with patch.object(w, 'refresh_history') as mock_ref:
                w.show()
                APP.processEvents()
                mock_ref.assert_called_once()
            self.assertFalse(w._hidden_to_tray)
            self.assertFalse(w._history_dirty)
            self.assertEqual(w.tick.interval(), 1000)


if __name__=='__main__':
    unittest.main(verbosity=2)
