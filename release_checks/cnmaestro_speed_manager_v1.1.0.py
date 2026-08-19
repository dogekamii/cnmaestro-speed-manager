import asyncio,json,os,re,sqlite3,threading,time,ssl,random,sys,webbrowser
from datetime import datetime,timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk,messagebox
import httpx,truststore
APP_VERSION='1.1.0';APP_DIR=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent;UPDATE_CONFIG=APP_DIR/'update_config.json'
PKGS={"6 Mbps":("6mbps Package",6451,2150),"10 Mbps":("10mbps Package",10752,1075),"15 Mbps":("15mbps Package",16128,3225),"20 Mbps":("20mbps Package",21500,10752),"25 Mbps":("25mbps Package",26880,3225),"50 Mbps":("50mbps Package",53760,10750),"75 Mbps":("75mbps Package",80640,10750),"100 Mbps":("100mbps Package",107520,21500)}
OTHER='Other / Unmatched';PHRASE='APPLY SPEED CHANGES';CONCURRENCY=4;CACHE_HOURS=24
DATA=Path(os.getenv('LOCALAPPDATA',Path.home()))/'cnMaestroSpeedManager';DATA.mkdir(exist_ok=True);DB=DATA/'speed_manager.db';SETTINGS=DATA/'settings.json'
def initdb():
 with sqlite3.connect(DB) as c:
  c.execute('CREATE TABLE IF NOT EXISTS cache(mac TEXT PRIMARY KEY,payload TEXT,scanned REAL)');c.execute('CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,timestamp TEXT,mac TEXT,name TEXT,old_package TEXT,target_package TEXT,template TEXT,job_id TEXT,job_state TEXT,verified_package TEXT,success INTEGER,detail TEXT)')
def exactpkg(dl,ul):
 for n,(_,d,u) in PKGS.items():
  if dl==d and ul==u:return n
 return OTHER
def nearest(dl,ul,tolerance):
 if dl is None:return None
 name,spec=min(PKGS.items(),key=lambda x:abs(dl-x[1][1])/max(x[1][1],1));pct=abs(dl-spec[1])/spec[1]*100
 if pct>tolerance:return None
 return {'name':name,'dl_pct':(dl-spec[1])/spec[1]*100,'ul_pct':(ul-spec[2])/spec[2]*100 if ul is not None else None,'target_dl':spec[1],'target_ul':spec[2]}
def rates(t):
 d=re.search(r'"sustainedDownlinkDataRate"\s*:\s*(\d+)',t);u=re.search(r'"sustainedUplinkDataRate"\s*:\s*(\d+)',t)
 if not d or not u:raise ValueError('QoS values not found')
 return int(d[1]),int(u[1])
def age(sec):
 try:s=int(sec or 0)
 except:return 'Unknown'
 return f'{s//60}m' if s<3600 else (f'{s/3600:.1f}h' if s<86400 else f'{s/86400:.1f}d')
def save(r):
 with sqlite3.connect(DB) as c:c.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)',(r['mac'],json.dumps(r),time.time()))
def cached():
 out={}
 with sqlite3.connect(DB) as c:
  for m,p,t in c.execute('SELECT mac,payload,scanned FROM cache'):
   try:r=json.loads(p);r['cache_age_hours']=round((time.time()-t)/3600,1);r['stale']=time.time()-t>CACHE_HOURS*3600;out[m]=r
   except:pass
 return out
class API:
 def __init__(self,auth,cid,secret,notice):self.auth=auth.rstrip('/');self.cid=cid;self.secret=secret;self.notice=notice;self.token='';self.base='';self.exp=0;self.ctx=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT);self.rate={}
 async def client(self):
  c=httpx.AsyncClient(timeout=45,verify=self.ctx)
  if not self.token or self.exp<time.time()+60:
   r=await c.post(self.auth+'/api/v2/access/token',auth=(self.cid,self.secret),data={'grant_type':'client_credentials'});r.raise_for_status();b=r.json();self.token=b['access_token'];self.base=b.get('redirect_uri',self.auth).rstrip('/');self.exp=time.time()+int(b.get('expires_in',3600))
  c.headers.update({'Authorization':'Bearer '+self.token,'Accept':'application/json'});return c
 async def req(self,c,m,u,**kw):
  for a in range(6):
   r=await c.request(m,u,**kw);self.rate={k.lower():v for k,v in r.headers.items() if k.lower() in ('ratelimit-limit','ratelimit-remaining','ratelimit-reset','retry-after')}
   if r.status_code!=429:r.raise_for_status();return r
   w=float(r.headers.get('Retry-After') or r.headers.get('RateLimit-Reset') or min(2**a,30));self.notice(f'Rate limited by cnMaestro. Resuming after server reset ({w:g}s).');await asyncio.sleep(w+random.random())
  r.raise_for_status()
 async def authonly(self):c=await self.client();await c.aclose();return self.base
 async def inventory(self):
  c=await self.client();out=[];off=0
  try:
   while True:
    b=(await self.req(c,'GET',self.base+'/api/v2/devices',params={'offset':off,'limit':100})).json();page=b.get('data',[]);out+=page
    if not page or len(out)>=int(b.get('paging',{}).get('total',len(out))):break
    off+=len(page)
   return [d for d in out if str(d.get('type','')).lower()=='pmp' and str(d.get('mode','')).lower()=='sm']
  finally:await c.aclose()
 async def one(self,m):
  c=await self.client()
  try:return (await self.req(c,'GET',self.base+f'/api/v2/devices/{m}')).json()['data'][0]
  finally:await c.aclose()
 async def pull(self,m):
  c=await self.client();u=self.base+f'/api/v2/devices/{m}/pull_config'
  try:
   await self.req(c,'POST',u,json={});await asyncio.sleep(1.5)
   for _ in range(8):
    r=await self.req(c,'GET',u)
    try:return rates(r.text)
    except ValueError:await asyncio.sleep(1)
   raise ValueError('Pulled config never returned QoS')
  finally:await c.aclose()
 async def apply(self,m,t):
  c=await self.client()
  try:return (await self.req(c,'PUT',self.base+f'/api/v2/devices/{m}',json={'template':t})).json()
  finally:await c.aclose()
 async def job(self,j):
  c=await self.client()
  try:
   for _ in range(30):
    x=(await self.req(c,'GET',self.base+f'/api/v2/jobs/{j}')).json().get('data',[])
    if x and str(x[0].get('state','')).lower() in ('completed','failed','cancelled'):return x[0]
    await asyncio.sleep(2)
   return {'state':'Timed out'}
  finally:await c.aclose()
class App(tk.Tk):
 def __init__(self):
  super().__init__();initdb();self.title(f'cnMaestro Speed Manager {APP_VERSION}');self.geometry('1540x900');self.api=None;self.rows=cached();self.preview=[];self.checked=set();self.cancel=threading.Event();self.scanning=False;self.sort_col='name';self.sort_rev=False;self.job_status={};self.style=ttk.Style();self.ui();self.load_settings();self.render();self.after(1500,lambda:self.check_updates(True))
 def ui(self):
  self.style.theme_use('clam');f=ttk.LabelFrame(self,text='Connection',padding=7);f.pack(fill='x',padx=7,pady=5);self.auth=tk.StringVar(value='https://cloud.cambiumnetworks.com');self.cid=tk.StringVar();self.sec=tk.StringVar();self.status=tk.StringVar(value=f'{len(self.rows)} cached')
  for i,(n,v,sh) in enumerate([('Auth URL',self.auth,''),('Client ID',self.cid,''),('Client secret',self.sec,'*')]):ttk.Label(f,text=n).grid(row=0,column=i*2);ttk.Entry(f,textvariable=v,width=27,show=sh).grid(row=0,column=i*2+1,padx=4)
  ttk.Button(f,text='Connect',command=self.connect).grid(row=0,column=6);ttk.Label(f,textvariable=self.status).grid(row=0,column=7,padx=7);self.theme=tk.StringVar(value='System');ttk.Label(f,text='Appearance').grid(row=0,column=8);tb=ttk.Combobox(f,textvariable=self.theme,values=['System','Light','Dark'],state='readonly',width=9);tb.grid(row=0,column=9);tb.bind('<<ComboboxSelected>>',lambda e:self.apply_theme())
  g=ttk.Frame(self);g.pack(fill='x',padx=7);self.mac=tk.StringVar(value='0A:00:3E:80:42:EC');ttk.Label(g,text='Scan one MAC').pack(side='left');ttk.Entry(g,textvariable=self.mac,width=21).pack(side='left');self.one=ttk.Button(g,text='Scan one',command=self.scan_one);self.one.pack(side='left');self.all=ttk.Button(g,text='Scan all SMs',command=self.scan_all);self.all.pack(side='left',padx=4);self.cancelb=ttk.Button(g,text='Cancel scan',command=lambda:self.cancel.set(),state='disabled');self.cancelb.pack(side='left');self.force=tk.BooleanVar(value=False);ttk.Checkbutton(g,text='Force refresh cached',variable=self.force).pack(side='left',padx=4);self.days=tk.StringVar(value='10');ttk.Label(g,text='Include offline up to').pack(side='left');ttk.Entry(g,textvariable=self.days,width=4).pack(side='left');ttk.Label(g,text='days').pack(side='left');self.clearb=ttk.Button(g,text='Clear cache',command=self.clear_cache);self.clearb.pack(side='left',padx=6)
  a=ttk.Frame(self);a.pack(fill='x',padx=7,pady=3);self.approx=tk.BooleanVar(value=False);self.tolerance=tk.StringVar(value='10');ttk.Checkbutton(a,text='Group unmatched by closest downstream package',variable=self.approx,command=self.render).pack(side='left');ttk.Label(a,text='DL tolerance %').pack(side='left',padx=(8,2));ttk.Entry(a,textvariable=self.tolerance,width=5).pack(side='left');ttk.Button(a,text='Apply matching',command=self.render).pack(side='left',padx=4);ttk.Separator(a,orient='vertical').pack(side='left',fill='y',padx=8);ttk.Button(a,text='Select all visible',command=self.select_visible).pack(side='left');ttk.Button(a,text='Deselect visible',command=self.deselect_visible).pack(side='left',padx=3);ttk.Button(a,text='Clear all selections',command=self.clear_selection).pack(side='left');self.onlysel=tk.BooleanVar(value=False);ttk.Checkbutton(a,text='Show selected only',variable=self.onlysel,command=self.render).pack(side='left',padx=8);self.seltext=tk.StringVar(value='0 selected');ttk.Label(a,textvariable=self.seltext).pack(side='left')
  h=ttk.Frame(self);h.pack(fill='x',padx=7,pady=3);self.pf=tk.StringVar(value='All packages');self.nf=tk.StringVar(value='All networks');self.tf=tk.StringVar(value='All towers');self.af=tk.StringVar(value='All APs');self.sf=tk.StringVar(value='All status ages');self.q=tk.StringVar();spec=[('Package',self.pf,['All packages']+list(PKGS)+[OTHER],17),('Network',self.nf,['All networks'],16),('Tower',self.tf,['All towers'],18),('AP',self.af,['All APs'],17),('Status age',self.sf,['All status ages','Online','Offline - all','Offline < 24 hours','Offline 1-7 days','Offline 7-30 days','Offline 30-90 days','Offline 90+ days'],18)];self.box=[]
  for n,v,vals,w in spec:ttk.Label(h,text=n).pack(side='left');b=ttk.Combobox(h,textvariable=v,values=vals,state='readonly',width=w);b.pack(side='left',padx=2);b.bind('<<ComboboxSelected>>',lambda e:self.render());self.box.append(b)
  ttk.Label(h,text='Search').pack(side='left');ttk.Entry(h,textvariable=self.q,width=23).pack(side='left');self.q.trace_add('write',lambda *_:self.render())
  self.cols=('select','name','mac','package','match','variance','network','tower','ap','online','stateage','rates','cache','stage','issue');heads=('Select','Customer','MAC','Exact package','Display match','DL / UL variance','Network','Tower','AP MAC','Online','Status age','DL / UL','Cache age','Progress','Issue');self.heads=dict(zip(self.cols,heads));self.tree=ttk.Treeview(self,columns=self.cols,show='headings',selectmode='none')
  for c,n in zip(self.cols,heads):self.tree.heading(c,text=n,command=lambda c=c:self.sort(c));self.tree.column(c,width=65 if c=='select' else (180 if c in ('name','issue') else 115),anchor='center' if c=='select' else 'w')
  self.tree.bind('<Button-1>',self.tree_click);self.tree.pack(fill='both',expand=True,padx=7,pady=4)
  b=ttk.LabelFrame(self,text='Preview and publish',padding=7);b.pack(fill='x',padx=7,pady=5);self.target=tk.StringVar(value='50 Mbps');self.write=tk.BooleanVar(value=False);self.confirm=tk.StringVar();ttk.Label(b,text='Target').grid(row=0,column=0);ttk.Combobox(b,textvariable=self.target,values=list(PKGS),state='readonly',width=14).grid(row=0,column=1);ttk.Button(b,text='Preview checked',command=self.preview_rows).grid(row=0,column=2,padx=5);self.w=ttk.Checkbutton(b,text='Enable write actions',variable=self.write);self.w.grid(row=0,column=3);ttk.Label(b,text='Confirmation').grid(row=0,column=4);ttk.Entry(b,textvariable=self.confirm,width=22).grid(row=0,column=5);self.pub=ttk.Button(b,text='Publish and verify',command=self.execute);self.pub.grid(row=0,column=6,padx=5);ttk.Button(b,text='Audit log',command=self.audit).grid(row=0,column=7);ttk.Button(b,text='Check for updates',command=lambda:self.check_updates(False)).grid(row=0,column=8,padx=5)
  self.progress=ttk.Progressbar(b,mode='determinate');self.progress.grid(row=1,column=0,columnspan=6,sticky='ew',pady=5);self.progtext=tk.StringVar(value='No publish operation active.');ttk.Label(b,textvariable=self.progtext).grid(row=1,column=6,columnspan=3,sticky='w');self.out=tk.Text(b,height=7);self.out.grid(row=2,column=0,columnspan=9,sticky='ew',pady=5);b.columnconfigure(8,weight=1)
 def bg(self,coro,done):
  def run():
   try:r=asyncio.run(coro);self.after(0,lambda r=r:done(r,None))
   except Exception as e:self.after(0,lambda e=e:done(None,e))
  threading.Thread(target=run,daemon=True).start()
 def notice(self,t):self.after(0,lambda:self.status.set(t))
 def connect(self):self.api=API(self.auth.get(),self.cid.get(),self.sec.get(),self.notice);self.status.set('Connecting...');self.bg(self.api.authonly(),lambda r,e:self.status.set('Error: '+str(e) if e else 'Connected: '+r))
 def row(self,d,dl=None,ul=None,err=''):return {'name':d.get('name'),'mac':d.get('mac'),'package':exactpkg(dl,ul) if dl is not None else OTHER,'network':d.get('network'),'tower':d.get('tower'),'ap_mac':d.get('ap_mac'),'online':d.get('online'),'status_time':d.get('status_time'),'downlink':dl,'uplink':ul,'error':err,'cache_age_hours':0,'stale':False}
 def setbusy(self,x):self.scanning=x;st='disabled' if x else 'normal';self.one.config(state=st);self.all.config(state=st);self.clearb.config(state=st);self.w.config(state=st);self.pub.config(state=st);self.cancelb.config(state='normal' if x else 'disabled')
 def scan_one(self):
  if not self.api:return messagebox.showerror('Connect','Connect first')
  async def w():d=await self.api.one(self.mac.get());dl,ul=await self.api.pull(self.mac.get());return self.row(d,dl,ul)
  self.bg(w(),lambda r,e:self.finish_one(r,e))
 def finish_one(self,r,e):
  if e:self.status.set('Error: '+str(e));return
  self.rows[r['mac']]=r;save(r);self.status.set('1 scanned');self.render()
 def scan_all(self):
  if not self.api:return messagebox.showerror('Connect','Connect first')
  try:limit=float(self.days.get())*86400
  except:return messagebox.showerror('Invalid','Offline days must be numeric')
  self.cancel.clear();self.setbusy(True)
  async def w():
   allx=await self.api.inventory();inv=[d for d in allx if d.get('online') or int(d.get('status_time') or 0)<=limit];excluded=len(allx)-len(inv);total=len(inv);sem=asyncio.Semaphore(CONCURRENCY);done=0
   async def one(d):
    nonlocal done
    if self.cancel.is_set():return
    old=self.rows.get(d['mac'])
    if old and not old.get('stale',True) and not self.force.get():r=old
    elif not d.get('online'):r={**old,'online':False,'status_time':d.get('status_time'),'error':'Offline - using cached package'} if old and old.get('downlink') is not None else self.row(d,err='Offline - package unknown')
    else:
     async with sem:
      try:dl,ul=await self.api.pull(d['mac']);r=self.row(d,dl,ul);save(r)
      except Exception as e:r=self.row(d,err=str(e))
    done+=1;self.after(0,lambda r=r,n=done:self.stream(r,n,total))
   await asyncio.gather(*(one(d) for d in inv));return done,total,excluded,self.cancel.is_set(),self.api.rate
  self.bg(w(),lambda r,e:self.finish_all(r,e))
 def stream(self,r,n,total):self.rows[r['mac']]=r;rem=self.api.rate.get('ratelimit-remaining');self.status.set(f'Scanning {n} of {total} | {CONCURRENCY} concurrent'+(f' | rate remaining {rem}' if rem else ''));self.render()
 def finish_all(self,r,e):self.setbusy(False);self.status.set('Error: '+str(e) if e else f'{"Cancelled" if r[3] else "Completed"}: {r[0]} of {r[1]} included | {r[2]} older offline excluded | Rate {r[4]}')
 def clear_cache(self):
  if not messagebox.askyesno('Clear cache','Clear cached inventory and classifications? Audit history is preserved.'):return
  with sqlite3.connect(DB) as c:c.execute('DELETE FROM cache')
  self.rows={};self.preview=[];self.checked.clear();self.render();self.status.set('Cache cleared')
 def suggestion(self,r):
  if r.get('package')!=OTHER:return None
  try:tol=float(self.tolerance.get())
  except:tol=10
  return nearest(r.get('downlink'),r.get('uplink'),tol) if self.approx.get() else None
 def displaypkg(self,r):
  s=self.suggestion(r);return s['name']+' (Approx.)' if s else r.get('package')
 def filt(self):
  def state(r):
   st=self.sf.get();on=bool(r.get('online'));d=int(r.get('status_time') or 0)/86400
   return st=='All status ages' or (st=='Online' and on) or (not on and (st=='Offline - all' or st=='Offline < 24 hours' and d<1 or st=='Offline 1-7 days' and 1<=d<7 or st=='Offline 7-30 days' and 7<=d<30 or st=='Offline 30-90 days' and 30<=d<90 or st=='Offline 90+ days' and d>=90))
  q=self.q.get().lower();p=self.pf.get()
  return [r for r in self.rows.values() if (p=='All packages' or r['package']==p or self.displaypkg(r).startswith(p)) and (self.nf.get()=='All networks' or r.get('network')==self.nf.get()) and (self.tf.get()=='All towers' or r.get('tower')==self.tf.get()) and (self.af.get()=='All APs' or r.get('ap_mac')==self.af.get()) and state(r) and (not self.onlysel.get() or r['mac'] in self.checked) and (not q or q in ' '.join(str(r.get(k,'')) for k in ('name','mac','package','network','tower','ap_mac','error')).lower())]
 def sortkey(self,r):
  c=self.sort_col;maps={'select':int(r['mac'] in self.checked),'name':r.get('name'),'mac':r.get('mac'),'package':r.get('package'),'match':self.displaypkg(r),'variance':abs((self.suggestion(r) or {}).get('dl_pct',9999)),'network':r.get('network'),'tower':r.get('tower'),'ap':r.get('ap_mac'),'online':int(bool(r.get('online'))),'stateage':r.get('status_time'),'rates':r.get('downlink'),'cache':r.get('cache_age_hours'),'stage':self.job_status.get(r['mac'],''),'issue':r.get('error')}
  v=maps.get(c,'');return (v is None, v.lower() if isinstance(v,str) else v)
 def sort(self,c):self.sort_rev=not self.sort_rev if self.sort_col==c else False;self.sort_col=c;self.render()
 def render(self):
  if not hasattr(self,'tree'):return
  vals=list(self.rows.values());sets=[['All networks']+sorted({r.get('network') for r in vals if r.get('network')}),['All towers']+sorted({r.get('tower') for r in vals if r.get('tower')}),['All APs']+sorted({r.get('ap_mac') for r in vals if r.get('ap_mac')})]
  for b,v in zip(self.box[1:4],sets):b['values']=v
  self.tree.delete(*self.tree.get_children());rows=sorted(self.filt(),key=self.sortkey,reverse=self.sort_rev)
  for c,h in self.heads.items():self.tree.heading(c,text=h+(' ▼' if c==self.sort_col and self.sort_rev else ' ▲' if c==self.sort_col else ''))
  for r in rows:
   m=r['mac'];s=self.suggestion(r);var=f"DL {s['dl_pct']:+.1f}% / UL {s['ul_pct']:+.1f}%" if s else '';self.tree.insert('', 'end',iid=m,values=('☑' if m in self.checked else '☐',r['name'],m,r['package'],self.displaypkg(r),var,r['network'],r['tower'],r['ap_mac'],r['online'],('Online for ' if r.get('online') else 'Offline for ')+age(r.get('status_time')),f"{r['downlink']} / {r['uplink']}",str(r.get('cache_age_hours',0))+'h',self.job_status.get(m,''),r['error']))
  self.seltext.set(f'{len(self.checked)} selected')
 def tree_click(self,e):
  if self.tree.identify_region(e.x,e.y)!='cell':return
  if self.tree.identify_column(e.x)!='#1':return
  m=self.tree.identify_row(e.y)
  if m:(self.checked.remove(m) if m in self.checked else self.checked.add(m));self.render()
 def select_visible(self):self.checked.update(r['mac'] for r in self.filt());self.render()
 def deselect_visible(self):self.checked.difference_update(r['mac'] for r in self.filt());self.render()
 def clear_selection(self):self.checked.clear();self.render()
 def show(self,x):self.out.delete('1.0','end');self.out.insert('1.0',json.dumps(x,indent=2,default=str))
 def preview_rows(self):
  t=self.target.get();out=[]
  for m in sorted(self.checked):
   r=self.rows.get(m)
   if not r:continue
   if not r.get('online'):return messagebox.showerror('Unavailable',r['name']+' is offline')
   if r['package']==t:return messagebox.showerror('Invalid',r['name']+' is already exactly on '+t)
   _,dl,ul=PKGS[t];out.append({**r,'display_match':self.displaypkg(r),'target_package':t,'target_downlink':dl,'target_uplink':ul})
  self.preview=out;self.show({'count':len(out),'changes':out})
 def publish_status(self,idx,total,m,stage,ok,fail):
  self.job_status[m]=stage;self.progress['maximum']=total;self.progress['value']=idx-1;self.progtext.set(f'{idx} of {total} | {stage} | Success {ok} | Failed {fail}');self.render()
 def execute(self):
  if not self.write.get():return messagebox.showerror('Blocked','Enable write actions')
  if self.confirm.get()!=PHRASE:return messagebox.showerror('Blocked','Enter: '+PHRASE)
  if not self.preview:return messagebox.showerror('Blocked','Create a preview')
  if not messagebox.askyesno('Confirm',f'Apply {self.target.get()} to {len(self.preview)} SM(s)?'):return
  self.setbusy(True);self.progress['value']=0
  async def w():
   out=[];t=self.target.get();tpl,_,_=PKGS[t];total=len(self.preview);ok=fail=0
   for idx,r in enumerate(self.preview,1):
    x={'mac':r['mac'],'name':r['name'],'old_package':r['package'],'target_package':t,'template':tpl,'job_id':None,'job_state':None,'verified_package':None,'success':False,'detail':''}
    try:
     self.after(0,lambda idx=idx,m=r['mac'],ok=ok,fail=fail:self.publish_status(idx,total,m,'Revalidating live package',ok,fail));dl,ul=await self.api.pull(r['mac']);live=exactpkg(dl,ul)
     if live!=r['package']:raise ValueError('Live package changed to '+live)
     self.after(0,lambda idx=idx,m=r['mac'],ok=ok,fail=fail:self.publish_status(idx,total,m,'Applying template',ok,fail));st=await self.api.apply(r['mac'],tpl);x['job_id']=st.get('job_id')
     self.after(0,lambda idx=idx,m=r['mac'],ok=ok,fail=fail:self.publish_status(idx,total,m,'Waiting for cnMaestro job',ok,fail));j=await self.api.job(x['job_id']);x['job_state']=j.get('state')
     if str(x['job_state']).lower()!='completed' or int(j.get('devices',{}).get('failed',0)):raise ValueError('Job failed')
     self.after(0,lambda idx=idx,m=r['mac'],ok=ok,fail=fail:self.publish_status(idx,total,m,'Verifying live QoS',ok,fail));dl,ul=await self.api.pull(r['mac']);x['verified_package']=exactpkg(dl,ul)
     if x['verified_package']!=t:raise ValueError('Verification mismatch')
     x['success']=True;ok+=1;self.rows[r['mac']].update(package=t,downlink=dl,uplink=ul);save(self.rows[r['mac']]);self.job_status[r['mac']]='Completed'
    except Exception as e:x['detail']=str(e);fail+=1;self.job_status[r['mac']]='Failed'
    with sqlite3.connect(DB) as c:c.execute('INSERT INTO audit(timestamp,mac,name,old_package,target_package,template,job_id,job_state,verified_package,success,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),x['mac'],x['name'],x['old_package'],x['target_package'],x['template'],x['job_id'],x['job_state'],x['verified_package'],int(x['success']),x['detail']))
    out.append(x);self.after(0,lambda i=idx,ok=ok,fail=fail:self.progress_done(i,total,ok,fail))
   return out,ok,fail
  self.bg(w(),self.finish_publish)
 def progress_done(self,i,total,ok,fail):self.progress['maximum']=total;self.progress['value']=i;self.progtext.set(f'{i} of {total} completed | Success {ok} | Failed {fail}');self.render()
 def finish_publish(self,r,e):
  self.setbusy(False)
  if e:self.show(str(e));messagebox.showerror('Publish error',str(e));return
  rows,ok,fail=r;self.show(rows);self.progtext.set(f'Completed | Success {ok} | Failed {fail}');messagebox.showinfo('Batch completed' if not fail else 'Batch completed with issues',f'{len(rows)} processed\n{ok} successful\n{fail} failed'+('\n\nReview the audit log for details.' if fail else ''))
 def audit(self):
  w=tk.Toplevel(self);w.title('Audit log');w.geometry('1180x520');cols=('time','name','mac','old','target','job','state','verified','success','detail');heads=('Timestamp UTC','Customer','MAC','Old','Target','Job ID','Job state','Verified','Success','Detail');tree=ttk.Treeview(w,columns=cols,show='headings')
  for c,h in zip(cols,heads):tree.heading(c,text=h);tree.column(c,width=115)
  y=ttk.Scrollbar(w,orient='vertical',command=tree.yview);x=ttk.Scrollbar(w,orient='horizontal',command=tree.xview);tree.configure(yscrollcommand=y.set,xscrollcommand=x.set);tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew');w.rowconfigure(0,weight=1);w.columnconfigure(0,weight=1)
  with sqlite3.connect(DB) as c:c.row_factory=sqlite3.Row;rows=[dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 1000')]
  for r in rows:tree.insert('', 'end',values=(r['timestamp'],r['name'],r['mac'],r['old_package'],r['target_package'],r['job_id'],r['job_state'],r['verified_package'],'Yes' if r['success'] else 'No',r['detail']))
  ttk.Label(w,text=f'{len(rows)} records | {DB}').grid(row=2,column=0,sticky='w',padx=5,pady=5)
 def apply_theme(self):
  mode=self.theme.get();dark=mode=='Dark';bg='#1e1e1e' if dark else '#f3f3f3';fg='#f0f0f0' if dark else '#111111';field='#2b2b2b' if dark else '#ffffff';accent='#3a3a3a' if dark else '#e7e7e7';self.configure(bg=bg);self.style.theme_use('clam');self.style.configure('.',background=bg,foreground=fg,fieldbackground=field);self.style.configure('Treeview',background=field,foreground=fg,fieldbackground=field,rowheight=24);self.style.configure('Treeview.Heading',background=accent,foreground=fg);self.style.map('Treeview',background=[('selected','#225a8f' if dark else '#cce5ff')],foreground=[('selected','#ffffff' if dark else '#000000')]);self.style.configure('TEntry',fieldbackground=field);self.style.configure('TCombobox',fieldbackground=field);self.out.configure(bg=field,fg=fg,insertbackground=fg);SETTINGS.write_text(json.dumps({'theme':mode}));self.render()
 def load_settings(self):
  try:self.theme.set(json.loads(SETTINGS.read_text()).get('theme','System'))
  except:pass
  self.apply_theme()
 def vt(self,v):return tuple(int(x) for x in re.findall(r'\d+',str(v))[:4])
 def check_updates(self,auto=False):
  if not UPDATE_CONFIG.exists():
   if not auto:messagebox.showinfo('Updates not configured',f'Create update_config.json beside the app.\n{UPDATE_CONFIG}')
   return
  try:url=json.loads(UPDATE_CONFIG.read_text())['manifest_url'];url+=('&' if '?' in url else '?')+'t='+str(int(time.time()))
  except Exception as e:return None if auto else messagebox.showerror('Update config error',str(e))
  def work():
   try:
    with httpx.Client(timeout=20,verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),follow_redirects=True) as c:m=c.get(url);m.raise_for_status();b=m.json()
    self.after(0,lambda:self.update_result(b,auto))
   except Exception as e:
    if not auto:self.after(0,lambda:messagebox.showerror('Update check failed',str(e)))
  threading.Thread(target=work,daemon=True).start()
 def update_result(self,m,auto):
  v=str(m.get('version','0'))
  if self.vt(v)>self.vt(APP_VERSION):
   if messagebox.askyesno('Update available',f'Version {v} is available.\n\n{m.get("notes","")}\n\nOpen the approved download location?'):webbrowser.open(str(m.get('download_url','')))
  elif not auto:messagebox.showinfo('No update available',f'Version {APP_VERSION} is current.')
if __name__=='__main__':App().mainloop()
