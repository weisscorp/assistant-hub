#!/usr/bin/env python3
"""Opt-in live classifier evaluation on synthetic, isolated Hub topics.

No target provider is executed. This measures routing accuracy on this small
suite, separately from mocked protocol tests; it is not a broad quality claim.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid

PROJECT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(PROJECT/'backend'))
from hub_providers import OllamaProvider
from hub_routing import RoutingProvider, ROUTING_INSTRUCTIONS
from hub_runtime import Coordinator, RuntimeStore


CODING_HISTORY=[('user','Добавь в скрипт обработку строк JSONL и тесты.'),
                ('assistant','Подготовлен разбор строк. Осталось добавить тесты на пустую строку.')]
CASES=[
    {'id':'code_new_script','expected':'coding','prompt':'Напиши Python-скрипт, который проверяет JSONL и выводит номера повреждённых строк.'},
    {'id':'code_fix_bug','expected':'coding','prompt':'Исправь ошибку в parse_date: часовой пояс сейчас теряется.'},
    {'id':'code_read_repository','expected':'coding','prompt':'Посмотри в репозитории, где формируется заголовок Authorization, и объясни найденную цепочку вызовов.'},
    {'id':'code_investigate_build','expected':'coding','prompt':'Проверь, почему сборка приложения падает после обновления зависимости. Найди причину по логам и файлам проекта.'},
    {'id':'code_configuration','expected':'coding','prompt':'Добавь отдельный dev-профиль в конфигурацию сборки, с портом 18318.'},
    {'id':'code_limited_change_negation','expected':'coding','prompt':'Не переписывай весь проект. Исправь только валидацию пустого имени в форме.'},
    {'id':'code_inline_review','expected':'coding','prompt':'Проверь эту функцию и найди ошибку: def first(items): return items[1]'},
    {'id':'code_continue_context','expected':'coding','prompt':'Продолжай.','history':CODING_HISTORY},
    {'id':'code_fix_context','expected':'coding','prompt':'Поправь.','history':[('user','Проверь тест для пустого списка.'),('assistant','В тесте ошибочно ожидается первый элемент вместо пустого результата. Исправлять этот тест?')]},
    {'id':'code_yes_context','expected':'coding','prompt':'Да.','history':[('user','Добавь обработку пустой строки в скрипт.'),('assistant','Добавить отдельный тест на пустую строку вместе с исправлением функции?')]},
    {'id':'general_recursion_concept','expected':'general','prompt':'Что такое рекурсия? Объясни простыми словами.'},
    {'id':'general_testing_concept','expected':'general','prompt':'Почему программисты пишут тесты?'},
    {'id':'general_no_code_change','expected':'general','prompt':'Не меняй код. Просто объясни, чем HTTP отличается от HTTPS.'},
    {'id':'general_dictated_message','expected':'general','prompt':'Отправь контакту Вера сообщение: исправь баг в сборке завтра утром.'},
    {'id':'general_dictated_note','expected':'general','prompt':'Запиши заметку: написать код для обработки ошибок сервера.'},
    {'id':'general_topic_switch','expected':'general','prompt':'Теперь другая тема: поставь таймер на две минуты.','history':CODING_HISTORY},
    {'id':'general_continue_without_history','expected':'general','prompt':'Продолжай.'},
    {'id':'general_fix_without_history','expected':'general','prompt':'Поправь.'},
    {'id':'general_yes_without_history','expected':'general','prompt':'Да.'},
    {'id':'general_continue_context','expected':'general','prompt':'Продолжай.','history':[('user','Расскажи, как приготовить овощной суп.'),('assistant','Сначала нарежь овощи. Дальше расскажу про варку и специи.')]}
]


class NonExecutingFixture:
    def status(self):return {'ready':True,'detail':'Synthetic evaluation fixture'}
    def run(self,*args,**kwargs):raise AssertionError('Live evaluation must never execute target agents')


def evaluate(url,model):
    outcomes=[]
    with tempfile.TemporaryDirectory(prefix='hub-routing-eval-') as directory:
        store=RuntimeStore(directory)
        providers={'ollama':OllamaProvider(url,model),'fixture-general':NonExecutingFixture(),'fixture-coding':NonExecutingFixture()}
        router=RoutingProvider({'classifier':'ollama','general':'fixture-general','coding':'fixture-coding'},providers)
        coordinator=Coordinator(store,providers)
        for case in CASES:
            context=store.create_context('Synthetic evaluation '+case['id'])['id']
            for role,content in case.get('history',[]):store.append(context,role,content,'synthetic-fixture')
            row,_=store.create_task(context,'synthetic-evaluation','auto',case['prompt'],str(uuid.uuid4()))
            started=time.monotonic()
            outcome={**case,'actual':None,'passed':False}
            try:
                outcome['actual']=router.classify(row,coordinator,threading.Event())
                outcome['passed']=outcome['actual']==case['expected']
            except Exception as error:
                outcome['error_type']=type(error).__name__
            outcome['elapsed_seconds']=round(time.monotonic()-started,3)
            outcomes.append(outcome)
            print(case['id']+': '+('PASS' if outcome['passed'] else 'FAIL')+
                  ' expected='+case['expected']+' actual='+str(outcome['actual']),flush=True)
    return {'created_at':datetime.now(timezone.utc).isoformat(),'evaluation':'live_ollama_semantic_routing',
            'fixture_data':'synthetic','targets_executed':0,'temporary_state':True,'url':url,'model':model,
            'suite_version':1,'routing_instructions_sha256':hashlib.sha256(ROUTING_INSTRUCTIONS.encode()).hexdigest(),
            'routing_module_sha256':hashlib.sha256((PROJECT/'backend/hub_routing.py').read_bytes()).hexdigest(),
            'total':len(outcomes),'passed':sum(o['passed'] for o in outcomes),'results':outcomes,
            'limitations':'Small hand-authored synthetic suite. No target execution, repository inspection, end-to-end agent test or general accuracy estimate.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True,help='Explicit owner-approved Ollama endpoint')
    parser.add_argument('--model',required=True,help='Existing model; this script never pulls one')
    parser.add_argument('--output',type=Path,help='JSON report path; default: verification/routing-live-<UTC timestamp>.json')
    args=parser.parse_args()
    parsed=urllib.parse.urlsplit(args.url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('Use an HTTP(S) endpoint without credentials, query or fragment')
    report=evaluate(args.url.rstrip('/'),args.model)
    output=args.output or PROJECT/'verification'/('routing-live-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('Report: '+str(output)+'; '+str(report['passed'])+'/'+str(report['total'])+' passed')
    return 0 if report['passed']==report['total'] else 1


if __name__=='__main__':sys.exit(main())
