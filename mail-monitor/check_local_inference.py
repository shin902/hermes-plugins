"""Actual LAN inference, wholly synthetic temporary state/DB. Counts only."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

spec=importlib.util.spec_from_file_location('inference_check',Path(__file__).with_name('__init__.py'),submodule_search_locations=[str(Path(__file__).parent)])
pkg=importlib.util.module_from_spec(spec); sys.modules[spec.name]=pkg; spec.loader.exec_module(pkg)
from inference_check.local_pipeline import LocalLLM, run
from inference_check.runtime import collect, locked
from inference_check.settings import settings


# Two adjacent notices; the first has an internal blank line. Saved bodies
# must contain only their original nonblank lines, not generated prose.
NOTICES = (
    '【合成：保護者会】\n日時：11月5日 10時\n会場：第一会議室',
    '【合成：図書返却】\n返却期限：11月12日\n場所：図書室',
)
BODY = '【合成：保護者会】\n日時：11月5日 10時\n\n会場：第一会議室\n' + NOTICES[1]
status = {'stage':'setup'}


def main():
    cfg=settings()
    with tempfile.TemporaryDirectory() as root:
        home=Path(root)
        cfg={**cfg,'dashboard_db':str(home/'dashboard.db')}
        def fetch(home,path):
            if 'mailFolders' in path:
                return {'value':[{'id':'synthetic-school','subject':'学校ポータル・合成検証',
                    'receivedDateTime':'2026-10-27T00:00:00Z',
                    'from':{'emailAddress':{'address':'portal@school.example'}}}]}
            return {'body':{'contentType':'text','content':BODY}}
        collect(home,fetch=fetch)
        llm=LocalLLM(cfg['local_endpoint'],cfg['local_model'])
        status['stage']='pipeline'
        result=run(home,cfg,llm,limit=1)
        status.update({key:result.get(key) for key in ('success','summaries_saved','school_imported','school_failed')})
        status['inference_calls']=llm.calls
        assert result['success'] and result['summaries_saved']==1 and result['school_imported']==2
        status['stage']='original_bodies'
        sys.path.insert(0,cfg['dashboard_root'])
        from dashboard.school import SchoolService
        items=SchoolService(Path(cfg['dashboard_db'])).list_notices()
        assert len(items)==2 and sorted(item['body'] for item in items)==sorted(NOTICES)
        status['stage']='repeat'
        before=llm.calls
        repeated=run(home,cfg,llm,limit=1)
        assert llm.calls==before and repeated['summaries_saved']==0 and repeated['school_imported']==0
        with locked(home) as (_,state):
            receipt=state['delivered']['synthetic-school']
            assert receipt['notification']=='disabled' and receipt['read'] is False
        print(json.dumps({'synthetic_local_inference':'PASS','inference_calls':before,
                          'summary_saved':1,'school_imported':2,'original_bodies_verified':True,
                          'repeat_inference_calls':llm.calls-before,'discord_calls':0}))

if __name__=='__main__':
    try: main()
    except Exception:
        print(json.dumps({'synthetic_local_inference':'FAIL',**status})); sys.exit(1)
