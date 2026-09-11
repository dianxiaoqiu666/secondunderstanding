"""Real page actions against the already-running local three services.

No HTTP mocks or CAD/confirmation state injection. Start with start.ps1 first.
"""
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/planning_m09/browser'
URL='http://127.0.0.1:8100'
CASES=['medium','square','long','column','edge_block','entrance','concave']
pytestmark=pytest.mark.e2e


@pytest.fixture(scope='module')
def lab_services():
    def healthy():
        try:
            return all(httpx.get(f'http://127.0.0.1:{p}/health',trust_env=False,timeout=.5).json()['status']=='ok'
                       for p in (8100,8101,8102))
        except (httpx.HTTPError,ValueError):return False
    if healthy():
        # Reuse only services whose current owner actually belongs to L3.
        import psutil
        state=json.loads((ROOT/'runtime/processes.json').read_text(encoding='utf-8-sig'))
        owner=psutil.Process(state['supervisor'])
        assert Path(owner.cwd()).resolve()==ROOT.resolve() and 'scripts/start.py' in owner.cmdline()
        yield
        return
    OUT.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,TEMP=str(ROOT/'.tmp'),TMP=str(ROOT/'.tmp'),PYTHONDONTWRITEBYTECODE='1',
             PYTHONIOENCODING='utf-8',L3_TEST_OFFLINE='1',L3_ACCEPTANCE_DIR='outputs/planning_m09/browser',
             PYTHONPATH=str(ROOT/'tests/offline_guard'))
    with (OUT/'launcher.log').open('a',encoding='utf-8') as log:
        process=subprocess.Popen(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'start.ps1')],
            cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline=time.monotonic()+45
            while time.monotonic()<deadline and process.poll() is None:
                if healthy():break
                time.sleep(.2)
            assert healthy() and process.poll() is None,'L3 startup failed; inspect browser/launcher.log'
            yield
        finally:
            if process.poll() is None:
                (ROOT/'runtime/stop.request').write_text('M09 browser finished',encoding='utf-8')
            process.wait(timeout=30)


@pytest.fixture(scope='module')
def browser(lab_services):
    OUT.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,TEMP=str(ROOT/'.tmp'),TMP=str(ROOT/'.tmp'),
             PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.cache/ms-playwright'))
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,env=env,
            args=['--disable-background-networking','--disable-component-update'])
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context=browser.new_context(viewport={'width':1600,'height':1000})
    errors=[];external=[];requests=[]
    def route(value):
        if urlparse(value.request.url).hostname not in ('127.0.0.1','localhost',None):
            external.append(value.request.url);value.abort()
        else:value.continue_()
    context.route('**/*',route)
    tab=context.new_page()
    tab.on('pageerror',lambda error:errors.append(str(error)))
    tab.on('request',lambda request:requests.append({'method':request.method,'url':request.url}))
    yield tab,requests
    context.close()
    assert not errors,errors
    assert not external,external


def generate(page,case):
    page.locator('#scenario-select').select_option(case)
    with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith(f'/api/algorithm/scenarios/{case}/generate'),timeout=30000) as received:
        page.get_by_role('button',name='生成并比较',exact=True).click()
    assert received.value.status==200,received.value.text()
    expect(page.locator('#status-label')).to_have_text('比较已完成',timeout=30000)
    expect(page.locator('#result-panel')).to_be_visible()
    return received.value.json()


@pytest.mark.parametrize('case',CASES)
def test_fixed_space_from_page_to_two_dimensional_comparison(page,case):
    tab,requests=page
    tab.goto(URL)
    tab.get_by_role('link',name='明确空间 · 算法验证').click()
    expect(tab.locator('#scenario-select')).to_be_enabled()
    assert tab.locator('input[type=file]').count()==0
    result=generate(tab,case)
    assert result['input']['input_kind']=='ALGORITHM_VALIDATION'
    for side in ('before','after'):
        expected=result[side]['measurements'];layout=result[side]['layout']
        svg=tab.locator(f'#{side}-canvas')
        expect(svg.get_by_test_id('lab-module')).to_have_count(expected['module_count'])
        expect(svg.get_by_test_id('lab-run')).to_have_count(expected['run_count'])
        expect(svg.get_by_test_id('lab-exclusion')).to_have_count(len(layout['space']['exclusions']))
        expect(svg.get_by_test_id('lab-passage')).to_have_count(len(layout['space']['entrances']))
        expect(svg.get_by_test_id('lab-hole')).to_have_count(len(layout['space']['boundary']['holes_mm']))
        assert expected['geometry_violation_count']==0
        assert expected['bom_quantity']==expected['module_count']
        shelf=layout['shelves'][0]
        svg.get_by_test_id('lab-module').first.click()
        expect(tab.locator(f'#{side}-detail')).to_contain_text(shelf['material_id'])
        tab.locator(f'#{side}-detail').get_by_role('button',name='查看整排').click()
        expect(tab.locator(f'#{side}-detail')).to_contain_text('连续排')
    assert tab.locator('#before-canvas').get_attribute('viewBox')==tab.locator('#after-canvas').get_attribute('viewBox')
    widths=[tab.locator(f'#{side}-canvas').bounding_box()['width'] for side in ('before','after')]
    assert widths[0]==widths[1]
    metric_rows=tab.locator('#metrics-body tr').count()
    assert metric_rows>=10
    expect(tab.locator('#selection-tradeoffs')).to_contain_text('所选货架深度')
    if result['selection']['kept_baseline']:
        expect(tab.locator('#selection-tradeoffs')).to_contain_text('保留基线')
    else:
        for key in ('effective_length_mm','average_run_length_mm','tail_waste_mm'):
            delta=abs(result['after']['measurements'][key]-result['before']['measurements'][key])
            expect(tab.locator('#selection-tradeoffs')).to_contain_text(f'{delta:,.0f} mm')
    # The actual DOM BOM rows are independently read, not assumed from payload.
    for side,column in [('before',3),('after',4)]:
        for row in result[side]['layout']['bom']:
            cells=tab.locator(f'#bom-body tr[data-material-id="{row["material_id"]}"] td')
            assert int(cells.nth(2).inner_text().replace(',',''))==row['default_level_count']
            assert int(cells.nth(column).inner_text().replace(',',''))==row['quantity']
        assert int(tab.locator(f'#{side}-bom-total').inner_text().replace(',',''))==len(result[side]['layout']['shelves'])
    tab.locator('.lab-comparison').screenshot(path=str(OUT/f'{case}-paired.png'))
    tab.screenshot(path=str(OUT/f'{case}-page.png'),full_page=True)
    record={'case':case,'mode':'REAL_BROWSER_REAL_SERVICES','new_context':True,
            'actions':['打开首页算法入口','选择固定场景','生成并比较','分别点击模块','分别查看整排'],
            'extra_confirmation_actions':0,'requests':requests,'result':result}
    (OUT/f'{case}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    assert not [r for r in requests if any(key in r['url'] for key in ('/api/v2/jobs','/confirm','/prepare','/resolve'))]


def test_repeated_generation_and_scene_change_do_not_show_stale_result(page):
    tab,_=page;tab.goto(URL+'/algorithm?scenario=column')
    expect(tab.locator('#scenario-select')).to_be_enabled()
    first=generate(tab,'column')
    again=generate(tab,'column')
    assert first['after']['layout']==again['after']['layout']
    assert first['selection']==again['selection']
    tab.locator('#scenario-select').select_option('entrance')
    expect(tab.locator('#result-panel')).to_be_hidden()
    different=generate(tab,'entrance')
    assert different['input_sha256']!=first['input_sha256']
    tab.reload();expect(tab.locator('#result-panel')).to_be_hidden()
    refreshed=generate(tab,'entrance')
    assert refreshed['after']['layout']==different['after']['layout']
