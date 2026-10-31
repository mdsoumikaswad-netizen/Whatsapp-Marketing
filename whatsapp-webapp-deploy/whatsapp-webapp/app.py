import csv, io, os, re, secrets, sqlite3, time, hmac
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import requests
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

BASE=Path(__file__).resolve().parent
DB=Path(os.getenv('DATABASE_PATH', str(BASE/'data'/'campaigns.db')))
DB.parent.mkdir(parents=True,exist_ok=True)
APP_PASSWORD=os.getenv('ADMIN_PASSWORD','')
SECRET=os.getenv('SESSION_SECRET','')
if not APP_PASSWORD or not SECRET or len(SECRET)<32:
    raise RuntimeError('Set ADMIN_PASSWORD and SESSION_SECRET (32+ characters) in environment')
app=FastAPI(title='WhatsApp Campaign Studio',docs_url=None,redoc_url=None)
app.add_middleware(SessionMiddleware,secret_key=SECRET,https_only=os.getenv('COOKIE_SECURE','true').lower()=='true',same_site='lax',max_age=3600*8)
templates=Jinja2Templates(directory=str(BASE/'templates'))
app.mount('/static',StaticFiles(directory=str(BASE/'static')),name='static')

@contextmanager
def db():
    conn=sqlite3.connect(DB,timeout=30)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        yield conn
        conn.commit()
    finally: conn.close()
with db() as c:
    c.executescript('''CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE, consent_at TEXT NOT NULL, consent_source TEXT NOT NULL, opted_in INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY, name TEXT NOT NULL, template_name TEXT NOT NULL, language TEXT NOT NULL, variable_name TEXT NOT NULL, created_at TEXT NOT NULL, dry_run INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL, contact_id INTEGER NOT NULL, status TEXT NOT NULL, message_id TEXT, details TEXT, sent_at TEXT NOT NULL, UNIQUE(campaign_id,contact_id));
    CREATE INDEX IF NOT EXISTS idx_messages_mid ON messages(message_id);''')

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def clean_phone(v):
    v=re.sub(r'[\s()\-]','',str(v or ''))
    if not re.fullmatch(r'\+?[1-9]\d{7,14}',v): raise ValueError('Use international format, e.g. +8801XXXXXXXXX')
    return v.lstrip('+')
def auth(request): return request.session.get('authenticated') is True
def page(request, name, **kwargs):
    return templates.TemplateResponse(request,name,{'request':request,**kwargs})
def redirect(path='/'): return RedirectResponse(path,status_code=303)
def csrf(request, token):
    if not token or not hmac.compare_digest(str(token),str(request.session.get('csrf',''))): raise HTTPException(403,'Invalid form token')
def require(request):
    if not auth(request): raise HTTPException(401,'Sign in required')

def view(request, notice=''):
    require(request)
    with db() as c:
        contacts=c.execute('SELECT * FROM contacts ORDER BY id DESC LIMIT 300').fetchall()
        campaigns=c.execute('''SELECT a.*,count(m.id) total, sum(CASE WHEN m.status='accepted' THEN 1 ELSE 0 END) accepted, sum(CASE WHEN m.status='failed' THEN 1 ELSE 0 END) failed FROM campaigns a LEFT JOIN messages m ON a.id=m.campaign_id GROUP BY a.id ORDER BY a.id DESC LIMIT 30''').fetchall()
        count=c.execute('SELECT count(*) n FROM contacts WHERE opted_in=1').fetchone()['n']
    return page(request,'dashboard.html',contacts=contacts,campaigns=campaigns,count=count,notice=notice,csrf_token=request.session['csrf'])

@app.get('/health')
def health(): return {'status':'ok'}
@app.get('/login',response_class=HTMLResponse)
def login_page(request:Request): return page(request,'login.html',error='')
@app.post('/login')
def login(request:Request,password:str=Form(...)):
    if not secrets.compare_digest(password,APP_PASSWORD): return page(request,'login.html',error='Incorrect password')
    request.session.clear();request.session['authenticated']=True;request.session['csrf']=secrets.token_urlsafe(32)
    return redirect()
@app.post('/logout')
def logout(request:Request,csrf_token:str=Form(...)):
    require(request);csrf(request,csrf_token);request.session.clear();return redirect('/login')
@app.get('/',response_class=HTMLResponse)
def home(request:Request):
    if not auth(request): return redirect('/login')
    return view(request)
@app.post('/contacts/import')
async def import_contacts(request:Request,upload:UploadFile=File(...),csrf_token:str=Form(...)):
    require(request);csrf(request,csrf_token)
    raw=await upload.read(2_000_001)
    if len(raw)>2_000_000: raise HTTPException(400,'CSV exceeds 2 MB')
    try: rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    except Exception: raise HTTPException(400,'Invalid UTF-8 CSV')
    if not rows or not {'name','phone','consent_at','consent_source'}.issubset(rows[0]): raise HTTPException(400,'Required headers: name,phone,consent_at,consent_source')
    added=0;skipped=0
    with db() as c:
        for row in rows[:5000]:
            try:
                phone=clean_phone(row['phone']);name=row['name'].strip()[:100];date=row['consent_at'].strip();source=row['consent_source'].strip()[:200]
                if not (name and date and source): raise ValueError()
                datetime.fromisoformat(date.replace('Z','+00:00'))
                if str(row.get('opted_in','yes')).strip().lower() not in ('yes','true','1'): raise ValueError()
                cur=c.execute('INSERT OR IGNORE INTO contacts(name,phone,consent_at,consent_source,created_at) VALUES(?,?,?,?,?)',(name,phone,date,source,now()))
                added+=cur.rowcount
            except (ValueError,TypeError,KeyError): skipped+=1
    return redirect(f'/?notice=Imported+{added}+contacts%3B+skipped+{skipped}+invalid+or+duplicate+rows')
@app.post('/contacts/optout')
def optout(request:Request,contact_id:int=Form(...),csrf_token:str=Form(...)):
    require(request);csrf(request,csrf_token)
    with db() as c:c.execute('UPDATE contacts SET opted_in=0 WHERE id=?',(contact_id,))
    return redirect('/?notice=Contact+unsubscribed')
@app.post('/campaigns/send')
def send(request:Request,name:str=Form(...),template_name:str=Form(...),language:str=Form('en'),variable_name:str=Form('name'),dry_run:str=Form('yes'),csrf_token:str=Form(...)):
    require(request);csrf(request,csrf_token)
    if not re.fullmatch(r'[a-z0-9_]{1,512}',template_name):raise HTTPException(400,'Invalid template name')
    if not re.fullmatch(r'[a-z]{2,3}(?:_[A-Z]{2})?',language):raise HTTPException(400,'Invalid language code')
    if variable_name not in ('name','none'):raise HTTPException(400,'Invalid variable selection')
    dry=dry_run=='yes'
    token=os.getenv('WHATSAPP_ACCESS_TOKEN','');phone_id=os.getenv('WHATSAPP_PHONE_NUMBER_ID','')
    if not dry and (not token or not phone_id):raise HTTPException(400,'WhatsApp API credentials not configured')
    with db() as c:
        contacts=c.execute('SELECT * FROM contacts WHERE opted_in=1 ORDER BY id LIMIT 50').fetchall()
        cur=c.execute('INSERT INTO campaigns(name,template_name,language,variable_name,created_at,dry_run) VALUES(?,?,?,?,?,?)',(name[:100],template_name,language,variable_name,now(),int(dry)))
        campaign_id=cur.lastrowid
        for contact in contacts:
            payload={'messaging_product':'whatsapp','to':contact['phone'],'type':'template','template':{'name':template_name,'language':{'code':language}}}
            if variable_name=='name':payload['template']['components']=[{'type':'body','parameters':[{'type':'text','text':contact['name']}]}]
            status='simulated' if dry else 'failed';mid=None;details='Dry run; no message submitted' if dry else ''
            if not dry:
                try:
                    resp=requests.post(f"https://graph.facebook.com/{os.getenv('WHATSAPP_API_VERSION','v23.0')}/{phone_id}/messages",headers={'Authorization':f'Bearer {token}'},json=payload,timeout=15)
                    data=resp.json()
                    if resp.ok and data.get('messages'):
                        status='accepted';mid=data['messages'][0].get('id');details='Accepted by Meta; not proof of delivery'
                    else: details=str(data.get('error',{}).get('message','API error'))[:300]
                except requests.RequestException as e: details=str(e)[:300]
                time.sleep(0.12)
            c.execute('INSERT INTO messages(campaign_id,contact_id,status,message_id,details,sent_at) VALUES(?,?,?,?,?,?)',(campaign_id,contact['id'],status,mid,details,now()))
    return redirect(f'/?notice=Campaign+created%3B+processed+{len(contacts)}+opted-in+contacts')
@app.get('/campaigns/{campaign_id}',response_class=HTMLResponse)
def campaign_detail(campaign_id:int,request:Request):
    require(request)
    with db() as c:
        campaign=c.execute('SELECT * FROM campaigns WHERE id=?',(campaign_id,)).fetchone()
        if not campaign:raise HTTPException(404)
        logs=c.execute('SELECT m.*,c.name,c.phone FROM messages m JOIN contacts c ON c.id=m.contact_id WHERE m.campaign_id=? ORDER BY m.id',(campaign_id,)).fetchall()
    return page(request,'campaign.html',campaign=campaign,logs=logs)
@app.get('/webhook')
def webhook_verify(request:Request):
    p=request.query_params
    if p.get('hub.mode')=='subscribe' and os.getenv('WHATSAPP_VERIFY_TOKEN') and secrets.compare_digest(p.get('hub.verify_token',''),os.getenv('WHATSAPP_VERIFY_TOKEN')):
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(p.get('hub.challenge',''))
    raise HTTPException(403)
@app.post('/webhook')
async def webhook_receive(request:Request):
    import hashlib
    app_secret=os.getenv('META_APP_SECRET','')
    if not app_secret:raise HTTPException(503,'Webhook signature verification not configured')
    raw=await request.body()
    signature=request.headers.get('x-hub-signature-256','')
    expected='sha256='+hmac.new(app_secret.encode(),raw,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature,expected):raise HTTPException(403)
    try: data=await request.json()
    except Exception:raise HTTPException(400)
    with db() as c:
        for entry in data.get('entry',[]):
            for change in entry.get('changes',[]):
                value=change.get('value',{})
                for s in value.get('statuses',[]):
                    if s.get('id') and s.get('status') in ('sent','delivered','read','failed'):
                        c.execute('UPDATE messages SET status=?,details=? WHERE message_id=?',(s['status'],str(s.get('errors',''))[:300],s['id']))
                for m in value.get('messages',[]):
                    body=(m.get('text') or {}).get('body','').strip().lower()
                    if re.fullmatch(r'(stop|unsubscribe|opt out|cancel|বন্ধ)',body):
                        try:c.execute('UPDATE contacts SET opted_in=0 WHERE phone=?',(clean_phone(m.get('from')),))
                        except ValueError:pass
    return JSONResponse({'ok':True})
