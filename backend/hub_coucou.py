"""Narrow loopback Coucou UI facade; no owner token or general API access."""
from agent_hub import uid
from hub_runtime import TERMINAL


def desktop_hub(app, method, body=None):
    store = app.store
    focus = app.client_focus('coucou')
    if method == 'POST':
        if not isinstance(body, dict):
            raise ValueError('Expected an action')
        action = body.get('action')
        if action == 'focus' and set(body) == {'action', 'context', 'revision'}:
            store.switch('coucou', uid(body['context']), body['revision'])
        elif action == 'send' and set(body) == {'action', 'context', 'provider', 'prompt', 'id'}:
            # Capture the selected topic in the request; later focus changes must
            # never redirect an in-flight message or an idempotent retry.
            return app.coordinator.submit(uid(body['context']), 'coucou', body['provider'], body['prompt'], uid(body['id']))
        else:
            raise ValueError('Unknown desktop Hub action')
        focus = app.client_focus('coucou')
    elif method != 'GET':
        raise ValueError('Use GET or POST')
    context = focus['context_id']
    with store.connect() as db:
        notifications = [dict(row) for row in db.execute(
            'SELECT * FROM notifications ORDER BY created_at DESC,id DESC LIMIT 60')]
    return {'focus': focus, 'contexts': store.contexts(),
            'providers': {name: {'ready': bool(value.get('ready', value.get('available', False))),
                                 'detail': str(value.get('detail', ''))}
                          for name, value in app.statuses().items()},
            'history': [entry for entry in store.history(context) if entry['role'] in {'user', 'assistant'}],
            'tasks': [{key: row[key] for key in ('id', 'status', 'output', 'error')}
                      for row in store.tasks(context) if row['status'] not in TERMINAL or row['error']],
            'notifications': notifications}
