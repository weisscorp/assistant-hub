"""Read-only AO session source and conservative task overview projection.

Only GET /api/v1/sessions is used. No credentials, transcripts or raw payloads
are persisted. The supported AO session-list DTO does not expose task text.
"""
from copy import deepcopy
from datetime import datetime
import json
from http.client import HTTPException
import math
import os
import threading
import time
from urllib.parse import quote, urlsplit
import urllib.request


LABELS = {
    'working': 'работает', 'waiting_user': 'ждёт пользователя',
    'blocked': 'заблокирован', 'completed': 'завершён',
    'stale': 'давно нет прогресса', 'unknown': 'неизвестно',
}

# AO's current session-status enum. Future values require an explicit policy;
# activity alone must not turn an unrecognized status into confirmed progress.
KNOWN_STATUSES = (
    'working', 'pr_open', 'draft', 'ci_failed', 'review_pending',
    'changes_requested', 'commented', 'approved', 'mergeable', 'merged',
    'needs_input', 'exited', 'idle', 'terminated', 'no_signal',
)


def instant(value):
    """AO HTTP timestamps are RFC3339; missing/invalid times stay unknown."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        number = parsed.timestamp() if parsed.tzinfo else None
        return number if number is not None and math.isfinite(number) and parsed.year > 1 else None
    except (ValueError, OverflowError, OSError):
        return None


def source_ref(source, project, session):
    return f'{source}:{quote(project, safe="")}/{quote(session, safe="")}'


def normalize(session, observed_at, stale_after):
    project, sid = session.get('projectId', ''), session['id']
    activity = session.get('activity') or {}
    raw, state = session.get('status'), activity.get('state')
    event_at = instant(session.get('lastEventAt'))
    if event_at is None:
        event_at = instant(activity.get('lastActivityAt'))
    ready = session.get('statusReadiness') == 'ready'
    status, reason, next_step, decision, blocker = (
        'unknown', 'AO does not confirm current task state.',
        'Inspect the session and confirm its next step.', None, None)
    if ready:
        if session.get('isTerminated') or state == 'exited':
            reason = 'Session stopped; task completion is not established.'
        elif raw not in KNOWN_STATUSES:
            reason = 'AO status is unrecognized; activity cannot establish task state.'
        elif state == 'blocked':
            status, reason = 'blocked', 'AO reports a pending permission or approval decision.'
            blocker = 'Pending AO decision; exact details are not supplied by the session list.'
            decision = 'Inspect and answer the pending decision in AO.'
            next_step = decision
        elif state == 'waiting_input' or raw == 'needs_input':
            status, reason = 'waiting_user', 'AO reports that the agent awaits user input.'
            decision = 'Provide the next instruction in AO.'
            next_step = decision
        elif raw == 'merged':
            status, reason = 'completed', 'AO reports merged delivery; broader task acceptance is not verified.'
            next_step = 'Confirm the expected result against the delivered change.'
        elif state in {'active', 'idle'} or raw == 'working':
            if event_at is not None and 0 <= event_at <= observed_at - stale_after:
                status, reason = 'stale', 'No recent confirmed AO event; silence does not prove a hang.'
            elif state == 'active' or raw == 'working':
                status, reason = 'working', 'AO reports active execution.'
                next_step = 'Wait for the next confirmed update.'
            else:
                reason = 'Idle activity alone does not establish task progress or completion.'
    else:
        reason = 'AO readiness is not ready; activity cannot be treated as live execution.'
    ref = source_ref('ao', project, sid)
    return {
        'ref': ref,
        'source': {'kind': 'ao', 'project_id': project or None, 'session_id': sid,
                   'link': f'ao://sessions/{quote(project, safe="")}/{quote(sid, safe="")}' if project else None},
        'title': session.get('displayName') or sid,
        'expected_result': None,
        # AO v0.13.5 SessionView excludes prompt/update metadata. Keep these
        # reserved fields unavailable until a text source is explicitly chosen.
        'task_direction': None,
        'executor': {'session_id': sid, 'harness': session.get('harness')},
        'last_confirmed_action': None,
        'action_evidence': None,
        'task_text_availability': 'unavailable_in_session_list',
        'next_step': next_step, 'next_step_kind': 'suggested',
        'blocker': blocker, 'expected_decision': decision,
        'status': status, 'status_label': LABELS[status], 'reason': reason,
        'raw_status': raw, 'activity_state': state,
        'last_progress_at': event_at, 'observed_at': observed_at,
        'is_live': True, 'contentKind': 'untrusted-task-report',
    }


class AOOverview:
    """An on-demand, bounded source. Failed reads expose explicitly historical data."""
    def __init__(self, url=None, timeout=5, stale_after=3600, clock=time.time):
        self.url = url or os.environ.get('MINI_TASKS_AO_URL', 'http://127.0.0.1:3001')
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Expected an AO base URL without credentials, query or fragment')
        if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError('Expected positive finite source timeout and stale threshold')
        self.timeout, self.stale_after, self.clock = timeout, stale_after, clock
        self.lock = threading.Lock()
        self.last_success = None
        self.cached = []

    def read_sessions(self):
        # Explicit GET only. Disable proxies and redirects so an AO read cannot
        # silently move to a different destination configured by the environment.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(self.url.rstrip('/') + '/api/v1/sessions', method='GET')
        with opener.open(request, timeout=self.timeout) as response:
            data = response.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError('Oversized AO response')
        payload = json.loads(data)
        sessions = payload.get('sessions') if isinstance(payload, dict) else None
        # AO encodes an empty slice as either [] or null.
        if sessions is None and isinstance(payload, dict) and 'sessions' in payload:
            sessions = []
        if not isinstance(sessions, list):
            raise ValueError('Invalid AO session envelope')
        seen = set()
        for row in sessions:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']
                    or not isinstance(row.get('projectId', ''), str)):
                raise ValueError('Invalid AO session identity')
            if (row.get('projectId', ''), row['id']) in seen:
                raise ValueError('Duplicate AO session identity')
            seen.add((row.get('projectId', ''), row['id']))
            if row.get('activity') is not None and not isinstance(row['activity'], dict):
                raise ValueError('Invalid AO activity')
        return sessions

    def snapshot(self):
        with self.lock:
            try:
                sessions = self.read_sessions()
                now = self.clock()
                rows = [normalize(row, now, self.stale_after) for row in sessions]
            except (OSError, HTTPException, ValueError, TypeError, AttributeError) as error:
                now = self.clock()
                rows = deepcopy(self.cached)
                for row in rows:
                    row.update(previous_status=row['status'], status='unknown',
                               status_label=LABELS['unknown'], is_live=False,
                               reason='Historical observation only: AO source is unavailable.',
                               next_step='Restore AO source access and refresh before acting.',
                               next_step_kind='suggested')
                return {'source': 'ao', 'source_status': 'unavailable',
                        'source_status_label': 'источник недоступен', 'checked_at': now,
                        'last_success_at': self.last_success, 'is_live': False,
                        'error': type(error).__name__, 'tasks': rows}
            self.last_success, self.cached = now, deepcopy(rows)
            return {'source': 'ao', 'source_status': 'available',
                    'source_status_label': 'источник доступен', 'checked_at': now,
                    'last_success_at': now, 'is_live': True, 'tasks': rows}
