#!/usr/bin/env python3
"""Benchmark indexed recall in a temporary synthetic DB, never in user memory."""
import argparse
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'backend'))
from hub_runtime import RuntimeStore
from knowledge_search import query_terms, match_expression, MAX_FACTS, MEMORY_BYTES


def run(count=50000, repetitions=80):
    with tempfile.TemporaryDirectory() as tmp:
        store=RuntimeStore(Path(tmp)/'state')
        context=store.create_context('Synthetic benchmark')['id']
        with store.connect() as db:
            db.executemany('''INSERT INTO memory(scope,key,body,source,revision,deleted,updated_at)
                              VALUES(?,?,?,?,?,?,?)''',
                [('global' if i%2 else context,'fact-'+str(i),
                  'Категория '+str(i%100)+' описание устройства номер '+str(i)+
                  (' Уникальный навигатор: маршрут до аэропорта.' if i==min(777,count-1) else ''),
                  'benchmark',1,0,1) for i in range(count)])
            plan=[r['detail'] for r in db.execute('EXPLAIN QUERY PLAN '+store.SEARCH_SQL,
                  (match_expression(query_terms('навигатор'),context),'global',context,MAX_FACTS+1))]
        queries={}
        for query in ('Какой навигатор до аэропорта?','Метеоритные наблюдения','устройство'):
            elapsed,cpu=[],[]
            for _ in range(repetitions):
                wall_start,cpu_start=time.perf_counter(),time.process_time()
                packet=store.packet(context,query=query)
                elapsed.append((time.perf_counter()-wall_start)*1000)
                cpu.append((time.process_time()-cpu_start)*1000)
            queries[query]={'p50Ms':round(statistics.median(elapsed),3),
                           'p95Ms':round(sorted(elapsed)[max(0,__import__('math').ceil(repetitions*.95)-1)],3),
                           'cpuP50Ms':round(statistics.median(cpu),3),
                           'selected':len(packet['memory']),'memoryBytes':packet['retrieval']['memoryBytes'],
                           'hasMore':packet['retrieval']['hasMore']}
        return {'syntheticFacts':count,'repetitions':repetitions,'sqliteVersion':sqlite3.sqlite_version,
                'queryPlan':plan,'queries':queries,'limits':{'facts':MAX_FACTS,'memoryBytes':MEMORY_BYTES},
                'physicalPhoneUsed':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--facts',type=int,default=50000)
    parser.add_argument('--repetitions',type=int,default=80)
    args=parser.parse_args()
    if not 1<=args.facts<=500000 or not 1<=args.repetitions<=1000:
        parser.error('facts must be 1..500000; repetitions 1..1000')
    result=json.dumps(run(args.facts,args.repetitions),ensure_ascii=False,indent=2)+'\n'
    if args.output:args.output.write_text(result)
    print(result,end='')
