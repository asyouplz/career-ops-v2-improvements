"""Read-only, metadata-only performance report. Never opens mail body caches."""
from pathlib import Path
import argparse,collections,datetime as dt,json,re,os

def read_json(path,default):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return default

def timestamp(value):
    try:return dt.datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):return None

def number(value):
    return max(0,float(value)) if isinstance(value,(int,float)) and not isinstance(value,bool) else None

def provider_summary(diagnostics,now,can_be_active,scope):
    groups=collections.defaultdict(lambda:{'batches':0,'attempts':0,'retries':0,'transport_seconds':0.0,'max_transport_seconds':0.0,'legacy_elapsed_estimate_seconds':0.0})
    active=[];completed=0;failures=0;legacy=0;inactive_unfinished=0
    for path,diagnostic,belongs_to_current in diagnostics:
        attempts=diagnostic.get('attempts') or []
        ops=diagnostic.get('operation_counts') or dict(collections.Counter(r.get('operation','unknown') for a in attempts[:1] for r in a.get('requests',[])))
        allowed=lambda op:op in {'profile','history','search_email_ids','read_email_thread','read_email','read_email:raw','read_email:full','read_email:metadata','read_email:minimal','batch_read_email_threads'}
        group='+'.join(sorted(op for op in ops if allowed(op))) or 'unknown'
        row=groups[group];row['batches']+=1;row['attempts']+=len(attempts);row['retries']+=max(0,len(attempts)-1)
        status=diagnostic.get('status')
        if status=='complete':completed+=1
        elif status in {'failed','duplicate_response'}:failures+=1
        elif can_be_active and belongs_to_current:
            started=timestamp(diagnostic.get('started_at'))
            active.append({'operations':group,'status':status,'elapsed_seconds':round(max(0,now-started),3) if started is not None else None,'attempt':len(attempts)})
        else:inactive_unfinished+=1
        # v2 records own transport only; association and batch elapsed include
        # child requests and must never be added to the total transport time.
        transport=number(diagnostic.get('transport_seconds'))
        if transport is not None:
            row['transport_seconds']+=transport;row['max_transport_seconds']=max(row['max_transport_seconds'],transport)
        elif status in {'complete','failed','duplicate_response'}:
            started=timestamp(diagnostic.get('started_at'))
            if started is not None:row['legacy_elapsed_estimate_seconds']+=max(0,path.stat().st_mtime-started);legacy+=1
    return {'scope':scope,'diagnostics':len(diagnostics),'completed_batches':completed,'failed_batches':failures,
            'active_batches':active,'inactive_unfinished_batches':inactive_unfinished,
            'transport_seconds':round(sum(v['transport_seconds'] for v in groups.values()),3),
            'retries':sum(v['retries'] for v in groups.values()),'groups':{k:{name:round(value,3) if isinstance(value,float) else value for name,value in v.items()} for k,v in sorted(groups.items())},
            'legacy_estimate_batches':legacy,'legacy_note':'Legacy estimates use file modification times and are not additive across nested calls.' if legacy else None}

def report(data,run_id=None,now=None):
    data=Path(data);state=read_json(data/'mail-sync-state.json',{})
    run_id=run_id or state.get('run_id')
    if not isinstance(run_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',run_id):raise ValueError('A valid synchronization run ID is required')
    records=[]
    try:
        for line in (data/'mail-sync-history.jsonl').read_text().splitlines():
            try:record=json.loads(line)
            except ValueError:continue
            if isinstance(record,dict) and record.get('run_id')==run_id:records.append(record)
    except FileNotFoundError:pass
    current=state if state.get('run_id')==run_id else records[-1] if records else {}
    now=now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    start=timestamp(current.get('started_at'));end=timestamp(current.get('completed_at'))
    elapsed=number(current.get('duration_seconds'))
    if elapsed is None and start is not None:elapsed=max(0,(end if end is not None else now)-start)
    # A checkpoint's run ID survives retries and reclassification. Its state
    # timer belongs to the latest process attempt, while cache diagnostics span
    # every attempt. Keep both scopes explicit instead of mixing their totals.
    attempt_id=current.get('attempt_id')
    scoped=start is not None or bool(attempt_id)
    diagnostics=[];unassigned=0
    diagnostic_dirs=[data/'provider-cache'/run_id/'diagnostics',data/'gmail-preflight-diagnostics'/run_id/'diagnostics']
    # Account directories contain private body caches too. Enumerate only
    # metadata diagnostics for this run, without opening the thread caches.
    for account in (data/'gmail-accounts').glob('*'):
        if not account.is_symlink() and re.fullmatch(r'[0-9a-f]{64}',account.name):
            diagnostic_dirs.append(account/'provider-diagnostics'/run_id/'diagnostics')
    for path in (path for directory in diagnostic_dirs for path in directory.glob('*.json')):
        diagnostic=read_json(path,{})
        if not isinstance(diagnostic,dict) or not diagnostic:continue
        diagnostic_start=timestamp(diagnostic.get('started_at'))
        if attempt_id and diagnostic.get('attempt_id'):
            belongs=diagnostic['attempt_id']==attempt_id
        elif start is not None and diagnostic_start is not None:
            belongs=diagnostic_start>=start and (end is None or diagnostic_start<=end)
        elif not scoped:
            belongs=True
        else:
            belongs=False;unassigned+=1
        diagnostics.append((path,diagnostic,belongs))
    current_diagnostics=[row for row in diagnostics if row[2]]
    active=current.get('status') in {'running','queued'}
    phase_seconds={str(k):round(v,3) for k,value in (current.get('phase_durations') or {}).items() if (v:=number(value)) is not None and re.fullmatch(r'[a-z_]{1,40}',str(k))}
    return {'run_id':run_id,'attempt_id':attempt_id,'status':current.get('status'),'started_at':current.get('started_at'),'completed_at':current.get('completed_at'),
            'elapsed_seconds':round(elapsed,3) if elapsed is not None else None,'run_mode':current.get('run_mode'),'resumed':current.get('resumed'),
            'mail_provider':current.get('provider'),'sync_mode':current.get('sync_mode'),
            'processed_threads':current.get('processed_threads'),'total_threads':current.get('total_threads'),'phase_seconds':phase_seconds,
            'attempt_history_count':len(records),'unassigned_diagnostics':unassigned,
            'provider':provider_summary(current_diagnostics,now,active,'current_attempt' if scoped else 'logical_run_unscoped'),
            'logical_run_provider':provider_summary(diagnostics,now,active,'logical_run')}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('DASHBOARD_DATA_DIR', str(Path(os.environ.get('CAREER_OPS_ROOT', str(Path(__file__).resolve().parents[3] / 'engine'))) / 'data'))))
    parser.add_argument('--run-id')
    args=parser.parse_args()
    print(json.dumps(report(args.data_dir,args.run_id),ensure_ascii=False,indent=2))
