"""Deterministic Outlook collector, durable single-mail handoff and publication."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from contextlib import contextmanager
from urllib.parse import quote

EMPTY = 'NO_PENDING_MAIL'

def write(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix('.tmp')
    with tmp.open('w') as f:
        os.chmod(tmp, 0o600)
        json.dump(obj, f, ensure_ascii=False)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)
    fd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

@contextmanager
def locked(home):
    d = home / 'cron/mail-monitor'
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (d / 'state.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        p = d / 'state.json'
        s = json.loads(p.read_text()) if p.exists() else {'version':1, 'pending':[], 'current':None, 'delivered':{}, 'generation':0}
        if s.get('version') != 1: raise ValueError('Unsupported mail state')
        yield p, s

def graph(home, path, method='GET', body=None):
    env = dict(os.environ, HERMES_HOME=str(home))
    r = subprocess.run(['node', str(Path(__file__).parent/'graph.cjs')], input=json.dumps({'path':path, 'method':method, 'body':body}), text=True, capture_output=True, env=env, timeout=60)
    if r.returncode: raise RuntimeError('Graph collection/authentication failed')
    return json.loads(r.stdout)

def acknowledge(home, mail_id):
    graph(home, '/me/messages/'+quote(mail_id, safe=''), 'PATCH', {'isRead':True})
    if graph(home, '/me/messages/'+quote(mail_id,safe='')+'?$select=isRead').get('isRead') is not True:
        raise RuntimeError('Graph acknowledgement verification failed')

def collect(home, fetch=graph, ack=acknowledge):
    from .routing import route_key
    with locked(home) as (p,s):
        # Delivery and Outlook acknowledgement are separate. Retry only ACK after confirmed send.
        for mid, receipt in s['delivered'].items():
            if not receipt.get('acknowledged'):
                ack(home, mid); receipt['acknowledged'] = True; write(p,s)
        known = set(s['delivered']) | {m['id'] for m in s['pending']}
        if s['current']: known.add(s['current']['mail']['id'])
        path = '/me/mailFolders/inbox/messages?$top=20&$select=id,subject,from,receivedDateTime&$filter=isRead%20eq%20false'
        meta = fetch(home,path)
        # Follow validated Graph nextLinks; do not let oldest retained mail starve newer arrivals.
        pages = 0; added = 0
        while True:
            for m in meta.get('value',[]):
                mid = m['id']
                if mid in known: continue
                details = fetch(home,'/me/messages/'+quote(mid,safe='')+'?$select=body,webLink,internetMessageHeaders')
                body = details.get('body',{})
                if body.get('contentType','text').lower() != 'text': raise RuntimeError('Graph did not return text body')
                text = body.get('content','')
                if len(text) > 24000: text = text[:24000]+'\n[本文は24000文字で切り詰め]'
                ea = m.get('from',{}).get('emailAddress',{})
                mail = {'id':mid, 'subject':m.get('subject',''), 'from':ea, 'received':m.get('receivedDateTime',''), 'body':text, 'webLink':details.get('webLink',''), 'headers':details.get('internetMessageHeaders',[])}
                mail['route_key'] = route_key(home,mail)
                s['pending'].append(mail)
                known.add(mid); added += 1; write(p,s)
            link = meta.get('@odata.nextLink'); pages += 1
            if not link: break
            if pages >= 100: raise RuntimeError('Graph pagination limit reached; partial collection retained')
            prefix = 'https://graph.microsoft.com/v1.0/me/'
            if not link.startswith(prefix): raise RuntimeError('Invalid Graph nextLink')
            meta = fetch(home,link.removeprefix('https://graph.microsoft.com/v1.0'))
        s['pending'].sort(key=lambda m:m['received']); write(p,s)
        return {'success':True,'added':added,'pending':len(s['pending'])}

def prepare(home):
    with locked(home) as (p,s):
        current = s['current']
        if current and current['status'] != 'ready':
            raise RuntimeError('Delivery unknown; retained without automatic resend')
        if not current and s['pending']:
            current = {'batch_id':uuid.uuid4().hex,'status':'ready','mail':s['pending'].pop(0)}
            s['current'] = current
        if not current: return EMPTY
        s['generation'] += 1; write(p,s)
        return json.dumps({'batch_id':current['batch_id'],'attempt':s['generation'],'mail':current['mail']},ensure_ascii=False)

def send(home,target,text):
    # Shared transport is owned by default; never copy its bot token into mail profile.
    env = dict(os.environ, HERMES_HOME=str(home.parent.parent))
    r = subprocess.run(['hermes','-p','default','send','--to',target,'--json','--file','-'],input=text,text=True,capture_output=True,env=env,timeout=90)
    data = json.loads(r.stdout)
    if r.returncode: raise RuntimeError('Discord delivery failed or unknown')
    return data

def publish(home, batch_id, summary, execution_id, target, sender=send, ack=acknowledge, thread_resolver=None):
    from .routing import ensure_thread
    thread_resolver = thread_resolver or ensure_thread
    if not execution_id or not re.fullmatch(r'discord:\d+',target): raise ValueError('Owned execution and fixed target required')
    if not isinstance(summary,str) or not 1 <= len(summary.strip()) <= 1750: raise ValueError('Summary must be 1..1750 characters')
    if re.search(r'MEDIA:|@everyone|@here|<@|\[\[as_document\]\]',summary,re.I): raise ValueError('Mentions/media forbidden')
    if len(summary.encode('utf-16-le'))//2 > 1800: raise ValueError('Summary exceeds Discord text budget')
    with locked(home) as (p,s):
        for mid,r in s['delivered'].items():
            if r['batch_id'] == batch_id:
                return {'success':True,'already_delivered':True,'acknowledged':r.get('acknowledged',False),'message_id':r['message_id']}
        c = s['current']
        if not c or c['batch_id'] != batch_id or c['status'] != 'ready': raise ValueError('Batch not ready; do not resend')
        key = c['mail'].get('route_key')
        if not key: raise ValueError('Missing deterministic routing; migration required')
        parent = target.split(':')[1]
        thread_id = thread_resolver(home,key,parent)
        c.update(status='sending',execution_id=execution_id,summary=summary,thread_id=thread_id); write(p,s)
        try:
            result = sender(home,'discord:'+parent+':'+thread_id,summary)
            mid = result.get('message_id')
            if result.get('success') is not True or not isinstance(mid,str) or not mid.isdecimal():
                raise RuntimeError('No confirmed Discord message receipt')
        except BaseException:
            c['status']='unknown'; write(p,s); raise
        mail_id = c['mail']['id']
        receipt = {'batch_id':batch_id,'message_id':mid,'execution_id':execution_id,'acknowledged':False,'thread_id':thread_id,'route_key':key}
        s['delivered'][mail_id] = receipt; s['current'] = None; write(p,s)
        try:
            ack(home,mail_id); receipt['acknowledged']=True; write(p,s)
        except Exception:
            return {'success':True,'message_id':mid,'acknowledged':False,'note':'Delivered; Graph acknowledgement retained for retry. Never resend.'}
        return {'success':True,'message_id':mid,'acknowledged':True}
