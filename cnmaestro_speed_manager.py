import asyncio,csv,json,os,re,sqlite3,threading,time,ssl,random,sys,webbrowser
from datetime import datetime,timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk,messagebox,filedialog
import httpx,truststore
APP_VERSION='1.3.0';APP_DIR=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent;UPDATE_CONFIG=APP_DIR/'update_config.json';DEFAULT_MANIFEST_URL='https://raw.githubusercontent.com/dogekamii/Operations-Toolkit/refs/heads/main/latest.json'
PKGS={"6 Mbps":("6mbps Package",6451,2150),"10 Mbps":("10mbps Package",10752,1075),"15 Mbps":("15mbps Package",16128,3225),"20 Mbps":("20mbps Package",21500,10752),"25 Mbps":("25mbps Package",26880,3225),"50 Mbps":("50mbps Package",53760,10750),"75 Mbps":("75mbps Package",80640,10750),"100 Mbps":("100mbps Package",107520,21500)}
OTHER='Other / Unmatched';PHRASE='APPLY SPEED CHANGES';CONCURRENCY=4;CACHE_HOURS=24
DATA=Path(os.getenv('LOCALAPPDATA',Path.home()))/'cnMaestroSpeedManager';DATA.mkdir(exist_ok=True);DB=DATA/'speed_manager.db';SETTINGS=DATA/'settings.json'
def resolve_manifest_url(config_path=UPDATE_CONFIG):
 if config_path.exists():return json.loads(config_path.read_text(encoding='utf-8'))['manifest_url']
 return DEFAULT_MANIFEST_URL
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
  super().__init__();initdb();self.title('Operations Toolkit');self.geometry('1280x720');self.minsize(1120,680);self.api=None;self.rows=cached();self.preview=[];self.checked=set();self.cancel=threading.Event();self.scanning=False;self.sort_col='name';self.sort_rev=False;self.job_status={};self.style=ttk.Style();self.ui()
  if os.getenv('OPERATIONS_TOOLKIT_PREVIEW'):self.configure_styles()
  else:self.load_settings();self.configure_styles() if self.theme.get()=='Dark' else None
  self.render();self.show_page('speed_manager')
  if os.getenv('OPERATIONS_TOOLKIT_SMOKE_NAV'):self.after(1000,self.smoke_inline_views)
  if not os.getenv('OPERATIONS_TOOLKIT_PREVIEW'):self.after(1500,lambda:self.check_updates(True))
 def configure_styles(self):
  self.colors={'bg':'#071625','sidebar':'#0a1827','surface':'#142333','surface2':'#192a3c','border':'#34485a','text':'#f5f7fa','muted':'#b5c0cf','accent':'#42d6ef','accent2':'#42d6ef','accent_dark':'#0c7085','success':'#49d26f','warning':'#e2b900','disabled':'#697788','danger':'#ef6b73'};c=self.colors;self.configure(bg=c['bg']);self.style.theme_use('clam')
  self.style.configure('.',font=('Segoe UI',8),background=c['bg'],foreground=c['text'])
  self.style.configure('TFrame',background=c['bg']);self.style.configure('Surface.TFrame',background=c['surface']);self.style.configure('TLabel',background=c['bg'],foreground=c['text']);self.style.configure('Surface.TLabel',background=c['surface'],foreground=c['text']);self.style.configure('Muted.TLabel',background=c['bg'],foreground=c['muted']);self.style.configure('SurfaceMuted.TLabel',background=c['surface'],foreground=c['muted'])
  self.style.configure('Title.TLabel',font=('Segoe UI Semibold',18),background=c['bg'],foreground=c['text']);self.style.configure('PageTitle.TLabel',font=('Segoe UI Semibold',14),background=c['bg'],foreground=c['text']);self.style.configure('CardTitle.TLabel',font=('Segoe UI Semibold',10),background=c['surface'],foreground=c['text']);self.style.configure('Metric.TLabel',font=('Segoe UI Semibold',16),background=c['surface'],foreground=c['text'])
  self.style.configure('TButton',font=('Segoe UI Semibold',8),padding=(10,5),background=c['surface2'],foreground=c['text'],borderwidth=1,relief='flat');self.style.map('TButton',background=[('active',c['border']),('disabled',c['surface'])],foreground=[('disabled',c['disabled'])])
  self.style.configure('Accent.TButton',background=c['accent'],foreground='#04131a',padding=(11,6));self.style.map('Accent.TButton',background=[('active','#78e5f7'),('pressed',c['accent_dark'])])
  self.style.configure('TEntry',font=('Segoe UI',8),fieldbackground=c['surface2'],foreground=c['text'],insertcolor=c['text'],bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'],padding=4)
  self.style.configure('TCombobox',font=('Segoe UI',8),fieldbackground=c['surface2'],background=c['surface2'],foreground=c['text'],arrowcolor=c['accent'],bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'],padding=3);self.style.map('TCombobox',fieldbackground=[('readonly',c['surface2'])],foreground=[('readonly',c['text'])])
  self.style.configure('TCheckbutton',background=c['surface'],foreground=c['text']);self.style.map('TCheckbutton',background=[('active',c['surface'])],foreground=[('active',c['text'])])
  self.style.configure('Treeview',font=('Segoe UI',8),background=c['surface'],foreground=c['text'],fieldbackground=c['surface'],bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'],rowheight=23);self.style.map('Treeview',background=[('selected',c['accent_dark'])],foreground=[('selected','#ffffff')])
  self.style.configure('Treeview.Heading',font=('Segoe UI Semibold',8),background=c['surface2'],foreground=c['muted'],bordercolor=c['border'],relief='flat',padding=(5,5));self.style.map('Treeview.Heading',background=[('active',c['border'])])
  self.style.configure('Horizontal.TProgressbar',background=c['accent'],troughcolor=c['surface2'],bordercolor=c['surface2'],lightcolor=c['accent'],darkcolor=c['accent']);self.style.configure('TScrollbar',background=c['surface2'],troughcolor=c['surface'],bordercolor=c['border'],arrowcolor=c['muted'],lightcolor=c['surface2'],darkcolor=c['surface2'])
  self.option_add('*TCombobox*Listbox.background',c['surface2']);self.option_add('*TCombobox*Listbox.foreground',c['text']);self.option_add('*TCombobox*Listbox.selectBackground',c['accent_dark'])
 def card(self,parent,padx=10,pady=7):
  outer=tk.Frame(parent,bg=self.colors['surface'],highlightbackground=self.colors['border'],highlightcolor=self.colors['border'],highlightthickness=1);body=ttk.Frame(outer,style='Surface.TFrame',padding=(padx,pady));body.pack(fill='both',expand=True);return outer,body
 def ui(self):
  self.configure_styles();c=self.colors;self.shell=tk.Frame(self,bg=c['bg']);self.shell.pack(fill='both',expand=True)
  self.sidebar=tk.Frame(self.shell,bg=c['sidebar'],width=160,highlightbackground=c['border'],highlightthickness=1);self.sidebar.pack(side='left',fill='y');self.sidebar.pack_propagate(False)
  self.nav_items={};self.service_navigation=[{'key':'cnmaestro','label':'cnMaestro','icon':'☁','tools':[{'key':'speed_manager','label':'Speed Manager'}]}]
  self.nav_container=tk.Frame(self.sidebar,bg=c['sidebar']);self.nav_container.pack(fill='x',pady=(15,0));self.add_nav_row('overview','Overview','⌂')
  for service in self.service_navigation:self.add_service_group(service)
  self.add_nav_row('audit','Audit Log','▣');self.add_nav_row('settings','Settings','⚙')
  tk.Label(self.sidebar,text=f'v{APP_VERSION}',font=('Segoe UI',7),fg='#596878',bg=c['sidebar']).pack(side='bottom',anchor='w',padx=14,pady=12)
  self.content=tk.Frame(self.shell,bg=c['bg']);self.content.pack(side='left',fill='both',expand=True);self.content.rowconfigure(0,weight=1);self.content.columnconfigure(0,weight=1)
  self.pages={}
  for key in ('overview','speed_manager','audit','settings'):
   page=ttk.Frame(self.content,padding=(15,12));page.grid(row=0,column=0,sticky='nsew');self.pages[key]=page
  self.auth=tk.StringVar(value='https://cloud.cambiumnetworks.com');self.cid=tk.StringVar();self.sec=tk.StringVar();self.status=tk.StringVar(value='Disconnected');self.theme=tk.StringVar(value='Dark')
  self.build_speed_manager();self.build_overview();self.build_audit_page();self.build_settings()
 def add_nav_row(self,key,label,icon,parent=None,indent=0):
  c=self.colors;holder=parent or self.nav_container;row=tk.Frame(holder,bg=c['sidebar'],height=35,cursor='hand2');row.pack(fill='x',padx=(5+indent,7),pady=1);row.pack_propagate(False)
  bar=tk.Frame(row,bg=c['sidebar'],width=3);bar.pack(side='left',fill='y');ico=tk.Label(row,text=icon,font=('Segoe UI Symbol',11),width=2,fg=c['muted'],bg=c['sidebar'],cursor='hand2');ico.pack(side='left',padx=(5,3));text=tk.Label(row,text=label,font=('Segoe UI Semibold',8),anchor='w',fg=c['muted'],bg=c['sidebar'],cursor='hand2');text.pack(side='left',fill='both',expand=True)
  for w in (row,bar,ico,text):w.bind('<Button-1>',lambda e,k=key:self.show_page(k))
  self.nav_items[key]=(row,bar,ico,text);return row
 def add_service_group(self,service):
  c=self.colors;row=tk.Frame(self.nav_container,bg=c['sidebar'],height=35,cursor='hand2');row.pack(fill='x',padx=(5,7),pady=1);row.pack_propagate(False);bar=tk.Frame(row,bg=c['sidebar'],width=3);bar.pack(side='left',fill='y');ico=tk.Label(row,text=service['icon'],font=('Segoe UI Symbol',11),width=2,fg=c['accent2'],bg=c['sidebar'],cursor='hand2');ico.pack(side='left',padx=(5,3));text=tk.Label(row,text=service['label'],font=('Segoe UI Semibold',8),anchor='w',fg=c['accent2'],bg=c['sidebar'],cursor='hand2');text.pack(side='left',fill='both',expand=True);chev=tk.Label(row,text='⌄',font=('Segoe UI',9),fg=c['accent2'],bg=c['sidebar'],cursor='hand2');chev.pack(side='right',padx=7)
  child=tk.Frame(self.nav_container,bg=c['sidebar']);child.pack(fill='x');service['row']=row;service['widgets']=(bar,ico,text,chev);service['child_frame']=child;service['expanded']=True
  for w in (row,bar,ico,text,chev):w.bind('<Button-1>',lambda e,s=service:self.toggle_service(s))
  for tool in service['tools']:self.add_nav_row(tool['key'],tool['label'],'',child,indent=22)
 def toggle_service(self,service):
  service['expanded']=not service['expanded'];service['widgets'][3].configure(text='⌄' if service['expanded'] else '›')
  if service['expanded']:service['child_frame'].pack(fill='x',after=service['row'])
  else:service['child_frame'].pack_forget()
 def show_page(self,key):
  if key not in self.pages:return
  if key=='speed_manager':
   service=self.service_navigation[0]
   if not service['expanded']:self.toggle_service(service)
  self.pages[key].tkraise();self.active_page=key;self.active_tool_view=key;self.refresh_nav()
  if key=='audit':self.refresh_audit()
 def refresh_nav(self):
  c=self.colors
  for key,(row,bar,ico,text) in self.nav_items.items():
   active=key==self.active_page;bg='#123455' if active else c['sidebar'];row.configure(bg=bg);bar.configure(bg=c['accent'] if active else bg);ico.configure(bg=bg,fg=c['accent2'] if active else c['muted']);text.configure(bg=bg,fg=c['accent2'] if active else c['muted'])
 def build_speed_manager(self):
  p=self.pages['speed_manager'];p.configure(padding=(10,8));p.columnconfigure(0,weight=1);p.rowconfigure(4,weight=1)
  hdr=ttk.Frame(p);hdr.grid(row=0,column=0,sticky='ew',pady=(0,6));hdr.columnconfigure(0,weight=1);ttk.Label(hdr,text='cnMaestro Speed Manager',style='PageTitle.TLabel').grid(row=0,column=0,sticky='w');ttk.Label(hdr,text='Bulk package operations',style='Muted.TLabel').grid(row=1,column=0,sticky='w');tk.Label(hdr,text='  Available  ',font=('Segoe UI Semibold',8),fg='#092019',bg=self.colors['success'],padx=4,pady=2).grid(row=0,column=1,rowspan=2,sticky='e')
  o,f=self.card(p,10,6);o.grid(row=1,column=0,sticky='ew',pady=(0,5));f.columnconfigure(0,weight=3,minsize=240);f.columnconfigure(1,weight=2,minsize=160);f.columnconfigure(2,weight=2,minsize=160)
  for col,(label,var,show) in enumerate([('Auth URL',self.auth,''),('Client ID',self.cid,''),('Client secret',self.sec,'*')]):ttk.Label(f,text=label,style='SurfaceMuted.TLabel').grid(row=0,column=col,sticky='w',padx=(0,7));ttk.Entry(f,textvariable=var,show=show).grid(row=1,column=col,sticky='ew',padx=(0,7))
  ttk.Button(f,text='Connect',style='Accent.TButton',command=self.connect).grid(row=1,column=3,sticky='e');ttk.Label(f,textvariable=self.status,style='SurfaceMuted.TLabel').grid(row=2,column=0,columnspan=4,sticky='w',pady=(4,0))
  self.controls=ttk.Frame(p);self.controls.grid(row=2,column=0,sticky='ew');self.controls.columnconfigure(0,weight=1)
  o,a=self.card(self.controls,10,6);o.grid(row=0,column=0,sticky='ew',pady=(0,5));self.mac=tk.StringVar(value='0A:00:3E:80:42:EC');self.force=tk.BooleanVar(value=False);self.days=tk.StringVar(value='10');self.approx=tk.BooleanVar(value=False);self.tolerance=tk.StringVar(value='10');self.onlysel=tk.BooleanVar(value=False);self.seltext=tk.StringVar(value='0 selected')
  ttk.Label(a,text='Scan one MAC',style='SurfaceMuted.TLabel').grid(row=0,column=0,sticky='w');ttk.Entry(a,textvariable=self.mac,width=20).grid(row=0,column=1,padx=(5,6));self.one=ttk.Button(a,text='Scan one',command=self.scan_one);self.one.grid(row=0,column=2);self.all=ttk.Button(a,text='Scan all SMs',command=self.scan_all);self.all.grid(row=0,column=3,padx=5);self.cancelb=ttk.Button(a,text='Cancel',command=lambda:self.cancel.set(),state='disabled');self.cancelb.grid(row=0,column=4);self.clearb=ttk.Button(a,text='Clear cache',command=self.clear_cache);self.clearb.grid(row=0,column=5,padx=(5,0))
  ttk.Checkbutton(a,text='Force refresh cached',variable=self.force).grid(row=1,column=0,columnspan=2,sticky='w',pady=(5,0));ttk.Label(a,text='Include offline up to',style='SurfaceMuted.TLabel').grid(row=1,column=2,sticky='e',pady=(5,0));ttk.Entry(a,textvariable=self.days,width=5).grid(row=1,column=3,sticky='w',pady=(5,0));ttk.Label(a,text='days',style='SurfaceMuted.TLabel').grid(row=1,column=3,padx=(45,0),sticky='w',pady=(5,0));ttk.Checkbutton(a,text='Closest package',variable=self.approx,command=self.render).grid(row=1,column=4,sticky='w',pady=(5,0));ttk.Entry(a,textvariable=self.tolerance,width=5).grid(row=1,column=5,sticky='w',pady=(5,0));ttk.Label(a,text='% tolerance',style='SurfaceMuted.TLabel').grid(row=1,column=5,padx=(40,0),sticky='w',pady=(5,0))
  o,h=self.card(self.controls,10,6);o.grid(row=1,column=0,sticky='ew',pady=(0,5));self.pf=tk.StringVar(value='All packages');self.nf=tk.StringVar(value='All networks');self.tf=tk.StringVar(value='All towers');self.af=tk.StringVar(value='All APs');self.sf=tk.StringVar(value='All status ages');self.q=tk.StringVar();spec=[('Package',self.pf,['All packages']+list(PKGS)+[OTHER],13),('Network',self.nf,['All networks'],13),('Tower',self.tf,['All towers'],13),('AP',self.af,['All APs'],13),('Status age',self.sf,['All status ages','Online','Offline - all','Offline < 24 hours','Offline 1-7 days','Offline 7-30 days','Offline 30-90 days','Offline 90+ days'],15),('Search',self.q,None,15)];self.box=[]
  for i,(n,v,vals,w) in enumerate(spec):
   ttk.Label(h,text=n,style='SurfaceMuted.TLabel').grid(row=0,column=i,sticky='w',padx=(0,4));widget=ttk.Combobox(h,textvariable=v,values=vals,state='readonly',width=w) if vals else ttk.Entry(h,textvariable=v,width=w);widget.grid(row=1,column=i,sticky='ew',padx=(0,5));h.columnconfigure(i,weight=1)
   if vals:widget.bind('<<ComboboxSelected>>',lambda e:self.render());self.box.append(widget)
  self.q.trace_add('write',lambda *_:self.render());choices=ttk.Frame(h,style='Surface.TFrame');choices.grid(row=2,column=0,columnspan=6,sticky='ew',pady=(5,0));ttk.Button(choices,text='Select visible',command=self.select_visible).pack(side='left');ttk.Button(choices,text='Deselect visible',command=self.deselect_visible).pack(side='left',padx=4);ttk.Button(choices,text='Clear selection',command=self.clear_selection).pack(side='left');ttk.Checkbutton(choices,text='Show selected only',variable=self.onlysel,command=self.render).pack(side='left',padx=(10,5));ttk.Label(choices,textvariable=self.seltext,style='SurfaceMuted.TLabel').pack(side='left')
  table_o,table=self.card(p,0,0);table_o.grid(row=4,column=0,sticky='nsew',pady=(0,5));table.columnconfigure(0,weight=1);table.rowconfigure(0,weight=1);self.cols=('select','name','mac','package','match','variance','network','tower','ap','online','stateage','rates','cache','stage','issue');heads=('Select','Customer','MAC','Exact package','Display match','DL / UL variance','Network','Tower','AP MAC','Online','Status age','DL / UL','Cache age','Progress','Issue');self.heads=dict(zip(self.cols,heads));self.tree=ttk.Treeview(table,columns=self.cols,show='headings',selectmode='none',height=4)
  for col,n in zip(self.cols,heads):self.tree.heading(col,text=n,command=lambda c=col:self.sort(c));self.tree.column(col,width=60 if col=='select' else (170 if col in ('name','issue') else 105),anchor='center' if col=='select' else 'w',stretch=False)
  self.tree.bind('<Button-1>',self.tree_click);y=ttk.Scrollbar(table,orient='vertical',command=self.tree.yview);x=ttk.Scrollbar(table,orient='horizontal',command=self.tree.xview);self.tree.configure(yscrollcommand=y.set,xscrollcommand=x.set);self.tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew')
  o,b=self.card(p,10,6);o.grid(row=5,column=0,sticky='ew');self.preview_window=o;self.target=tk.StringVar(value='50 Mbps');self.write=tk.BooleanVar(value=False);self.confirm=tk.StringVar();ttk.Label(b,text='Publish and verify',style='CardTitle.TLabel').grid(row=0,column=0,columnspan=6,sticky='w',pady=(0,4));ttk.Label(b,text='Target',style='SurfaceMuted.TLabel').grid(row=1,column=0,sticky='w');ttk.Combobox(b,textvariable=self.target,values=list(PKGS),state='readonly',width=12).grid(row=1,column=1,sticky='w');ttk.Button(b,text='Preview Changes',command=self.preview_changes).grid(row=1,column=2,padx=5,sticky='w');self.w=ttk.Checkbutton(b,text='Enable write actions',variable=self.write);self.w.grid(row=1,column=3,sticky='w');ttk.Label(b,text='Confirmation',style='SurfaceMuted.TLabel').grid(row=1,column=4,padx=(14,3),sticky='e');ttk.Entry(b,textvariable=self.confirm,width=22).grid(row=1,column=5,sticky='ew');b.columnconfigure(5,weight=1)
  self.pub=ttk.Button(b,text='Publish and verify',style='Accent.TButton',command=self.execute);self.pub.grid(row=2,column=0,sticky='w',pady=(5,0));ttk.Button(b,text='Audit Log',command=self.audit).grid(row=2,column=1,sticky='w',padx=5,pady=(5,0));ttk.Button(b,text='Check updates',command=lambda:self.check_updates(False)).grid(row=2,column=2,sticky='w',pady=(5,0));self.progress=ttk.Progressbar(b,mode='determinate');self.progress.grid(row=2,column=3,columnspan=2,sticky='ew',padx=(14,0),pady=(5,0));self.progtext=tk.StringVar(value='No publish operation active.');ttk.Label(b,textvariable=self.progtext,style='SurfaceMuted.TLabel').grid(row=2,column=5,sticky='e',padx=(7,0),pady=(5,0));self.out=tk.Text(b,height=2,width=1,bg=self.colors['surface2'],fg=self.colors['text'],insertbackground=self.colors['text'],relief='flat',highlightbackground=self.colors['border'],highlightthickness=1,font=('Consolas',8),wrap='none');self.out.grid(row=3,column=0,columnspan=6,sticky='ew',pady=(5,0))
 def build_control_window(self):self.controls=self.pages['speed_manager']
 def open_controls(self):self.show_page('speed_manager')
 def back_to_speed_manager(self):self.show_page('speed_manager')
 def show_preview_view(self):self.show_page('speed_manager')
 def smoke_inline_views(self):self.open_controls();self.show_preview_view();self.back_to_speed_manager();self.title('Operations Toolkit - inline views smoke complete')
 def build_preview_window(self):self.preview_window=self.pages['speed_manager']
 def preview_changes(self):
  self.preview_rows()
  if self.preview:self.show_preview_view()
 def update_metrics(self):
  if not hasattr(self,'metric_vars'):return
  vals=list(self.rows.values());self.metric_vars['devices'].set(str(len(vals)));self.metric_vars['selected'].set(str(len(self.checked)));self.metric_vars['online'].set(str(sum(bool(r.get('online')) for r in vals)));self.metric_vars['unmatched'].set(str(sum(r.get('package')==OTHER for r in vals)))
 def build_overview(self):
  p=self.pages['overview'];ttk.Label(p,text='Overview',style='PageTitle.TLabel').pack(anchor='w');ttk.Label(p,text='Operations Toolkit services and operational status',style='Muted.TLabel').pack(anchor='w',pady=(3,15));o,b=self.card(p,14,12);o.pack(fill='x');ttk.Label(b,text='cnMaestro',style='CardTitle.TLabel').pack(anchor='w');ttk.Label(b,text='Speed Manager is available from the cnMaestro service menu.',style='SurfaceMuted.TLabel').pack(anchor='w',pady=(5,10));ttk.Button(b,text='Open Speed Manager',style='Accent.TButton',command=lambda:self.show_page('speed_manager')).pack(anchor='w')
 def build_audit_page(self):
  p=self.pages['audit'];p.columnconfigure(0,weight=1);p.rowconfigure(2,weight=1);top=ttk.Frame(p);top.grid(row=0,column=0,sticky='ew');top.columnconfigure(0,weight=1);ttk.Label(top,text='Audit Log',style='PageTitle.TLabel').grid(row=0,column=0,sticky='w');ttk.Button(top,text='Export CSV',style='Accent.TButton',command=self.export_audit_csv).grid(row=0,column=1,sticky='e');ttk.Label(p,text='Recorded cnMaestro package operations',style='Muted.TLabel').grid(row=1,column=0,sticky='w',pady=(2,14));o,b=self.card(p,0,0);o.grid(row=2,column=0,sticky='nsew');b.columnconfigure(0,weight=1);b.rowconfigure(0,weight=1);cols=('time','name','mac','old','target','job','state','verified','success','detail');heads=('Timestamp UTC','Customer','MAC','Old','Target','Job ID','Job state','Verified','Success','Detail');self.audit_tree=ttk.Treeview(b,columns=cols,show='headings')
  for col,head in zip(cols,heads):self.audit_tree.heading(col,text=head);self.audit_tree.column(col,width=115,stretch=False)
  y=ttk.Scrollbar(b,orient='vertical',command=self.audit_tree.yview);x=ttk.Scrollbar(b,orient='horizontal',command=self.audit_tree.xview);self.audit_tree.configure(yscrollcommand=y.set,xscrollcommand=x.set);self.audit_tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew');self.audit_summary=tk.StringVar(value='No audit records');ttk.Label(p,textvariable=self.audit_summary,style='Muted.TLabel').grid(row=3,column=0,sticky='w',pady=(8,0))
 def build_settings(self):
  p=self.pages['settings'];ttk.Label(p,text='Settings',style='PageTitle.TLabel').grid(row=0,column=0,sticky='w');ttk.Label(p,text='Application appearance, paths, and updates',style='Muted.TLabel').grid(row=1,column=0,sticky='w',pady=(3,15));o,b=self.card(p,14,12);o.grid(row=2,column=0,sticky='ew');ttk.Label(b,text='Application',style='CardTitle.TLabel').grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,8));ttk.Label(b,text='Appearance',style='SurfaceMuted.TLabel').grid(row=1,column=0,sticky='w',pady=4);tb=ttk.Combobox(b,textvariable=self.theme,values=['System','Light','Dark'],state='readonly',width=12);tb.grid(row=1,column=1,sticky='w',padx=(18,0));tb.bind('<<ComboboxSelected>>',lambda e:self.apply_theme())
  for i,(label,value) in enumerate([('Data directory',str(DATA)),('Audit database',str(DB)),('Update manifest',str(UPDATE_CONFIG))],2):ttk.Label(b,text=label,style='SurfaceMuted.TLabel').grid(row=i,column=0,sticky='nw',pady=4);ttk.Label(b,text=value,style='Surface.TLabel',wraplength=700).grid(row=i,column=1,sticky='w',padx=(18,0),pady=4)
  ttk.Button(b,text='Check for updates',command=lambda:self.check_updates(False)).grid(row=5,column=0,sticky='w',pady=(12,0))
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
   m=r['mac'];s=self.suggestion(r);var=f"DL {s['dl_pct']:+.1f}% / UL {s['ul_pct']:+.1f}%" if s else '';self.tree.insert('', 'end',iid=m,values=('☑' if m in self.checked else '☐',r['name'],m,r['package'],self.displaypkg(r),var,r['network'],r['tower'],r['ap_mac'],('● Online' if r.get('online') else '● Offline'),('Online for ' if r.get('online') else 'Offline for ')+age(r.get('status_time')),f"{r['downlink']} / {r['uplink']}",str(r.get('cache_age_hours',0))+'h',self.job_status.get(m,''),r['error']))
  self.seltext.set(f'{len(self.checked)} selected');self.update_metrics()
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
 def audit(self):self.show_page('audit')
 def audit_rows(self,limit=1000):
  with sqlite3.connect(DB) as c:c.row_factory=sqlite3.Row;return [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?',(limit,))]
 def refresh_audit(self):
  rows=self.audit_rows();self.audit_tree.delete(*self.audit_tree.get_children())
  for r in rows:self.audit_tree.insert('', 'end',values=(r['timestamp'],r['name'],r['mac'],r['old_package'],r['target_package'],r['job_id'],r['job_state'],r['verified_package'],'Yes' if r['success'] else 'No',r['detail']))
  self.audit_summary.set(f'{len(rows)} records | {DB}')
 def export_audit_csv(self):
  rows=self.audit_rows()
  if not rows:return messagebox.showinfo('Export audit log','There are no audit records to export.')
  path=filedialog.asksaveasfilename(title='Export audit log',defaultextension='.csv',filetypes=[('CSV files','*.csv')],initialfile='operations-toolkit-audit.csv')
  if not path:return
  with open(path,'w',newline='',encoding='utf-8-sig') as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
  messagebox.showinfo('Audit log exported',f'{len(rows)} records exported to:\n{path}')
 def apply_theme(self):
  mode=self.theme.get();dark=mode=='Dark';bg='#1e1e1e' if dark else '#f3f3f3';fg='#f0f0f0' if dark else '#111111';field='#2b2b2b' if dark else '#ffffff';accent='#3a3a3a' if dark else '#e7e7e7';self.configure(bg=bg);self.style.theme_use('clam');self.style.configure('.',background=bg,foreground=fg,fieldbackground=field);self.style.configure('Treeview',background=field,foreground=fg,fieldbackground=field,rowheight=24);self.style.configure('Treeview.Heading',background=accent,foreground=fg);self.style.map('Treeview',background=[('selected','#225a8f' if dark else '#cce5ff')],foreground=[('selected','#ffffff' if dark else '#000000')]);self.style.configure('TEntry',fieldbackground=field);self.style.configure('TCombobox',fieldbackground=field);self.out.configure(bg=field,fg=fg,insertbackground=fg);SETTINGS.write_text(json.dumps({'theme':mode}));self.render()
 def load_settings(self):
  try:self.theme.set(json.loads(SETTINGS.read_text()).get('theme','System'))
  except:pass
  self.apply_theme()
 def vt(self,v):return tuple(int(x) for x in re.findall(r'\d+',str(v))[:4])
 def check_updates(self,auto=False):
  try:url=resolve_manifest_url();url+=('&' if '?' in url else '?')+'t='+str(int(time.time()))
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
