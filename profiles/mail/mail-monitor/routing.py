"""Port of my-discord-agent mailRouteKey/resolveMailRouteKey and durable thread bindings."""
import json
from pathlib import Path
import re
import urllib.request
import urllib.error

class DiscordHTTPError(RuntimeError):
    def __init__(self,status):
        self.status=status
        super().__init__('Discord HTTP '+str(status))

def discord(home,path,method='GET',body=None):
    from dotenv import dotenv_values
    cfg=dotenv_values(home.parent.parent/'.env')
    token=cfg.get('DISCORD_BOT_TOKEN') or cfg.get('DISCORD_TOKEN')
    if not token: raise RuntimeError('Shared Discord transport missing')
    if not path.startswith('/channels/'): raise ValueError('Invalid Discord path')
    req=urllib.request.Request('https://discord.com/api/v10'+path,method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':'Bot '+token,'Content-Type':'application/json','User-Agent':'Hermes-Mail-Monitor'})
    try:
        with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)
    except urllib.error.HTTPError as exc: raise DiscordHTTPError(exc.code) from None

def github(home,path):
    token_path=home/'credentials/github-route-token'
    headers={'Accept':'application/vnd.github+json','User-Agent':'Hermes-Mail-Monitor','X-GitHub-Api-Version':'2022-11-28'}
    if token_path.exists(): headers['Authorization']='Bearer '+token_path.read_text().strip()
    req=urllib.request.Request('https://api.github.com'+path,headers=headers)
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)

def repository(mail):
    address=mail.get('from',{}).get('address','unknown').strip().lower()
    if address != 'notifications@github.com': return None
    list_id=next((h.get('value','') for h in mail.get('headers',[]) if h.get('name','').lower()=='list-id'),'')
    m=re.match(r'^\s*([\w.-]+)/([\w.-]+)(?:\s|$)',list_id,re.I)
    if not m: return None
    # Do not accept path traversal in repository parts.
    if any(p in ('.','..') for p in m.groups()): raise ValueError('Invalid repository part')
    return '/'.join(m.groups()).lower()

def route_key(home,mail,lookup=github):
    repo=repository(mail)
    item=re.search(r'\((?:PR )?#(\d+)\)\s*$',mail.get('subject',''))
    fallback='mail:'+mail.get('from',{}).get('address','unknown').strip().lower()
    key=f'github:{repo}:item:{item[1]}' if repo and item else fallback
    commit=re.match(r'^(?:Re: )?\[[^\]]+\] PR run failed: .+ \(([a-f\d]{7,40})\)\s*$',mail.get('subject',''),re.I)
    if not repo or not commit: return key
    pulls=lookup(home,f'/repos/{repo}/commits/{commit[1]}/pulls?per_page=100')
    if len(pulls)>=100: return key
    numbers={p['number'] for p in pulls if p.get('base',{}).get('repo',{}).get('full_name','').lower()==repo and isinstance(p.get('number'),int) and not isinstance(p['number'],bool) and 0<p['number']<=9007199254740991}
    return f'github:{repo}:item:{next(iter(numbers))}' if len(numbers)==1 else key

def thread_name(key):
    key=re.sub(r'^mail:','',key)
    return re.sub(r'^github:(.+):item:(\d+)$',r'\1 #\2',key)[:100]

def ensure_thread(home,key,parent,api=discord):
    from .runtime import write
    path=home/'cron/mail-monitor/threads.json'
    data=json.loads(path.read_text()) if path.exists() else {'version':1,'parent_id':parent,'bindings':{}}
    if data.get('parent_id')!=parent: raise ValueError('Thread bindings belong to another parent')
    binding=data['bindings'].get(key)
    if binding and binding.get('status')!='ready': raise RuntimeError('Thread creation unknown; reconcile before retry')
    if binding:
        try: thread=api(home,'/channels/'+binding['thread_id'])
        except DiscordHTTPError as exc:
            if exc.status!=404: raise
            thread=None
        if thread:
            if str(thread.get('parent_id'))!=parent or thread.get('type') not in (10,11,12): raise ValueError('Mapped thread parent/type mismatch')
            if thread.get('thread_metadata',{}).get('archived'):
                thread=api(home,'/channels/'+binding['thread_id'],'PATCH',{'archived':False})
                if thread.get('thread_metadata',{}).get('archived'): raise RuntimeError('Thread unarchive failed')
            return binding['thread_id']
    # Persist a creation fence BEFORE POST; a lost reply must never create duplicate threads.
    data['bindings'][key]={'status':'creating'}; write(path,data)
    try:
        thread=api(home,'/channels/'+parent+'/threads','POST',{'name':thread_name(key),'type':11})
        tid=thread.get('id')
        if not isinstance(tid,str) or not tid.isdecimal() or str(thread.get('parent_id'))!=parent: raise RuntimeError('Invalid thread creation receipt')
        data['bindings'][key]={'status':'ready','thread_id':tid}; write(path,data)
        check=api(home,'/channels/'+tid)
        if str(check.get('parent_id'))!=parent or check.get('id')!=tid: raise RuntimeError('Created thread readback failed')
        return tid
    except BaseException:
        if data['bindings'][key].get('status')!='ready':
            data['bindings'][key]={'status':'unknown'}; write(path,data)
        raise
