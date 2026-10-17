import os, sqlite3, csv, io, json, urllib.request, urllib.error, secrets
from datetime import datetime, timezone
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Depends
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler

DB=os.getenv('DB_PATH','parcels.sqlite3')
API_TOKEN=os.getenv('APP_API_TOKEN','')
app=FastAPI(title='TikTok Parcel Manager Backend',version='0.2.0')

def auth(authorization: str|None=Header(default=None)):
    if not API_TOKEN or not authorization or not secrets.compare_digest(authorization, 'Bearer '+API_TOKEN):
        raise HTTPException(401,'Configure APP_API_TOKEN and provide a valid Bearer token')

@contextmanager
def connect():
    c=sqlite3.connect(DB,timeout=15)
    c.row_factory=sqlite3.Row
    try:
        yield c
        c.commit()
    finally:c.close()

def init():
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, label TEXT NOT NULL UNIQUE)')
        c.execute('''CREATE TABLE IF NOT EXISTS parcels(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL REFERENCES accounts(id),tracking_no TEXT NOT NULL,carrier TEXT NOT NULL DEFAULT 'jnt',status TEXT NOT NULL DEFAULT 'unknown',last_event TEXT,last_checked TEXT,raw_json TEXT, UNIQUE(account_id,tracking_no))''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_parcels_tracking ON parcels(tracking_no)')

class Account(BaseModel):
    label:str=Field(min_length=1,max_length=100)
class Parcel(BaseModel):
    account_id:int
    tracking_no:str=Field(min_length=5,max_length=80)
    carrier:str='jnt'

def fetch_tracking(number):
    key=os.getenv('ETRACKINGS_API_KEY')
    secret=os.getenv('ETRACKINGS_KEY_SECRET')
    courier=os.getenv('ETRACKINGS_JNT_COURIER')
    if not (key and secret and courier):
        raise RuntimeError('Missing ETRACKINGS_API_KEY, ETRACKINGS_KEY_SECRET or confirmed ETRACKINGS_JNT_COURIER')
    payload=json.dumps({'courier':courier,'trackingNo':number}).encode()
    req=urllib.request.Request('https://api.etrackings.com/api/v3/tracks/find',data=payload,headers={'Content-Type':'application/json','Etrackings-api-key':key,'Etrackings-key-secret':secret},method='POST')
    with urllib.request.urlopen(req,timeout=20) as response:
        result=json.load(response)
    if result.get('meta',{}).get('code')!=200:
        raise RuntimeError('Tracking provider rejected request: '+str(result.get('meta',{}).get('message','unknown error')))
    return result.get('data') or {}

def normalize(raw):
    code=str(raw.get('status','')).upper()
    if 'DELIVERED' in code:return 'delivered'
    if 'CANCEL' in code:return 'cancelled'
    if 'SHIPPING' in code or 'TRANSIT' in code or 'PICKUP' in code:return 'transit'
    if 'EXCEPTION' in code or 'FAIL' in code:return 'exception'
    return 'unknown'

def refresh_one(parcel_id):
    with connect() as c:
        row=c.execute('SELECT * FROM parcels WHERE id=?',(parcel_id,)).fetchone()
    if row is None:raise ValueError('Parcel not found')
    raw=fetch_tracking(row['tracking_no'])
    status=normalize(raw)
    with connect() as c:
        c.execute('UPDATE parcels SET status=?,raw_json=?,last_event=?,last_checked=? WHERE id=?',(status,json.dumps(raw,ensure_ascii=False),str(raw.get('status','')),datetime.now(timezone.utc).isoformat(),parcel_id))
    return {'id':parcel_id,'status':status,'provider_status':raw.get('status')}

@app.on_event('startup')
def startup():
    init()
    minutes=int(os.getenv('POLL_MINUTES','60'))
    if minutes>=15 and os.getenv('ENABLE_POLLING','false').lower()=='true':
        scheduler.add_job(poll,'interval',minutes=minutes,max_instances=1,coalesce=True)
        scheduler.start()

scheduler=BackgroundScheduler()
last_poll_at = None
def poll():
    global last_poll_at
    last_poll_at = datetime.now(timezone.utc).isoformat()
    with connect() as c:
        rows = c.execute(
            "SELECT id FROM parcels WHERE status NOT IN ('delivered','cancelled') ORDER BY COALESCE(last_checked,'') LIMIT ?",
            (int(os.getenv('POLL_BATCH_SIZE', '25')),)
        ).fetchall()
    for r in rows:
        try:
            refresh_one(r['id'])
        except Exception as exc:
            print('Refresh failed:', r['id'], type(exc).__name__)

@app.get('/health')
def health():
    return {
        'ok': True,
        'provider_configured': bool(
            os.getenv('ETRACKINGS_API_KEY')
            and os.getenv('ETRACKINGS_KEY_SECRET')
            and os.getenv('ETRACKINGS_JNT_COURIER')
        ),
        'polling_enabled': scheduler.running,
        'last_poll_at': last_poll_at
    }

@app.post('/accounts',dependencies=[Depends(auth)])
def add_account(item:Account):
    try:
        with connect() as c:
            cur=c.execute('INSERT INTO accounts(label) VALUES(?)',(item.label.strip(),))
            return {'id':cur.lastrowid,'label':item.label.strip()}
    except sqlite3.IntegrityError:raise HTTPException(409,'Account label already exists')

@app.get('/accounts',dependencies=[Depends(auth)])
def accounts():
    with connect() as c:return [dict(x) for x in c.execute('SELECT * FROM accounts ORDER BY label')]

@app.post('/parcels',dependencies=[Depends(auth)])
def add_parcel(item:Parcel):
    with connect() as c:
        if not c.execute('SELECT id FROM accounts WHERE id=?',(item.account_id,)).fetchone():raise HTTPException(404,'Account not found')
        try:
            cur=c.execute('INSERT INTO parcels(account_id,tracking_no,carrier) VALUES(?,?,?)',(item.account_id,item.tracking_no.strip(),item.carrier))
            return {'id':cur.lastrowid}
        except sqlite3.IntegrityError:raise HTTPException(409,'Parcel already exists for this account')

@app.get('/parcels',dependencies=[Depends(auth)])
def parcels(account_id:int|None=None,status:str|None=None,q:str|None=None,limit:int=100,offset:int=0):
    clauses=[]; args=[]
    if account_id is not None:clauses.append('p.account_id=?');args.append(account_id)
    if status:clauses.append('p.status=?');args.append(status)
    if q:clauses.append('(p.tracking_no LIKE ? OR a.label LIKE ?)');args.extend(['%'+q+'%']*2)
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    with connect() as c:return [dict(x) for x in c.execute('SELECT p.id,p.account_id,a.label AS account,p.tracking_no,p.carrier,p.status,p.last_event,p.last_checked FROM parcels p JOIN accounts a ON a.id=p.account_id'+where+' ORDER BY p.id DESC LIMIT ? OFFSET ?',args+[min(max(limit,1),500),max(offset,0)])]

@app.get('/summary',dependencies=[Depends(auth)])
def summary():
    with connect() as c:
        counts={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM parcels GROUP BY status')}
        return {'total':sum(counts.values()),'by_status':counts,'accounts':c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]}

@app.post('/parcels/{parcel_id}/refresh',dependencies=[Depends(auth)])
def refresh(parcel_id:int):
    try:return refresh_one(parcel_id)
    except ValueError:raise HTTPException(404,'Parcel not found')
    except (RuntimeError,urllib.error.URLError,TimeoutError) as e:raise HTTPException(502,str(e))

@app.post('/import-csv',dependencies=[Depends(auth)])
async def import_csv(file:UploadFile=File(...)):
    content=(await file.read()).decode('utf-8-sig')
    reader=csv.DictReader(io.StringIO(content))
    if not {'account','tracking_no'}.issubset(reader.fieldnames or []):raise HTTPException(400,'CSV requires account,tracking_no columns')
    inserted=0;skipped=0
    with connect() as c:
        for row in reader:
            label=row['account'].strip();number=row['tracking_no'].strip()
            if not label or not number:skipped+=1;continue
            c.execute('INSERT OR IGNORE INTO accounts(label) VALUES(?)',(label,))
            account_id=c.execute('SELECT id FROM accounts WHERE label=?',(label,)).fetchone()[0]
            cur=c.execute('INSERT OR IGNORE INTO parcels(account_id,tracking_no) VALUES(?,?)',(account_id,number))
            inserted+=cur.rowcount;skipped+=1-cur.rowcount
    return {'inserted':inserted,'skipped':skipped}

# Serve the web interface from the same origin as the API.
from fastapi.staticfiles import StaticFiles
app.mount('/ui', StaticFiles(directory=os.path.join(os.path.dirname(__file__), 'static'), html=True), name='ui')

from fastapi.responses import FileResponse

@app.get("/tiktok3VmtIrD1ImX9OQuusCvtOc3UqEMIWlY2.txt")
def tiktok_verification():
    return FileResponse(
        "static/tiktok3VmtIrD1ImX9OQuusCvtOc3UqEMIWlY2.txt",
        media_type="text/plain"
    )
