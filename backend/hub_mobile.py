"""Existing Android wire contracts connected to the same Hub context and memory."""
import json
import time
from agent_hub import encoded, uid
from conversation import validate, response
from home_link import HomeLinkStore, MAX_IMAGE, MAX_SNAPSHOT, RevisionConflict
from hub_runtime import TERMINAL


class MobileLink:
    def __init__(self, app):
        self.app=app
        self.link=HomeLinkStore(app.store.directory/'phone-link')
        from audio_notes import AudioNotes
        self.notes=AudioNotes(app.store.directory/'audio-notes')
        from speech_dataset import SpeechDataset
        self.speech=SpeechDataset(app.store.directory/'asr-dataset')

    def audio_note(self,device,note):
        value=self.notes.put(device,note)
        key='audio-note:'+value['id']
        body=encoded({'id':value['id'],'title':value['title'],'transcript':value['text'][:6000],
                      'createdAt':value['createdAt'],'detailsTool':'audio_notes'} )
        previous=next((m for m in self.app.store.memories() if m['key']==key),None)
        if not previous or previous['body']!=body:
            self.app.store.put_memory(key,body,'audio-notes',previous['revision'] if previous else 0)
            self.app.store.event('memory.changed',None,None,key=key,scope='global')
        return value

    def knowledge(self,device,source):
        if not source:
            value=self.link.knowledge(uid(device))
            return {'revision':value['revision'],'sources':[{k:s[k] for k in ('id','name','kind','aliases')} for s in value['sources']],
                    'people':value['people'],'note':value['note']}
        item=self.link.knowledge(uid(device),uid(source))
        # Detailed recipes/long documents remain in the authoritative phone store.
        return {**{k:item[k] for k in ('id','name','kind','participant','aliases','savedAt','instructions','observation')},
                'value':item['value'][:8000],'truncated':len(item['value'])>8000}

    def sync(self,device,body):
        result=self.link.sync(uid(device),body)
        key='android:'+device+':knowledge'
        # Shared memory contains discoverable references, not a second mutable copy.
        data=encoded({'device':device,'revision':body['revision'],'sources':[{k:i[k] for k in ('id','name','kind','aliases')} for i in body['sources']]})
        if len(data)>8000:data=encoded({'device':device,'revision':body['revision'],'sourceCount':len(body['sources']),
                                      'detailsTool':'phone_knowledge','truncated':True})
        previous=next((m for m in self.app.store.memories() if m['key']==key),None)
        if not previous or previous['body']!=data:
            self.app.store.put_memory(key,data,'android-sync',previous['revision'] if previous else 0)
            self.app.store.event('memory.changed',None,None,device=device,revision=body['revision'])
        return result

    def turn(self,body):
        validate(body)
        client='android:'+body.get('phoneDevice',body['session'])
        if body['text'].casefold().strip(' .!?') in {'сводка событий','что требует внимания','мои события'}:
            cases = [c for c in self.app.awareness.snapshot()['cases'] if c['effective_status'] in {'open','in_progress'}]
            reply = 'Открытых ситуаций нет.' if not cases else '\n'.join(
                str(i+1)+'. '+c['project']+': '+c['title']+'. '+c['reason'] for i,c in enumerate(cases[:5]))
            return response({'reply':reply[:1500],'action':None},body)
        if self.app.context_router.enabled:
            return response({'reply':self.app.context_router.turn(body,client),'action':None},body)
        context=self.app.client_focus(client)['context_id']
        default_provider='auto' if 'auto' in self.app.providers else 'ollama'
        row=self.app.coordinator.submit(context,client,self.app.config.get('mobile_provider',default_provider),body['text'],body['id'])
        deadline=time.monotonic()+25
        while row['status'] not in TERMINAL and time.monotonic()<deadline:
            time.sleep(.1);row=self.app.store.task(row['id'])
        reply=row['output'] if row['status']=='completed' else row['error'] if row['status'] in TERMINAL else 'Запрос ещё выполняется. Результат сохранится в общей теме.'
        return response({'reply':reply[:1500] or 'Не удалось получить ответ. Проверьте выбранную модель.','action':None},body)

    def deliver(self,row,body):
        # Polling inbox, no push/SMS/device commands. Only already paired devices
        # whose requesting client is explicitly focused on this task's topic.
        for entry in self.link.devices():
            device=entry['device']
            if self.app.store.focus('android:'+device)['context_id']==row['context_id']:
                import uuid
                self.link.publish(device,{'id':str(uuid.uuid5(uuid.NAMESPACE_URL,'hub:'+row['id'])),
                    'kind':'message','source':'assistant-hub','title':'Ответ ассистента','body':body[:4000],
                    'priority':'normal','createdAt':int(time.time()*1000)})
